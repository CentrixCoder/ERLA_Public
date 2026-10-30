"""Learned sparse retrieval channel (0.7.0, Phase 2.4; opt-in `sparse.enabled: true`).

A local fastembed sparse model (default Qdrant BM42; SPLADE++ as an option) gives each chunk a weighted
bag of vocabulary terms that includes related terms the text does not print ("crack growth" also weighs
"da/dN"-style vocabulary), which helps research questions worded differently from the library. Its ranking
joins BM25 and the dense vectors in reciprocal rank fusion (search.py).

Storage: table `sparse_vectors` in the index (term ids int32, weights float32 per chunk; the index file is
already a permitted write, G1.2 - no new file). Search builds the inverted index in memory from that table
once per process and rebuilds it when `sparse_built_at` changes. Duplicate documents are never encoded (S6).
Library text never leaves this computer: the model runs locally (ONNX).
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from . import store

DEFAULT_MODEL = "Qdrant/bm42-all-minilm-l6-v2-attentions"
NOT_DUP = "c.doc_id NOT IN (SELECT doc_id FROM duplicates)"
_MODELS: dict[str, Any] = {}
_INDEX: dict[str, Any] = {}


def enabled(cfg) -> bool:
    return bool(cfg is not None and (getattr(cfg, "sparse", None) or {}).get("enabled"))


def model_name(cfg) -> str:
    return str((cfg.sparse or {}).get("model") or DEFAULT_MODEL)


def model(cfg):
    name = model_name(cfg)
    if name not in _MODELS:
        from fastembed import SparseTextEmbedding
        _MODELS[name] = SparseTextEmbedding(name, cache_dir=str(cfg.agent_root / "models"))
    return _MODELS[name]


def _encode(cfg, texts: list[str], kind: str = "passage") -> list[tuple[np.ndarray, np.ndarray]]:
    m = model(cfg)
    gen = m.query_embed(texts) if kind == "query" else m.embed(texts, batch_size=32)
    return [(np.asarray(e.indices, dtype=np.int32), np.asarray(e.values, dtype=np.float32)) for e in gen]


def _merge(parts: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    """Windows of one chunk -> one sparse vector (max weight per term)."""
    if len(parts) == 1:
        return parts[0]
    acc: dict[int, float] = {}
    for idx, val in parts:
        for i, v in zip(idx.tolist(), val.tolist()):
            if v > acc.get(i, 0.0):
                acc[i] = v
    keys = sorted(acc)
    return np.asarray(keys, dtype=np.int32), np.asarray([acc[k] for k in keys], dtype=np.float32)


def embed_pending(con, cfg, batch: int = 128, limit: int | None = None, log=print, progress=None) -> int:
    """Encode non-duplicate chunks without a sparse vector of the configured model. Resumable."""
    from .embed import chunk_inputs
    name = model_name(cfg)
    sql = (f"FROM chunks c JOIN documents d ON d.doc_id=c.doc_id LEFT JOIN sparse_vectors s ON s.chunk_id=c.chunk_id "
           f"WHERE (s.chunk_id IS NULL OR s.model IS NOT ?) AND {NOT_DUP}")
    todo = con.execute(f"SELECT count(*) {sql}", (name,)).fetchone()[0]
    if limit:
        todo = min(todo, limit)
    log(f"sparse: {todo} chunks pending (model {name})")
    done, t0 = 0, time.time()
    while done < todo:
        rows = con.execute(f"SELECT c.chunk_id, c.text, c.locator, d.title, d.standard_id, d.revision {sql} "
                           "ORDER BY c.chunk_id LIMIT ?", (name, min(batch, todo - done))).fetchall()
        if not rows:
            break
        inputs = [chunk_inputs(cfg, r["text"] or "", r["title"], r["standard_id"], r["revision"], r["locator"])
                  for r in rows]
        enc = _encode(cfg, [w for ws in inputs for w in ws])
        out, i = [], 0
        for r, ws in zip(rows, inputs):
            idx, val = _merge(enc[i:i + len(ws)])
            i += len(ws)
            out.append((r["chunk_id"], name, idx.tobytes(), val.tobytes()))
        con.executemany("INSERT OR REPLACE INTO sparse_vectors(chunk_id, model, indices, vals) VALUES (?,?,?,?)", out)
        con.commit()
        done += len(rows)
        if progress:
            progress()
        if done % (batch * 20) < batch:
            log(f"  {done}/{todo} ({done / max(time.time() - t0, 1e-6):.0f} chunks/s)")
    return done


def prune(con) -> int:
    """Drop sparse vectors of chunks that are gone or now belong to a duplicate document."""
    if not store.has_table(con, "sparse_vectors"):
        return 0
    cur = con.execute("DELETE FROM sparse_vectors WHERE chunk_id NOT IN (SELECT c.chunk_id FROM chunks c "
                      f"WHERE {NOT_DUP})")
    con.commit()
    return cur.rowcount or 0


def mark_built(con) -> int:
    """Record that the sparse vectors changed, so readers rebuild their in-memory index."""
    n = con.execute("SELECT count(*) FROM sparse_vectors").fetchone()[0] if store.has_table(con, "sparse_vectors") else 0
    store.set_meta(con, "sparse_rows", str(n))
    store.set_meta(con, "sparse_built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    con.commit()
    return int(n)


def _load(cfg, con) -> dict[str, Any] | None:
    """In-memory inverted index {terms, ptr, pos, vals, chunk_ids} for the configured model, cached."""
    if not store.has_table(con, "sparse_vectors"):
        return None
    key = f"{cfg.index_path}|{store.get_meta(con, 'sparse_built_at')}|{model_name(cfg)}"
    if _INDEX.get("key") == key:
        return _INDEX["idx"]
    terms: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    pos: list[np.ndarray] = []
    chunk_ids: list[int] = []
    cur = con.execute("SELECT s.chunk_id, s.indices, s.vals FROM sparse_vectors s JOIN chunks c ON c.chunk_id=s.chunk_id "
                      f"WHERE s.model=? AND {NOT_DUP} ORDER BY s.chunk_id", (model_name(cfg),))
    for cid, ib, vb in cur:
        idx = np.frombuffer(ib, dtype=np.int32)
        if not len(idx):
            continue
        terms.append(idx)
        vals.append(np.frombuffer(vb, dtype=np.float32))
        pos.append(np.full(len(idx), len(chunk_ids), dtype=np.int32))
        chunk_ids.append(cid)
    if not chunk_ids:
        _INDEX.update(key=key, idx=None)
        return None
    t = np.concatenate(terms)
    order = np.argsort(t, kind="stable")
    t = t[order]
    uniq, start = np.unique(t, return_index=True)
    idx = {"terms": uniq, "ptr": np.append(start, len(t)), "pos": np.concatenate(pos)[order],
           "vals": np.concatenate(vals)[order], "chunk_ids": np.asarray(chunk_ids, dtype=np.int64)}
    _INDEX.update(key=key, idx=idx)
    return idx


def search(cfg, con, query: str, top: int = 600) -> list[tuple[int, float]]:
    """[(chunk_id, score)] by sparse dot product; [] when the channel is off or not built."""
    if not enabled(cfg):
        return []
    idx = _load(cfg, con)
    if idx is None:
        return []
    qi, qv = _encode(cfg, [query], kind="query")[0]
    scores = np.zeros(len(idx["chunk_ids"]), dtype=np.float32)
    for t, w in zip(qi.tolist(), qv.tolist()):
        j = int(np.searchsorted(idx["terms"], t))
        if j >= len(idx["terms"]) or idx["terms"][j] != t:
            continue
        a, b = idx["ptr"][j], idx["ptr"][j + 1]
        np.add.at(scores, idx["pos"][a:b], w * idx["vals"][a:b])
    nz = int(np.count_nonzero(scores))
    if not nz:
        return []
    top = min(top, nz)
    best = np.argpartition(-scores, top - 1)[:top]
    best = best[np.argsort(-scores[best])]
    return [(int(idx["chunk_ids"][i]), float(scores[i])) for i in best]
