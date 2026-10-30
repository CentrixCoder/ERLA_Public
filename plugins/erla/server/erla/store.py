"""SQLite index: documents, chunks, FTS5, duplicates, tags, embeddings."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4
# chunks_fts layout. 1: (text, title) with the raw chunk text, title = title + file stem + rel_path.
# 2 (0.7.0): (text, title, path) with the text normalised by fts_text() (sentence-final periods and edge
# hyphens stripped, compound parts appended) and the library-relative path in its own low-weight column.
# A version-1 index stays readable and writable until `fts_upgrade_if_stale` (erla refresh) rebuilds it.
FTS_VERSION = 2

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
    indexed_at TEXT,
    added_at TEXT
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
    text, title, path, content='', contentless_delete=1, tokenize="porter unicode61 tokenchars '-.'"
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_vi USING fts5vocab(chunks_fts, instance);
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
CREATE TABLE IF NOT EXISTS refs (
    doc_id INTEGER NOT NULL,
    std_key TEXT NOT NULL,
    standard_id TEXT,
    n INTEGER,
    PRIMARY KEY (doc_id, std_key)
);
CREATE INDEX IF NOT EXISTS ix_refs_std ON refs(std_key);
CREATE TABLE IF NOT EXISTS ref_docs (
    doc_id INTEGER NOT NULL,
    std_key TEXT NOT NULL,
    label TEXT,
    kind TEXT,
    edition TEXT,
    PRIMARY KEY (doc_id, std_key)
);
CREATE INDEX IF NOT EXISTS ix_ref_docs_key ON ref_docs(std_key);
CREATE TABLE IF NOT EXISTS sparse_vectors (chunk_id INTEGER PRIMARY KEY, model TEXT, indices BLOB, vals BLOB);
"""
# refs (0.7.0, Phase 4.4): standard numbers each document cites in its text (classify.cited_standards),
# keyed by classify.std_key; filled at index time and rebuilt from stored chunks when REFS_VERSION changes.
# sparse_vectors (0.7.0, Phase 2.4, opt-in `sparse.enabled`): learned sparse term weights per chunk.

MIGRATIONS = [("chunks", "quality_json", "ALTER TABLE chunks ADD COLUMN quality_json TEXT"),
              ("documents", "added_at", "ALTER TABLE documents ADD COLUMN added_at TEXT"),
              ("refs", "kind", "ALTER TABLE refs ADD COLUMN kind TEXT"),
              ("refs", "edition", "ALTER TABLE refs ADD COLUMN edition TEXT")]
# added_at: when the document first entered the index; indexed_at moves with every re-read or move.
# Rows from before the column (or written by an older release after a rollback) stay NULL here and
# get an estimate from the indexer (indexer.backfill_added), which can stat the file.


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


def connect_scratch() -> sqlite3.Connection:
    """A private in-memory database (no file is touched), e.g. to tokenize a query with the index's own FTS5
    tokenizer (erla.bm25.tokens)."""
    return sqlite3.connect(":memory:", check_same_thread=False)


def connect_writer(cfg) -> sqlite3.Connection:
    """Writer connection on the configured index, after the G1.2 guard check (erla.safeio)."""
    from . import safeio
    return connect(safeio.parent_at(safeio.check(cfg, cfg.index_path)), write=True)


def has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def at_locator(rows: list[sqlite3.Row], locator: str) -> list[sqlite3.Row]:
    """The chunks a cited locator names: the exact locator, else the locators it begins at a word boundary
    ('p.6' opens 'p.6 part 2' and 'p.6 table 1', never 'p.60'; EPUB 'item 3' opens 'item 3 §Scope', never
    'item 30'). A blank locator names nothing. One rule for library_show, erla show and verify_packet."""
    want = locator.lower().strip()
    if not want:
        return []
    exact = [r for r in rows if str(r["locator"]).lower() == want]
    if exact:
        return exact
    out: list[sqlite3.Row] = []
    for r in rows:
        loc = str(r["locator"]).lower()
        if loc.startswith(want) and (len(loc) == len(want) or not (want[-1].isalnum() and loc[len(want)].isalnum())):
            out.append(r)
    return out


