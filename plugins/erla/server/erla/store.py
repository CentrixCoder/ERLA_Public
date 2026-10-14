"""SQLite index: documents, chunks, FTS5, duplicates, tags, embeddings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
    doc_id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    rel_path TEXT,
    sha256 TEXT,
    simhash TEXT,
    size INTEGER,
    mtime REAL,
    ext TEXT,
    title TEXT,
    authors TEXT,
    year INTEGER,
    doc_type TEXT,
    discipline TEXT,
    standard_id TEXT,
    revision TEXT,
    authority_tier TEXT,
    classification_flag TEXT,
    top_folder TEXT,
    page_count INTEGER,
    ocr_used INTEGER DEFAULT 0,
    ocr_pending INTEGER DEFAULT 0,
    extract_status TEXT,
    extract_error TEXT,
    meta_json TEXT,
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_doc_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS ix_doc_ext ON documents(ext);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    seq INTEGER,
    locator TEXT,
    text TEXT,
    units_detected TEXT,
    has_table INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_chunk_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, title, content='', contentless_delete=1, tokenize="porter unicode61 tokenchars '-.'"
);
CREATE TABLE IF NOT EXISTS duplicates (
    doc_id INTEGER PRIMARY KEY,
    canonical_doc_id INTEGER,
    kind TEXT
);
CREATE TABLE IF NOT EXISTS tags (doc_id INTEGER, tag TEXT, PRIMARY KEY (doc_id, tag));
CREATE TABLE IF NOT EXISTS embeddings (chunk_id INTEGER PRIMARY KEY, model TEXT, vector BLOB);
CREATE TABLE IF NOT EXISTS extract_cache (
    sha256 TEXT NOT NULL,
    ext TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT,
    payload BLOB,
    created_at TEXT,
    PRIMARY KEY (sha256, ext)
);
CREATE TABLE IF NOT EXISTS properties (
    prop_id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL,
    chunk_id INTEGER,
    locator TEXT,
    quantity TEXT,
    value REAL,
    value_text TEXT,
    units TEXT,
    value_si REAL,
    units_si TEXT,
    material TEXT,
    temper TEXT,
    form TEXT,
    thickness TEXT,
    direction TEXT,
    temperature TEXT,
    basis TEXT,
    row_label TEXT,
    col_label TEXT,
    caption TEXT,
    flags TEXT,
    ocr INTEGER DEFAULT 0,
    extracted_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_prop_q ON properties(quantity, material);
CREATE INDEX IF NOT EXISTS ix_prop_doc ON properties(doc_id);
CREATE TABLE IF NOT EXISTS table_scan (
    doc_id INTEGER PRIMARY KEY,
    sha256 TEXT,
    version TEXT,
    n_tables INTEGER,
    n_props INTEGER,
    scanned_at TEXT
);
"""

MIGRATIONS = [("chunks", "quality_json", "ALTER TABLE chunks ADD COLUMN quality_json TEXT")]


