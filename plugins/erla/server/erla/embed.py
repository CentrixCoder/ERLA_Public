"""Phase 4: local dense embeddings (skill S6 step 2).

Model runs LOCALLY via fastembed/ONNX (no remote API, no PyTorch).
Vectors: L2-normalised float16, stored in the `embeddings` table (resumable).

Search index (S7), rebuilt by `export_matrix`:
  * faiss installed  -> `vectors.faiss` (exact IndexFlatIP by default; `embeddings.ann: hnsw`
    for approximate HNSW) + `vector_ids.npy`. Memory-mapped on load.
  * faiss missing, or `embeddings.ann: none` -> `vectors.f16.npy` brute-force matrix + `vector_ids.npy`
    (the no-faiss path; load also falls back to it when `vectors.faiss` is absent).
Chunks of duplicate documents (exact or near, table `duplicates`) are never embedded (S6).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import safeio

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

MAX_CHARS = 2000          # ~512 tokens: model context limit (per window)
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_MODELS: dict[str, Any] = {}
NOT_DUP = "c.doc_id NOT IN (SELECT doc_id FROM duplicates)"   # S6

# Query / passage instructions per model family (Phase 2.3). fastembed's query_embed adds none, so ERLA
# does: (model prefix, query instruction, passage instruction, instruction required by the model card).
_BGE_Q = "Represent this sentence for searching relevant passages: "
INSTRUCTIONS: list[tuple[str, str, str, bool]] = [
    ("BAAI/bge-", _BGE_Q, "", False),                     # v1.5: optional, slightly better for short queries
    ("snowflake/snowflake-arctic-embed", _BGE_Q, "", True),
    ("mixedbread-ai/mxbai-embed-large", _BGE_Q, "", True),
    ("nomic-ai/nomic-embed-text", "search_query: ", "search_document: ", True),
    ("intfloat/", "query: ", "passage: ", True),
]


def model_name(cfg) -> str:
    return str(cfg.embeddings.get("model") or DEFAULT_MODEL)


def instructions(cfg) -> tuple[str, str]:
    """(query prefix, passage prefix) for the configured model. `embeddings.query_instruction` true/false
    forces the query prefix on or off; unset follows the model card (required -> on, optional -> off)."""
    name = model_name(cfg)
    force = cfg.embeddings.get("query_instruction")
    for pre, q, p, required in INSTRUCTIONS:
        if name.startswith(pre):
            use_q = required if force is None else bool(force)
            return (q if use_q else ""), p
    return ("", "")


def window_settings(cfg) -> tuple[int, int, int]:
    """(window chars, overlap chars, max windows per chunk). window 0 = the 0.6 rule (first 2,000 chars)."""
    e = cfg.embeddings
    return (int(e.get("window_chars", 1500)), int(e.get("window_overlap", 200)), int(e.get("max_windows", 8)))


def signature(cfg) -> str:
    """What a stored vector was computed with: the model, plus the contextual header and window settings
    when they differ from the 0.6 rule. A vector with another signature is re-embedded (audit F7)."""
    name = model_name(cfg)
    ctx = bool(cfg.embeddings.get("contextual", True))
    win, ov, mx = window_settings(cfg)
    _q, p = instructions(cfg)
    if not ctx and win <= 0 and not p:
        return name                                  # 0.6-compatible rows keep their plain model name
    return f"{name}|ctx{int(ctx)}|w{win}-{ov}-{mx}|p{int(bool(p))}"


def model(cfg):
    name = model_name(cfg)
    if name not in _MODELS:
        from fastembed import TextEmbedding
        cache = cfg.embeddings.get("cache_dir") or (cfg.agent_root / "models")   # cache_dir: scripts/embed_trial.py
        _MODELS[name] = TextEmbedding(name, cache_dir=str(cache))
    return _MODELS[name]


def encode(cfg, texts: list[str], batch: int = 64, kind: str = "passage") -> np.ndarray:
    q, p = instructions(cfg)
    pre = q if kind == "query" else p
    vecs = np.asarray(list(model(cfg).embed([pre + t[:MAX_CHARS] for t in texts], batch_size=batch)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    return vecs


def context_header(title: str | None, standard_id: str | None, revision: str | None, locator: str | None) -> str:
    """Phase 2.1: what the chunk is from, embedded with it (title, standard and revision, locator)."""
    parts = [str(title or "").strip()[:150]]
    if standard_id:
        parts.append(f"{standard_id}{(' ' + revision) if revision else ''}")
    if locator:
        parts.append(str(locator)[:40])
    return " | ".join(p for p in parts if p)


def windows(text: str, size: int, overlap: int, max_n: int) -> list[str]:
    """Phase 2.2 (audit F2): overlapping windows that cover the whole chunk, cut at whitespace."""
    text = text or ""
    if size <= 0:
        return [text[:MAX_CHARS]]
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start = 0
    step = max(1, size - max(0, overlap))
    while start < len(text) and len(out) < max_n:
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(" ", start + step, end)
            end = cut if cut > start else end
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return out


def chunk_inputs(cfg, text: str, title: str | None, standard_id: str | None, revision: str | None,
                 locator: str | None) -> list[str]:
    """The strings embedded for one chunk: its windows, each prefixed by the contextual header."""
    win, ov, mx = window_settings(cfg)
    ctx = bool(cfg.embeddings.get("contextual", True))
    head = context_header(title, standard_id, revision, locator) if ctx else ""
    size = (win - len(head) - 1) if win > 0 and head else win
    return [f"{head}\n{w}" if head else w for w in windows(text, max(size, 200) if win > 0 else win, ov, mx)]


def paths(cfg) -> tuple[Path, Path]:
    """Brute-force matrix + id array (fallback backend)."""
    return cfg.agent_root / "vectors.f16.npy", cfg.agent_root / "vector_ids.npy"


def faiss_path(cfg) -> Path:
    return cfg.agent_root / "vectors.faiss"


def _faiss():
    try:
        import faiss  # noqa: F401
        return faiss
    except Exception:
        return None


def prune_duplicate_embeddings(con) -> int:
    """S6: drop vectors of chunks whose document is now a duplicate (index-only write)."""
    cur = con.execute("DELETE FROM embeddings WHERE chunk_id IN (SELECT chunk_id FROM chunks c WHERE "
                      + NOT_DUP.replace("NOT IN", "IN") + ")")
    con.commit()
    return cur.rowcount or 0


def embed_pending(con, cfg, batch: int = 256, limit: int | None = None, log=print, progress=None) -> int:
    """Embed non-duplicate chunks that have no vector, or one computed with another signature (model,
    contextual header, windows). Resumable; commits per batch. Each row stores one float16 vector per
    window of the chunk, concatenated."""
    from . import store
    sig = signature(cfg)
    pending_sql = (f"FROM chunks c JOIN documents d ON d.doc_id=c.doc_id LEFT JOIN embeddings e ON e.chunk_id=c.chunk_id "
                   f"WHERE (e.chunk_id IS NULL OR e.model IS NOT ?) AND {NOT_DUP}")
    todo = con.execute(f"SELECT count(*) {pending_sql}", (sig,)).fetchone()[0]
    if limit:
        todo = min(todo, limit)
    log(f"embed: {todo} chunks pending (vectors {sig}; duplicate documents skipped)")
    done, t0 = 0, time.time()
    dim = None
    while done < todo:
        rows = con.execute(f"SELECT c.chunk_id, c.text, c.locator, d.title, d.standard_id, d.revision {pending_sql} "
                           f"ORDER BY c.chunk_id LIMIT ?", (sig, min(batch, todo - done))).fetchall()
        if not rows:
            break
        inputs = [chunk_inputs(cfg, r["text"] or "", r["title"], r["standard_id"], r["revision"], r["locator"])
                  for r in rows]
        flat = [w for ws in inputs for w in ws]
        vecs = encode(cfg, flat).astype(np.float16)
        dim = int(vecs.shape[1])
        out, i = [], 0
        for r, ws in zip(rows, inputs):
            out.append((r["chunk_id"], sig, vecs[i:i + len(ws)].tobytes()))
            i += len(ws)
        con.executemany("INSERT OR REPLACE INTO embeddings(chunk_id, model, vector) VALUES (?,?,?)", out)
        store.set_meta(con, "embed_dim", str(dim))
        store.set_meta(con, f"embed_dim:{model_name(cfg)}", str(dim))   # per model: a switch back never misreads
        store.set_meta(con, "embed_signature", sig)
        con.commit()
        done += len(rows)
        if progress:
            progress()
        if done % (batch * 20) < batch:
            log(f"  {done}/{todo} ({done / max(time.time() - t0, 1e-6):.0f} chunks/s)")
    return done


def _dim(con, cfg) -> int:
    from . import store
    name = model_name(cfg)
    d = store.get_meta(con, f"embed_dim:{name}")
    if d:
        return int(d)
    # no record for this model: a 0.6 row of it holds exactly one vector
    row = con.execute(f"SELECT vector FROM embeddings e WHERE {MODEL_SQL} AND e.model NOT LIKE '%|%' LIMIT 1",
                      _model_args(name)).fetchone()
    if row and row[0]:
        return len(row[0]) // 2
    d = store.get_meta(con, "embed_dim")
    return int(d) if d else 384


MODEL_SQL = "(e.model = ? OR substr(e.model, 1, ?) = ?)"     # the model's rows, any signature


def _model_args(name: str) -> tuple:
    return (name, len(name) + 1, name + "|")


def _iter_vectors(con, batch: int = 50_000, sig: str | None = None, dim: int | None = None):
    """Yield (ids int64[], vecs float32[n, d]) in batches for non-duplicate chunks, one row per window
    (a chunk with several windows repeats its id). `sig` names a model: only its vectors are read, so a
    model change never mixes two vector spaces (audit F7)."""
    where = f"WHERE {NOT_DUP}" + (f" AND {MODEL_SQL}" if sig else "")
    cur = con.execute("SELECT e.chunk_id, e.vector FROM embeddings e JOIN chunks c ON c.chunk_id=e.chunk_id "
                      f"{where} ORDER BY e.chunk_id", _model_args(sig) if sig else ())
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        if dim is None:
            vecs = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)
            ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        else:
            counts = [len(r[1]) // (2 * dim) for r in rows]
            vecs = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(-1, dim)
            ids = np.repeat(np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows)), counts)
        yield ids, vecs.astype(np.float32)


def _new_faiss_index(faiss, dim: int, cfg):
    ann = str(cfg.embeddings.get("ann", "flat")).lower()
    if ann == "hnsw":
        idx = faiss.IndexHNSWSQ(dim, faiss.ScalarQuantizer.QT_fp16, int(cfg.embeddings.get("hnsw_m", 32)),
                                faiss.METRIC_INNER_PRODUCT)
        idx.hnsw.efConstruction = int(cfg.embeddings.get("hnsw_ef_construction", 80))
        return idx, "hnsw"
    return faiss.IndexFlatIP(dim), "flat"


def _replace(tmp: Path, final: Path) -> None:
    safeio.replace_at(safeio.WritablePath(tmp), safeio.WritablePath(final))   # both checked in export_matrix


def export_matrix(con, cfg, progress=None) -> int:
    """Rebuild the vector search index from the embeddings table (S7). Returns row count."""
    from . import store
    vp, ip = paths(cfg)
    fp = faiss_path(cfg)
    g = safeio.Guard.from_config(cfg)
    vp, ip, fp = (safeio.check(g, p) for p in (vp, ip, fp))
    faiss = _faiss() if str(cfg.embeddings.get("ann", "flat")).lower() != "none" else None
    name = model_name(cfg)                # every vector of this model: 0.6 rows and windowed rows share its space
    dim = _dim(con, cfg)
    ids_all: list[np.ndarray] = []
    if faiss is not None:
        index, kind = None, None
        for ids, vecs in _iter_vectors(con, sig=name, dim=dim):
            if index is None:
                index, kind = _new_faiss_index(faiss, vecs.shape[1], cfg)
                if not index.is_trained:
                    index.train(vecs)
            index.add(vecs)
            ids_all.append(ids)
            if progress:
                progress()
        if index is None:
            index, kind = faiss.IndexFlatIP(dim), "flat"
        tmp = safeio.faiss_write_at(safeio.check(g, fp.with_suffix(".tmp")), faiss, index)
        ids = np.concatenate(ids_all) if ids_all else np.empty(0, np.int64)
        itmp = safeio.np_save_at(safeio.check(g, ip.with_suffix(".tmp.npy")), ids)
        _replace(tmp, fp)
        _replace(itmp, ip)
        safeio.unlink_at(vp)              # stale brute-force matrix would not match the ids
        backend = f"faiss-{kind}"
    else:
        n = int(con.execute("SELECT coalesce(sum(length(e.vector)), 0) FROM embeddings e JOIN chunks c "
                            f"ON c.chunk_id=e.chunk_id WHERE {NOT_DUP} AND {MODEL_SQL}",
                            _model_args(name)).fetchone()[0]) // (2 * dim)
        tmp = safeio.check(g, vp.with_suffix(".tmp.npy"))
        mat, i, ids = None, 0, np.empty(n, dtype=np.int64)
        for bid, vecs in _iter_vectors(con, sig=name, dim=dim):
            if mat is None:
                mat = safeio.np_open_memmap_at(tmp, np.float16, (n, vecs.shape[1]))
            mat[i:i + len(bid)] = vecs.astype(np.float16)
            ids[i:i + len(bid)] = bid
            i += len(bid)
        if mat is None:
            mat = safeio.np_open_memmap_at(tmp, np.float16, (0, dim))
        mat.flush()
        del mat
        _replace(tmp, vp)
        safeio.np_save_at(ip, ids)
        safeio.unlink_at(fp)
        backend = "numpy-f16"
    rows = int(len(ids))
    store.set_meta(con, "vector_backend", backend)
    store.set_meta(con, "vector_rows", str(rows))
    store.set_meta(con, "vector_chunks", str(int(len(np.unique(ids))) if len(ids) else 0))
    store.set_meta(con, "vector_model", name)
    store.set_meta(con, "vector_dim", str(dim))
    store.set_meta(con, "vector_built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    con.commit()
    _CACHE.clear()
    return rows


_CACHE: dict = {}


def _load(cfg):
    """Load (and cache by mtime) whichever backend export_matrix wrote. Returns (kind, obj, ids) or None."""
    vp, ip = paths(cfg)
    fp = faiss_path(cfg)
    if not ip.is_file():
        return None
    faiss = _faiss() if fp.is_file() else None
    src = fp if faiss is not None else vp
    if not src.is_file():
        return None
    key = (str(src), src.stat().st_mtime, ip.stat().st_mtime)
    if _CACHE.get("key") != key:
        if faiss is not None:
            try:
                obj = faiss.read_index(str(fp), faiss.IO_FLAG_MMAP)
            except Exception:
                obj = faiss.read_index(str(fp))
            hnsw = getattr(faiss, "downcast_index", lambda x: x)(obj)
            if hasattr(hnsw, "hnsw"):
                hnsw.hnsw.efSearch = int(cfg.embeddings.get("hnsw_ef_search", 256))
            _CACHE.update(key=key, kind="faiss", obj=obj, ids=np.load(ip))
        else:
            _CACHE.update(key=key, kind="numpy", obj=np.load(vp, mmap_mode="r"), ids=np.load(ip))
    return _CACHE["kind"], _CACHE["obj"], _CACHE["ids"]


def vector_backend(cfg) -> str:
    """'faiss-<kind>' | 'numpy-f16' | 'none' (for stats/diagnostics)."""
    got = _load(cfg)
    if not got:
        return "none"
    if got[0] == "numpy":
        return "numpy-f16"
    return "faiss-hnsw" if hasattr(_faiss().downcast_index(got[1]), "hnsw") else "faiss-flat"


def vector_mismatch(cfg, con) -> str | None:
    """Why the built vector index must not answer this site's queries, or None. A vector index built with
    another model lives in another vector space: searching it with this model's query vector returns noise
    (audit F7). An index exported before 0.7.0 records no model; its dimension is checked at search time."""
    from . import store
    if not store.has_table(con, "meta"):
        return None
    built = store.get_meta(con, "vector_model")
    if built and built != model_name(cfg):
        return f"vector index built with {built}, site model is {model_name(cfg)}: run erla refresh"
    # chunks deleted after the export: their ids may now belong to other text (correctness audit 2026-10-31: an
    # unrelated document ranked first on a deleted document's vector until the next export)
    try:
        deleted = float(store.get_meta(con, "chunks_deleted_at") or 0)
        exported = paths(cfg)[1].stat().st_mtime
    except (OSError, ValueError):
        return None
    if deleted > exported:
        return "chunks changed since the vector index was built: vectors resume after the next erla refresh"
    return None


def _dedupe(pairs, top: int) -> list[tuple[int, float]]:
    """Window hits -> chunk hits: a chunk keeps its best window (pairs arrive best first)."""
    seen: set[int] = set()
    out: list[tuple[int, float]] = []
    for cid, sc in pairs:
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, sc))
        if len(out) >= top:
            break
    return out


def vector_search(cfg, query: str, top: int = 600) -> list[tuple[int, float]]:
    """Return [(chunk_id, cosine)] best matches (a chunk scores by its best window); [] if no vectors
    are built or the index dimension does not match the model."""
    got = _load(cfg)
    if not got:
        return []
    kind, obj, ids = got
    if len(ids) == 0:
        return []
    q = encode(cfg, [query], kind="query")[0]
    want = min(len(ids), top * 3)        # windows: a chunk may hold several of the best rows
    if kind == "faiss":
        if int(getattr(obj, "d", len(q))) != len(q):
            return []
        D, I = obj.search(q[None, :].astype(np.float32), want)
        return _dedupe(((int(ids[i]), float(d)) for i, d in zip(I[0], D[0]) if i >= 0), top)
    mat = obj
    if mat.shape[1] != len(q):
        return []
    q16 = q.astype(np.float16)
    scores = np.empty(len(ids), dtype=np.float32)
    step = 200_000
    for s in range(0, len(ids), step):
        scores[s:s + step] = mat[s:s + step] @ q16
    idx = np.argpartition(-scores, want - 1)[:want]
    idx = idx[np.argsort(-scores[idx])]
    return _dedupe(((int(ids[i]), float(scores[i])) for i in idx), top)
