"""Phase 4: local dense embeddings (skill S6 step 2).

Model runs LOCALLY via fastembed/ONNX (no remote API, no PyTorch).
Vectors: L2-normalised float16, stored in the `embeddings` table (resumable).

Search index (S7), rebuilt by `export_matrix`:
  * faiss installed  -> `vectors.faiss` (exact IndexFlatIP by default; `embeddings.ann: hnsw`
    for approximate HNSW) + `vector_ids.npy`. Memory-mapped on load.
  * faiss missing    -> `vectors.f16.npy` brute-force matrix + `vector_ids.npy` (pre-0.1.4 path).
Chunks of duplicate documents (exact or near, table `duplicates`) are never embedded (S6).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

from . import safeio

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

MAX_CHARS = 2000          # ~512 tokens: model context limit
_MODEL = None
NOT_DUP = "c.doc_id NOT IN (SELECT doc_id FROM duplicates)"   # S6


def model(cfg):
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(cfg.embeddings.get("model", "BAAI/bge-small-en-v1.5"),
                               cache_dir=str(cfg.agent_root / "models"))
    return _MODEL


def encode(cfg, texts: list[str], batch: int = 64) -> np.ndarray:
    vecs = np.asarray(list(model(cfg).embed([t[:MAX_CHARS] for t in texts], batch_size=batch)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    return vecs


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
    """Embed non-duplicate chunks that have no vector yet. Resumable; commits per batch."""
    name = cfg.embeddings.get("model", "BAAI/bge-small-en-v1.5")
    pending_sql = (f"FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.chunk_id "
                   f"WHERE e.chunk_id IS NULL AND {NOT_DUP}")
    todo = con.execute(f"SELECT count(*) {pending_sql}").fetchone()[0]
    if limit:
        todo = min(todo, limit)
    log(f"embed: {todo} chunks pending (model {name}; duplicate documents skipped)")
    done, t0 = 0, time.time()
    while done < todo:
        rows = con.execute(f"SELECT c.chunk_id, c.text {pending_sql} LIMIT ?",
                           (min(batch, todo - done),)).fetchall()
        if not rows:
            break
        vecs = encode(cfg, [r["text"] or "" for r in rows]).astype(np.float16)
        con.executemany("INSERT OR REPLACE INTO embeddings(chunk_id, model, vector) VALUES (?,?,?)",
                        [(r["chunk_id"], name, v.tobytes()) for r, v in zip(rows, vecs)])
        con.commit()
        done += len(rows)
        if progress:
            progress()
        if done % (batch * 20) < batch:
            log(f"  {done}/{todo} ({done / max(time.time() - t0, 1e-6):.0f} chunks/s)")
    return done


def _iter_vectors(con, batch: int = 50_000):
    """Yield (ids int64[], vecs float32[n, d]) in batches for non-duplicate chunks."""
    cur = con.execute("SELECT e.chunk_id, e.vector FROM embeddings e JOIN chunks c ON c.chunk_id=e.chunk_id "
                      f"WHERE {NOT_DUP} ORDER BY e.chunk_id")
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        vecs = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)
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
    ids_all: list[np.ndarray] = []
    if faiss is not None:
        index, kind = None, None
        for ids, vecs in _iter_vectors(con):
            if index is None:
                index, kind = _new_faiss_index(faiss, vecs.shape[1], cfg)
                if not index.is_trained:
                    index.train(vecs)
            index.add(vecs)
            ids_all.append(ids)
            if progress:
                progress()
        if index is None:
            index, kind = faiss.IndexFlatIP(384), "flat"
        tmp = safeio.faiss_write_at(safeio.check(g, fp.with_suffix(".tmp")), faiss, index)
        ids = np.concatenate(ids_all) if ids_all else np.empty(0, np.int64)
        itmp = safeio.np_save_at(safeio.check(g, ip.with_suffix(".tmp.npy")), ids)
        _replace(tmp, fp)
        _replace(itmp, ip)
        safeio.unlink_at(vp)              # stale brute-force matrix would not match the ids
        backend = f"faiss-{kind}"
    else:
        n = con.execute("SELECT count(*) FROM embeddings e JOIN chunks c ON c.chunk_id=e.chunk_id "
                        f"WHERE {NOT_DUP}").fetchone()[0]
        tmp = safeio.check(g, vp.with_suffix(".tmp.npy"))
        mat, i, ids = None, 0, np.empty(n, dtype=np.int64)
        for bid, vecs in _iter_vectors(con):
            if mat is None:
                mat = safeio.np_open_memmap_at(tmp, np.float16, (n, vecs.shape[1]))
            mat[i:i + len(bid)] = vecs.astype(np.float16)
            ids[i:i + len(bid)] = bid
            i += len(bid)
        if mat is None:
            mat = safeio.np_open_memmap_at(tmp, np.float16, (0, 384))
        mat.flush()
        del mat
        _replace(tmp, vp)
        safeio.np_save_at(ip, ids)
        safeio.unlink_at(fp)
        backend = "numpy-f16"
    rows = int(len(ids))
    store.set_meta(con, "vector_backend", backend)
    store.set_meta(con, "vector_rows", str(rows))
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


def vector_search(cfg, query: str, top: int = 600) -> list[tuple[int, float]]:
    """Return [(chunk_id, cosine)] best matches; [] if no vectors built."""
    got = _load(cfg)
    if not got:
        return []
    kind, obj, ids = got
    if len(ids) == 0:
        return []
    q = encode(cfg, [query])[0]
    top = min(top, len(ids))
    if kind == "faiss":
        D, I = obj.search(q[None, :].astype(np.float32), top)
        return [(int(ids[i]), float(d)) for i, d in zip(I[0], D[0]) if i >= 0]
    mat = obj
    q16 = q.astype(np.float16)
    scores = np.empty(len(ids), dtype=np.float32)
    step = 200_000
    for s in range(0, len(ids), step):
        scores[s:s + step] = mat[s:s + step] @ q16
    idx = np.argpartition(-scores, top - 1)[:top]
    idx = idx[np.argsort(-scores[idx])]
    return [(int(ids[i]), float(scores[i])) for i in idx]