def connect(path: Path | str, write: bool = False) -> sqlite3.Connection:
    """Open the index.

    write=True  (indexer, refresh, tables, embed, cache --seed, fts, MCP index job): creates/migrates
                the schema. Only writers ever change the schema, so a read command can never block
                behind a running refresh or alter an index another process is writing (0.2.0 defect C2).
    write=False (search, show, stats, props, webcheck, eval, MCP read tools): opened read-only
                (`mode=ro`, `PRAGMA query_only`), no DDL. Works on an older schema: readers use
                has_table()/has_column() and degrade (e.g. no quality flags) until a writer migrates.
                A missing index file gives an empty in-memory index instead of creating the file."""
    p = Path(path)
    if write:
        con = sqlite3.connect(str(p), timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            if col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(ddl)
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.commit()   # release the write lock at once; long-lived processes keep connections open
        return con
    if not p.is_file():
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        return con
    con = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def connect_writer(cfg) -> sqlite3.Connection:
    """Writer connection on the configured index, after the G1.2 guard check (erla.safeio)."""
    from . import safeio
    return connect(safeio.parent_at(safeio.check(cfg, cfg.index_path)), write=True)


def has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def quality_expr(con: sqlite3.Connection, alias: str = "c") -> str:
    """SELECT expression for chunks.quality_json that also works on a pre-0.2.0 schema."""
    return f"{alias}.quality_json" if has_column(con, "chunks", "quality_json") else "NULL AS quality_json"


def fts_title(title: str | None, path: str, rel_path: str | None) -> str:
    """Single source of truth for the FTS 'title' column. chunks_fts is contentless (content=''),
    so a later 'delete' must pass byte-identical values; both insert and delete call this."""
    return f"{(title or '')[:400]} {Path(path).stem} {rel_path or ''}"


def delete_document(con: sqlite3.Connection, doc_id: int) -> None:
    """Remove a document's rows from the index (never touches the source file)."""
    rows = con.execute("SELECT chunk_id, text FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
    d = con.execute("SELECT title, path, rel_path FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    t = fts_title(d["title"], d["path"], d["rel_path"]) if d else ""
    if fts_rowid_delete(con):
        con.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(r["chunk_id"],) for r in rows])
    else:
        for r in rows:
            con.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text, title) VALUES('delete', ?, ?, ?)",
                        (r["chunk_id"], r["text"], t))
    con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM embeddings WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
    con.execute("DELETE FROM duplicates WHERE doc_id=? OR canonical_doc_id=?", (doc_id, doc_id))
    con.execute("DELETE FROM tags WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM properties WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM table_scan WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))


def fts_rowid_delete(con: sqlite3.Connection) -> bool:
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()
    return bool(sql and "contentless_delete" in sql[0])


_WORD = __import__("re").compile(r"(?<![\w.\-])[A-Za-z]{5,}(?![\w.\-])")   # whole token under tokenchars '-.'


def fts_check(con: sqlite3.Connection, sample: int = 400) -> tuple[bool, str]:
    """Consistency check of chunks_fts. Returns (ok, message).
    1. FTS5 structural integrity-check.  2. schema supports rowid deletes.
    3. FTS row set == chunks row set.  4. random sample: each chunk is found by a word of its own
    text, and every MATCH query completes without DatabaseError (detects the corruption seen
    2026-10-07 that integrity-check alone misses)."""
    try:
        con.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 0)")
        if not fts_rowid_delete(con):
            return False, "legacy FTS schema (no contentless_delete) - run `erla fts --rebuild`"
        n_fts = con.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        n_ch = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if n_fts != n_ch:
            return False, f"row count mismatch: fts={n_fts} chunks={n_ch}"
        rows = con.execute("SELECT chunk_id, text FROM chunks ORDER BY random() LIMIT ?", (sample,)).fetchall()
        miss = 0
        for cid, text in rows:
            w = _WORD.findall(text or "")
            if not w:
                continue
            hit = con.execute("SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid=?",
                              (f'text:"{w[len(w) // 2]}"', cid)).fetchone()
            miss += hit is None
        if miss:
            return False, f"{miss}/{len(rows)} sampled chunks not findable by their own text"
        return True, f"ok ({n_fts} rows, {len(rows)} sampled)"
    except sqlite3.DatabaseError as exc:
        return False, str(exc)


def fts_rebuild(con: sqlite3.Connection, log=print, batch: int = 5000) -> int:
    """Rebuild contentless chunks_fts from chunks + documents (index-only write, G1.2).
    Drops and recreates the virtual table so no corrupted segment survives."""
    con.execute("DROP TABLE IF EXISTS chunks_fts")
    con.executescript(SCHEMA)          # recreates chunks_fts only (other tables exist)
    rows = con.execute(
        "SELECT c.chunk_id, c.text, d.title, d.path, d.rel_path FROM chunks c "
        "JOIN documents d ON d.doc_id = c.doc_id ORDER BY c.chunk_id")
    n = 0
    buf = []
    for r in rows:
        buf.append((r["chunk_id"], r["text"], fts_title(r["title"], r["path"], r["rel_path"])))
        if len(buf) >= batch:
            con.executemany("INSERT INTO chunks_fts(rowid, text, title) VALUES (?,?,?)", buf)
            n += len(buf)
            buf.clear()
            log(f"  fts rebuild: {n} chunks")
    if buf:
        con.executemany("INSERT INTO chunks_fts(rowid, text, title) VALUES (?,?,?)", buf)
        n += len(buf)
    con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
    con.commit()
    return n


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
