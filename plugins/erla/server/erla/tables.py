"""A2/A3 backfill: add PDF table chunks and property records to documents indexed before 0.2.0.

Opens library PDFs read-only (G1), only for documents not yet scanned with the current
TABLES_VERSION, only pages whose text looks numeric. Index-only writes; resumable (table_scan).
New chunks get FTS rows now and embeddings on the next `erla embed` / refresh.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import props, store

TABLES_VERSION = "tables-1"


def pending_docs(con, limit: int | None = None) -> list:
    from .store import has_table
    if not has_table(con, "table_scan"):            # pre-0.2.0 index: every eligible PDF is pending
        rows = con.execute("SELECT doc_id, path, sha256, title, rel_path FROM documents WHERE ext='pdf' "
                           "AND extract_status='ok' AND ocr_used=0").fetchall()
        return rows[:limit] if limit else rows
    sql = ("SELECT d.doc_id, d.path, d.sha256, d.title, d.rel_path FROM documents d "
           "LEFT JOIN table_scan t ON t.doc_id = d.doc_id AND t.version = ? AND t.sha256 = d.sha256 "
           "WHERE d.ext='pdf' AND d.extract_status='ok' AND d.ocr_used=0 AND t.doc_id IS NULL "
           "AND d.doc_id NOT IN (SELECT doc_id FROM duplicates) ORDER BY d.authority_tier, d.doc_id")
    rows = con.execute(sql, (TABLES_VERSION,)).fetchall()
    return rows[:limit] if limit else rows


def scan_document(con, row, cfg, max_tables: int = 8) -> tuple[int, int]:
    """Re-read one PDF's numeric pages for tables; replace its table chunks; rebuild its properties."""
    import fitz
    from .extractors.base import clean
    from .extractors.pdf import NUM_ROW, page_tables

    doc_id, path = row["doc_id"], Path(row["path"])
    if not path.is_file():
        return 0, 0
    new_chunks = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc):
            txt = clean(page.get_text("text") or "")
            if not NUM_ROW.search(txt):
                continue
            try:
                label = page.get_label() or ""
            except Exception:
                label = ""
            loc = f"p.{label}" if label else f"p.{i + 1}"
            if label and label != str(i + 1):
                loc += f" (pdf {i + 1})"
            new_chunks += page_tables(page, loc, max_tables)
    old = con.execute("SELECT chunk_id, text FROM chunks WHERE doc_id=? AND has_table=1 AND locator LIKE '% table %' "
                      "AND quality_json LIKE '%\"table\"%'", (doc_id,)).fetchall()
    fv = store.fts_version(con)
    store.fts_delete(con, [(int(o["chunk_id"]), o["text"]) for o in old], row["title"], row["path"], row["rel_path"], fv)
    for o in old:
        con.execute("DELETE FROM embeddings WHERE chunk_id=?", (o["chunk_id"],))
        if store.has_table(con, "sparse_vectors"):
            con.execute("DELETE FROM sparse_vectors WHERE chunk_id=?", (o["chunk_id"],))
        con.execute("DELETE FROM chunks WHERE chunk_id=?", (o["chunk_id"],))
    seq = (con.execute("SELECT max(seq) FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0] or 0) + 1
    added: list[tuple[int, str | None]] = []
    for k, ch in enumerate(new_chunks):
        c = con.execute("INSERT INTO chunks(doc_id, seq, locator, text, units_detected, has_table, quality_json) "
                        "VALUES (?,?,?,?,?,1,?)",
                        (doc_id, seq + k, ch.locator, ch.text, ch.units, json.dumps(ch.quality) if ch.quality else None))
        added.append((int(c.lastrowid or 0), ch.text))
    store.fts_insert(con, added, row["title"], row["path"], row["rel_path"], fv)
    from . import refs                      # table chunks can cite standards too (Phase 4.4)
    refs.record_document(con, doc_id, [r[0] or "" for r in con.execute(
        "SELECT text FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,))], (con.execute("SELECT standard_id FROM documents WHERE doc_id=?", (doc_id,)).fetchone() or [None])[0],
        refs.site_rules(cfg))
    n_props = props.extract_document(con, doc_id)
    con.execute("INSERT OR REPLACE INTO table_scan(doc_id, sha256, version, n_tables, n_props, scanned_at) "
                "VALUES (?,?,?,?,?,?)", (doc_id, row["sha256"], TABLES_VERSION, len(new_chunks), n_props,
                                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return len(new_chunks), n_props


def backfill(con, cfg, limit: int | None = None, log=print, heartbeat=None, max_seconds: float | None = None) -> dict:
    rows = pending_docs(con, limit)
    t0 = time.time()
    st = {"pending": len(rows), "scanned": 0, "tables": 0, "properties": 0, "failed": 0, "failures": []}
    log(f"tables: {len(rows)} PDFs to scan (T1 first)")
    for row in rows:
        try:
            nt, npr = scan_document(con, row, cfg)
            st["tables"] += nt
            st["properties"] += npr
        except Exception as exc:                 # corrupt PDF etc.: record and continue
            st["failed"] += 1
            st["failures"].append(f"{row['rel_path']}: {type(exc).__name__}: {exc}"[:300])
            con.execute("INSERT OR REPLACE INTO table_scan(doc_id, sha256, version, n_tables, n_props, scanned_at) "
                        "VALUES (?,?,?,0,0,?)", (row["doc_id"], row["sha256"], TABLES_VERSION,
                                                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
        st["scanned"] += 1
        con.commit()
        if heartbeat:
            heartbeat()
        if st["scanned"] % 50 == 0:
            log(f"  {st['scanned']}/{len(rows)} PDFs, {st['tables']} tables, {st['properties']} property records")
        if max_seconds and time.time() - t0 > max_seconds:
            log("  time budget reached; resume with the same command")
            break
    # documents without PDF tables (docx/xlsx/csv/pptx) already hold markdown tables: build their properties
    st["office_docs"] = 0
    for (did,) in con.execute("SELECT DISTINCT c.doc_id FROM chunks c JOIN documents d ON d.doc_id=c.doc_id "
                              "WHERE c.has_table=1 AND d.ext != 'pdf' AND d.extract_status='ok' "
                              "AND c.doc_id NOT IN (SELECT doc_id FROM properties)").fetchall():
        st["properties"] += props.extract_document(con, did)
        st["office_docs"] += 1
    con.commit()
    st["seconds"] = round(time.time() - t0, 1)
    st["failures"] = st["failures"][:30]
    return st
