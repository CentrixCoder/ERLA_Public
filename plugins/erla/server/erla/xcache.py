"""S8: content-addressed extraction cache for slow legacy formats (.doc / .ppt via Office COM, .xls).

Stored in the index database (table `extract_cache`, guardrail G1.2: index file only), keyed by
(sha256, ext) and stamped with CACHE_VERSION. A re-index of an unchanged file - including
`erla index --full`, a moved/renamed file, or a rebuilt index - reuses the cached chunks instead of
starting Word/PowerPoint again. Failed extractions are never cached, so they are retried.
Bump CACHE_VERSION whenever a cached extractor's output format changes.
"""

from __future__ import annotations

import json
import zlib
from datetime import datetime, timezone

from .extractors.base import Chunk, ExtractResult

CACHE_VERSION = "legacy-1"
DEFAULT_EXTS = ("doc", "ppt", "xls")
CACHEABLE_STATUS = ("ok", "empty")


def cache_exts(cfg) -> set[str]:
    lo = getattr(cfg, "legacy_office", None) or {}
    if lo.get("cache", True) is False:
        return set()
    return {str(e).lower().lstrip(".") for e in lo.get("cache_exts", DEFAULT_EXTS)}


def _pack(res: ExtractResult) -> bytes:
    body = {"chunks": [[c.locator, c.text, bool(c.has_table), c.quality] for c in res.chunks],
            "meta": res.meta, "status": res.status, "error": res.error}
    return zlib.compress(json.dumps(body, default=str).encode("utf-8"), 6)


def _unpack(blob: bytes) -> ExtractResult:
    body = json.loads(zlib.decompress(blob).decode("utf-8"))
    return ExtractResult(chunks=[Chunk(c[0], c[1], has_table=bool(c[2]), quality=c[3] if len(c) > 3 else None)
                                 for c in body["chunks"]],
                         meta=body.get("meta") or {}, status=body.get("status", "ok"),
                         error=body.get("error") or "")


def get(con, sha: str, ext: str) -> ExtractResult | None:
    row = con.execute("SELECT payload FROM extract_cache WHERE sha256=? AND ext=? AND version=?",
                      (sha, ext, CACHE_VERSION)).fetchone()
    if not row:
        return None
    try:
        return _unpack(row[0])
    except Exception:            # corrupt entry: drop it and re-extract
        con.execute("DELETE FROM extract_cache WHERE sha256=? AND ext=?", (sha, ext))
        return None


def put(con, sha: str, ext: str, res: ExtractResult) -> bool:
    if not sha or res.status not in CACHEABLE_STATUS:
        return False
    con.execute("INSERT OR REPLACE INTO extract_cache(sha256, ext, version, status, payload, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (sha, ext, CACHE_VERSION, res.status, _pack(res),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return True


def seed_from_index(con, exts, log=print) -> int:
    """Populate the cache from documents already in the index (no file is opened, no Office started).
    The placeholder 'file' chunk written for empty documents is not part of the extraction."""
    exts = sorted(exts)
    if not exts:
        return 0
    q = ",".join("?" * len(exts))
    docs = con.execute(
        f"SELECT d.doc_id, d.sha256, d.ext, d.extract_status, d.extract_error, d.meta_json FROM documents d "
        f"LEFT JOIN extract_cache x ON x.sha256=d.sha256 AND x.ext=d.ext AND x.version=? "
        f"WHERE d.ext IN ({q}) AND d.extract_status IN ('ok','empty') AND d.sha256 IS NOT NULL "
        f"AND x.sha256 IS NULL", (CACHE_VERSION, *exts)).fetchall()
    n = 0
    for d in docs:
        rows = con.execute("SELECT locator, text, has_table FROM chunks WHERE doc_id=? ORDER BY seq",
                           (d["doc_id"],)).fetchall()
        chunks = [Chunk(r["locator"], r["text"] or "", bool(r["has_table"])) for r in rows
                  if not (d["extract_status"] == "empty" and r["locator"] == "file")]
        try:
            meta = json.loads(d["meta_json"] or "{}")
        except ValueError:
            meta = {}
        n += put(con, d["sha256"], d["ext"],
                 ExtractResult(chunks=chunks, meta=meta, status=d["extract_status"], error=d["extract_error"] or ""))
        if n % 200 == 0:
            con.commit()
    con.commit()
    log(f"extract cache: seeded {n} entries from the index ({', '.join(exts)})")
    return n


def stats(con) -> dict:
    from .store import has_table
    if not has_table(con, "extract_cache"):
        return {"entries": 0, "mb": 0.0, "version": CACHE_VERSION}
    r = con.execute("SELECT count(*), coalesce(sum(length(payload)),0) FROM extract_cache WHERE version=?",
                    (CACHE_VERSION,)).fetchone()
    return {"entries": r[0], "mb": round(r[1] / 1e6, 1), "version": CACHE_VERSION}