def quality_expr(con: sqlite3.Connection, alias: str = "c") -> str:
    """SELECT expression for chunks.quality_json that also works on a pre-0.2.0 schema."""
    return f"{alias}.quality_json" if has_column(con, "chunks", "quality_json") else "NULL AS quality_json"


def fts_title(title: str | None, path: str, rel_path: str | None) -> str:
    """The version-1 FTS 'title' column (title + file stem + rel_path). Kept for version-1 indexes and
    for callers that only need the title text; writes go through fts_insert / fts_delete."""
    return f"{(title or '')[:400]} {Path(path).stem} {rel_path or ''}"


# ------------------------------------------------------------------ FTS text (version 2)
_FTS_TOKEN = re.compile(r"(?:[^\W_]|[-.])+")      # a token under unicode61 with tokenchars '-.'
_NUMERIC = re.compile(r"[\d.]+")                    # 1.68, 0.1, 3.7.6.0: kept whole, never split
_PARTS = re.compile(r"[-.]+")


def fts_text(text: str | None) -> str:
    """Chunk or title text as stored in a version-2 chunks_fts (Phase 1.1, audit F1).

    The tokenizer keeps '-' and '.' inside tokens so that `7075-T651`, `MMPDS-17` and `3.7.6.0` match
    exactly, but it also glued sentence-final periods and edge hyphens to words (`measured.` never
    matched `measured`) and hid the parts of compounds (`MMPDS` never matched `MMPDS-17`). Here each
    token loses its leading and trailing '.'/'-', and the parts of every non-numeric compound are
    appended on a last line, so a phrase across a compound (`high-strength steel`) still matches inline
    and each part matches alone. The chunk text in `chunks` (citations, locators, verify_packet) is
    never changed: chunks_fts is contentless."""
    if not text:
        return ""
    extra: list[str] = []

    def one(m: re.Match[str]) -> str:
        tok = m.group(0)
        core = tok.strip(".-")
        if not core:
            return " "
        if ("-" in core or "." in core) and not _NUMERIC.fullmatch(core):
            parts = [p for p in _PARTS.split(core) if p]
            if len(parts) > 1:
                extra.extend(parts)
        return core

    body = _FTS_TOKEN.sub(one, text)
    return f"{body}\n{' '.join(extra)}" if extra else body


def fts_version(con: sqlite3.Connection) -> int:
    """2 when chunks_fts has the path column (normalised text), else 1."""
    return 2 if has_column(con, "chunks_fts", "path") else 1


def fts_columns(version: int, text: str | None, title: str | None, path: str, rel_path: str | None) -> tuple:
    """The column values chunks_fts stores for one chunk. A contentless table's legacy 'delete'
    must pass byte-identical values, so insert and delete both call this."""
    if version >= 2:
        return (fts_text(text), fts_text(f"{(title or '')[:400]} {Path(path).stem}"), fts_text(rel_path or ""))
    return (text or "", fts_title(title, path, rel_path))


def fts_insert(con: sqlite3.Connection, rows: list[tuple[int, str | None]], title: str | None, path: str,
               rel_path: str | None, version: int | None = None) -> None:
    """Index (chunk_id, text) rows of one document in chunks_fts."""
    v = version or fts_version(con)
    if v >= 2:
        con.executemany("INSERT INTO chunks_fts(rowid, text, title, path) VALUES (?,?,?,?)",
                        [(cid, *fts_columns(v, t, title, path, rel_path)) for cid, t in rows])
    else:
        con.executemany("INSERT INTO chunks_fts(rowid, text, title) VALUES (?,?,?)",
                        [(cid, *fts_columns(v, t, title, path, rel_path)) for cid, t in rows])


def fts_delete(con: sqlite3.Connection, rows: list[tuple[int, str | None]], title: str | None, path: str,
               rel_path: str | None, version: int | None = None) -> None:
    """Remove (chunk_id, text) rows of one document from chunks_fts."""
    if fts_rowid_delete(con):
        con.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(cid,) for cid, _ in rows])
        return
    v = version or fts_version(con)
    cols = "text, title, path" if v >= 2 else "text, title"
    marks = ",".join("?" * (3 if v >= 2 else 2))
    con.executemany(f"INSERT INTO chunks_fts(chunks_fts, rowid, {cols}) VALUES('delete', ?, {marks})",
                    [(cid, *fts_columns(v, t, title, path, rel_path)) for cid, t in rows])


def delete_document(con: sqlite3.Connection, doc_id: int) -> None:
    """Remove a document's rows from the index (never touches the source file)."""
    rows = con.execute("SELECT chunk_id, text FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
    d = con.execute("SELECT title, path, rel_path FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    fts_delete(con, [(r["chunk_id"], r["text"]) for r in rows], d["title"] if d else "",
               d["path"] if d else "", d["rel_path"] if d else "")
    con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM embeddings WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
    if has_table(con, "sparse_vectors"):
        con.execute("DELETE FROM sparse_vectors WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
    if has_table(con, "refs"):
        con.execute("DELETE FROM refs WHERE doc_id=?", (doc_id,))
    if has_table(con, "ref_docs"):
        con.execute("DELETE FROM ref_docs WHERE doc_id=?", (doc_id,))
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
        note = "" if fts_version(con) >= FTS_VERSION else \
            f"; FTS version {fts_version(con)} (older tokenization: `erla refresh` or `erla fts --rebuild` upgrades it)"
        return True, f"ok ({n_fts} rows, {len(rows)} sampled){note}"
    except sqlite3.DatabaseError as exc:
        return False, str(exc)


def fts_rebuild(con: sqlite3.Connection, log=print, batch: int = 5000) -> int:
    """Rebuild contentless chunks_fts from chunks + documents (index-only write, G1.2), always at the
    current FTS_VERSION. Drops and recreates the virtual table so no corrupted segment survives."""
    con.execute("DROP TABLE IF EXISTS chunks_fts")
    con.executescript(SCHEMA)          # recreates chunks_fts only (other tables exist)
    rows = con.execute(
        "SELECT c.chunk_id, c.text, d.title, d.path, d.rel_path FROM chunks c "
        "JOIN documents d ON d.doc_id = c.doc_id ORDER BY c.chunk_id")
    v = fts_version(con)
    n = 0
    buf = []
    for r in rows:
        buf.append((r["chunk_id"], *fts_columns(v, r["text"], r["title"], r["path"], r["rel_path"])))
        if len(buf) >= batch:
            con.executemany("INSERT INTO chunks_fts(rowid, text, title, path) VALUES (?,?,?,?)", buf)
            n += len(buf)
            buf.clear()
            log(f"  fts rebuild: {n} chunks")
    if buf:
        con.executemany("INSERT INTO chunks_fts(rowid, text, title, path) VALUES (?,?,?,?)", buf)
        n += len(buf)
    con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
    set_meta(con, "fts_version", str(v))
    con.commit()
    return n


def fts_upgrade_if_stale(con: sqlite3.Connection, log=print) -> int:
    """Rebuild chunks_fts when it predates FTS_VERSION (writer connections; index only, no library file
    is read). Returns the number of chunks re-indexed, 0 when already current."""
    if not has_table(con, "chunks_fts") or fts_version(con) >= FTS_VERSION:
        return 0
    log(f"fts: upgrading the full-text index to version {FTS_VERSION} (punctuation and compound words)")
    return fts_rebuild(con, log=log)


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
