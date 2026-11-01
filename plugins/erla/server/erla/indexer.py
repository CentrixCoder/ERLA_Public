"""Incremental library indexer (skill S1).

Read-only with respect to the library (G1): files are only opened for reading;
all writes go to the index database and logs under the agent root.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import props, refs, safeio, store, tables, xcache
from .classify import (classify_document, copy_group_flags, detect_markings, detect_standard, guess_year,
                       most_restrictive)
from .config import Config
from .dedupe import near_duplicate_pairs, sha256_file, simhash
from .extractors import COM_EXTS, EXTRACTORS, ExtractResult
from .extractors.legacy import ComWorker

SKIP_DIRS = {"__pycache__", ".git", ".svn", "$recycle.bin", "system volume information"}


@dataclass
class IndexStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    duplicates: int = 0
    near_duplicates: int = 0
    ocr_used: int = 0
    ocr_pending: int = 0
    empty: int = 0
    failed: int = 0
    skipped_large: int = 0
    cache_hits: int = 0        # S8: legacy files served from the extract cache
    moved: int = 0             # files renamed/moved: index record relocated, no re-extraction
    repaired: int = 0          # orphaned duplicate records (canonical gone) re-processed
    copy_flags: int = 0        # G2.5: copies given their copy group's flag (0.2.18)
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["failures"] = self.failures[:50]
        return d


def _real(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def iter_library(cfg: Config, errors: list[str] | None = None):
    """Yield (abs_path, rel_path, root) for every indexable file, each file once.

    Folders that cannot be listed are appended to ``errors`` (when given): a caller that removes index
    records for files it did not see must not remove those under an unreadable folder (a share that dropped
    for a moment must not delete its OCR text and vectors). A file under two configured roots (one root
    inside another) is yielded once, under the first root. A junction (which os.walk follows on Windows) is
    followed, but no real folder is walked twice: a junction back to its own root looped (correctness audit 2026-10-31: 128 paths for 2
    files)."""
    seen: set[str] = set()
    walked: set[str] = set()

    def failed(exc: OSError) -> None:
        if errors is not None:
            errors.append(str(exc.filename or exc))

    for root in cfg.library_roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=failed):
            here = _real(dirpath)
            if here in walked:
                dirnames[:] = []
                continue
            walked.add(here)
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")
                           and _real(os.path.join(dirpath, d)) not in walked]
            for fn in filenames:
                p = Path(dirpath) / fn
                if not cfg.is_included(p):
                    continue
                key = os.path.normcase(str(p))
                if key in seen:
                    continue
                seen.add(key)
                yield p, p.relative_to(root).as_posix(), root


def _storable(p: Path) -> bool:
    """A file name the index can store: a lone surrogate (an invalid UTF-16 name on Windows) cannot be
    written as UTF-8 and aborted the whole run (correctness audit 2026-10-31)."""
    try:
        str(p).encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def orphan_ids(con) -> set[int]:
    """'duplicate' records whose original is gone: they hold no text, so their content is not searchable
    until they are read again."""
    live = {r[0] for r in con.execute("SELECT doc_id FROM documents")}
    valid = {d for d, c in con.execute("SELECT doc_id, canonical_doc_id FROM duplicates") if c in live and d in live}
    return {r[0] for r in con.execute("SELECT doc_id FROM documents WHERE extract_status='duplicate'")
            if r[0] not in valid}


XVER_SQL = "CASE WHEN json_valid(meta_json) THEN json_extract(meta_json, '$.extractor') END"


def outdated_extract(ext: str, stamp: str | None, status: str | None) -> bool:
    """True when a document was read by an older version of its extractor (one that sets ``version``).
    Duplicates are never extracted themselves and failed files are retried anyway."""
    want = getattr(EXTRACTORS.get(ext), "version", "")
    return bool(want) and stamp != want and status not in ("duplicate", "failed")


def probe_library(cfg: Config, con) -> dict:
    """Change probe (P2): stat-only walk of the library vs the index. Never opens file contents.

    Stale when: no recorded full index, age > stale_days, a file is on disk but not in the index,
    an indexed file changed (size/mtime), an indexed file is gone, or the newest library mtime is
    later than the last full index.
    """
    last = store.get_meta(con, "last_index_at")
    last_ts = None
    if last:
        last_ts = datetime.fromisoformat(last)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
    indexed = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM documents")}
    outdated = sum(1 for ext, stamp, status in con.execute(
        f"SELECT ext, {XVER_SQL}, extract_status FROM documents") if outdated_extract(ext or "", stamp, status))
    on_disk = new = changed = large = 0
    newest_m, newest_p = 0.0, None
    seen: set[str] = set()
    max_b = cfg.max_file_mb * 1024 * 1024
    for p, rel, _root in iter_library(cfg):
        if not _storable(p):
            continue
        try:
            s = p.stat()
        except OSError:
            continue
        if s.st_size > max_b:
            large += 1
            continue
        on_disk += 1
        sp = str(p)
        seen.add(sp)
        if s.st_mtime > newest_m:
            newest_m, newest_p = s.st_mtime, rel
        old = indexed.get(sp)
        if old is None:
            new += 1
        elif old[0] != s.st_size or abs((old[1] or 0) - s.st_mtime) >= 1e-3:
            changed += 1
    missing = sum(1 for k in indexed if k not in seen)
    newest_iso = datetime.fromtimestamp(newest_m, timezone.utc).isoformat(timespec="seconds") if newest_m else None
    age = (datetime.now(timezone.utc) - last_ts).days if last_ts else None
    reasons = []
    if last_ts is None:
        reasons.append("no completed full index recorded")
    elif age > cfg.stale_days:
        reasons.append(f"index {age} d old > stale_days {cfg.stale_days}")
    if new:
        reasons.append(f"{new} new file(s) not in index")
    if changed:
        reasons.append(f"{changed} indexed file(s) changed")
    if missing:
        reasons.append(f"{missing} indexed file(s) no longer on disk")
    if outdated:
        reasons.append(f"{outdated} indexed file(s) read by an older extractor")
    orphaned = len(orphan_ids(con))
    if orphaned:
        reasons.append(f"{orphaned} duplicate record(s) without their original (text not searchable)")
    if last_ts and newest_m and newest_m > last_ts.timestamp():
        reasons.append("library modified after last full index")
    return {"last_index_at": last, "age_days": age, "stale_days": cfg.stale_days,
            "files_on_disk": on_disk, "documents_indexed": len(indexed), "new": new, "changed": changed,
            "missing": missing, "outdated_extract": outdated, "skipped_large": large, "newest_mtime": newest_iso, "newest_file": newest_p,
            "stale": bool(reasons), "reasons": reasons}


def _created(path: str) -> float | None:
    """When the file arrived on this disk (stat only, never opened): its creation time where the OS
    records one, else its modification time."""
    try:
        s = os.stat(path)
    except OSError:
        return None
    born = getattr(s, "st_birthtime", None)
    if born is None:
        born = s.st_ctime if os.name == "nt" else s.st_mtime   # Windows ctime is creation; Linux has none
    return born or None


def estimated_added(path: str, indexed_at: str | None) -> str | None:
    """added_at for a record from before the column (or from an older release after a rollback): the
    file's creation time, never later than indexed_at. indexed_at alone would date every document to
    the last full re-read."""
    born = _created(path)
    if born is None:
        return indexed_at
    iso = datetime.fromtimestamp(born, timezone.utc).isoformat(timespec="seconds")
    return min(iso, indexed_at) if indexed_at else iso


def backfill_added(con) -> int:
    """Give every record without added_at its estimate (writer connections only)."""
    rows = con.execute("SELECT doc_id, path, indexed_at FROM documents WHERE added_at IS NULL").fetchall()
    con.executemany("UPDATE documents SET added_at=? WHERE doc_id=?",
                    [(estimated_added(r["path"], r["indexed_at"]), r["doc_id"]) for r in rows])
    con.commit()
    return len(rows)


def _extract_job(path_str: str, ext: str, cfg: Config, ocr_now: bool, progress=None) -> ExtractResult:
    """Top-level so it can run in a worker process."""
    cls = EXTRACTORS[ext]
    try:
        if ext == "pdf":
            return cls(cfg, ocr_now=ocr_now, progress=progress).extract(Path(path_str))
        return cls(cfg).extract(Path(path_str))
    except Exception as exc:  # never let one file kill the run
        return ExtractResult(status="failed", error=f"{type(exc).__name__}: {exc}")


def doc_title(meta: dict, path: Path) -> str:
    """The title a document is written with (documents.title is its first 400 characters)."""
    return str(meta.get("title") or meta.get("title_guess") or path.stem)


@dataclass
class Prepared:
    """The pure-CPU part of writing one document, computed from its ExtractResult alone (S11, L12).

    The writer used to compute all of it in the single main process (simhash, chunk units, FTS text
    normalisation, reference scan), so extra workers bought nothing. Pool workers now compute it beside
    extraction; every other path (one worker, cache hits, Office COM, deferred OCR) computes it in the
    writer with the same function, so every path writes the same rows."""
    fts_version: int
    simhash: str | None
    units: list[str]
    fts_bodies: list[str]
    refs: dict[str, dict] | None           # None: not scanned (the index has no refs table)

    def fits(self, res: ExtractResult, fts_version: int, with_refs: bool) -> bool:
        """True when this was prepared for `res` under the writer's current index layout."""
        n = len(res.chunks)
        return (self.fts_version == fts_version and len(self.units) == n and len(self.fts_bodies) == n
                and (self.refs is not None or not with_refs))


def prepare(res: ExtractResult, fts_version: int, ref_rules: dict, with_refs: bool = True) -> Prepared:
    """Prepared for one extracted document. Pure: reads nothing but its arguments (worker-safe)."""
    texts = [ch.text for ch in res.chunks]
    return Prepared(fts_version=fts_version,
                    simhash=simhash(res.text) if res.chunks else None,
                    units=[ch.units for ch in res.chunks],
                    fts_bodies=[store.fts_body(fts_version, t) for t in texts],
                    refs=refs.extract(texts, ref_rules) if with_refs else None)


def _extract_prepare_job(path_str: str, ext: str, cfg: Config, ocr_now: bool, fts_version: int,
                         ref_rules: dict, with_refs: bool = True) -> tuple[ExtractResult, Prepared | None]:
    """Worker job: extract one file, then prepare its rows. A failed prepare returns None, so the writer
    computes it (and meets the same error there, as before S11)."""
    res = _extract_job(path_str, ext, cfg, ocr_now)
    try:
        return res, prepare(res, fts_version, ref_rules, with_refs)
    except Exception:
        return res, None


class Indexer:
    def __init__(self, cfg: Config, log=print, heartbeat=None):
        self.cfg = cfg
        self.log = log
        self.heartbeat = heartbeat or (lambda: None)   # S5: progress signal for the refresh lock
        self.con = store.connect_writer(cfg)
        self._com: dict[str, ComWorker] = {}
        self._ref_rules = refs.site_rules(cfg)          # citation patterns, read once per run

    # ------------------------------------------------------------ writing
    def _write(self, path: Path, rel: str, size: int, mtime: float, sha: str,
               res: ExtractResult, doc_id: int | None, prep: Prepared | None = None) -> int:
        con = self.con
        fv = store.fts_version(con)
        with_refs = store.has_table(con, "refs")
        if prep is None or not prep.fits(res, fv, with_refs):
            prep = prepare(res, fv, self._ref_rules, with_refs)
        copies: list[int] = []
        added = self._added_at(doc_id)
        if doc_id is not None:
            old = con.execute("SELECT sha256 FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if old is not None and old["sha256"] == sha:
                # same bytes re-extracted (full re-index, OCR pass): its exact copies stay linked to it,
                # so the copy group and its flag (G2.5) survive the rewrite (0.2.18)
                copies = [r[0] for r in con.execute(
                    "SELECT doc_id FROM duplicates WHERE canonical_doc_id=? AND kind='exact'", (doc_id,))]
            store.delete_document(con, doc_id)
        ext = path.suffix.lower().lstrip(".")
        meta = dict(res.meta or {})
        if getattr(EXTRACTORS.get(ext), "version", ""):
            # a read that skipped Office for this run only stays outdated, so the next run reads it again
            meta["extractor"] = EXTRACTORS[ext].version + ("+pending" if meta.get("com_skipped") else "")
        title = doc_title(meta, path)
        head = "\n".join(c.text for c in res.chunks[:2])
        tail = "\n".join(c.text for c in res.chunks[-2:]) if len(res.chunks) > 2 else ""
        flag = self.cfg.path_flag(rel) or detect_markings(head, tail)
        std, rev = detect_standard(f"{path.name}\n{title}\n{head[:3000]}")
        year = meta.get("year") or guess_year(path.name, title, head[:2000])
        cls = classify_document(rel, ext, title, head)
        sh = prep.simhash
        cur = con.execute(
            """INSERT INTO documents(path, rel_path, sha256, simhash, size, mtime, ext, title, authors, year,
                   doc_type, discipline, standard_id, revision, authority_tier, classification_flag, top_folder,
                   page_count, ocr_used, ocr_pending, extract_status, extract_error, meta_json, indexed_at, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(path), rel, sha, sh, size, mtime, ext, str(title)[:400], meta.get("authors"), year,
             cls["doc_type"], cls["discipline"], std, rev, cls["authority_tier"], flag, cls["top_folder"],
             meta.get("page_count") or meta.get("slide_count"), int(res.ocr_used), int(res.ocr_pending),
             res.status, res.error[:500] if res.error else None, json.dumps(meta, default=str)[:20000],
             datetime.now(timezone.utc).isoformat(timespec="seconds"), added))
        new_id = cur.lastrowid
        con.executemany("INSERT OR REPLACE INTO duplicates(doc_id, canonical_doc_id, kind) VALUES (?,?,'exact')",
                        [(c, new_id) for c in copies])
        fts_rows: list[tuple[int, str | None]] = []
        for seq, ch in enumerate(res.chunks):
            c = con.execute("INSERT INTO chunks(doc_id, seq, locator, text, units_detected, has_table, quality_json) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (new_id, seq, ch.locator, ch.text, prep.units[seq], int(ch.has_table),
                             json.dumps(ch.quality) if ch.quality else None))
            fts_rows.append((int(c.lastrowid or 0), ch.text))
        store.fts_insert(con, fts_rows, str(title), str(path), rel, fv, bodies=prep.fts_bodies)
        refs.record_document(con, int(new_id or 0), [ch.text for ch in res.chunks], std,   # Phase 4.4
                             self._ref_rules, found=prep.refs)
        if any(ch.has_table and "|" in ch.text for ch in res.chunks):
            props.extract_document(con, new_id)                       # A3
        if ext == "pdf" and res.status == "ok":
            con.execute("INSERT OR REPLACE INTO table_scan(doc_id, sha256, version, n_tables, n_props, scanned_at) "
                        "VALUES (?,?,?,?,(SELECT count(*) FROM properties WHERE doc_id=?),?)",
                        (new_id, sha, tables.TABLES_VERSION, int(meta.get("tables") or 0), new_id,
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
        if not res.chunks:
            # keep the document findable by name/path even with no extractable text
            c = con.execute("INSERT INTO chunks(doc_id, seq, locator, text) VALUES (?,?,?,?)",
                            (new_id, 0, "file", f"{title}\n{rel}"))
            store.fts_insert(con, [(int(c.lastrowid or 0), f"{title}\n{rel}")], str(title), str(path), rel, fv)
        return new_id

    def _added_at(self, doc_id: int | None) -> str:
        """When the document first entered the index: kept from the record a re-read replaces, else now."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if doc_id is not None:
            row = self.con.execute("SELECT added_at, path, indexed_at FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if row is not None:
                return row["added_at"] or estimated_added(row["path"], row["indexed_at"]) or now
        return now

    def _write_duplicate(self, path: Path, rel: str, size: int, mtime: float, sha: str,
                         canonical: int, doc_id: int | None) -> None:
        added = self._added_at(doc_id)
        if doc_id is not None:
            store.delete_document(self.con, doc_id)
        # G2.5: a copy of a flagged document is flagged too (its own folder rule first), so its file
        # name is protected by the web gate and labelled wherever it is listed (0.2.16)
        row = self.con.execute("SELECT classification_flag FROM documents WHERE doc_id=?", (canonical,)).fetchone()
        flag = self.cfg.path_flag(rel) or (row[0] if row else None)
        cur = self.con.execute(
            """INSERT INTO documents(path, rel_path, sha256, size, mtime, ext, title, classification_flag,
                                     extract_status, indexed_at, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(path), rel, sha, size, mtime, path.suffix.lower().lstrip("."), path.stem, flag, "duplicate",
             datetime.now(timezone.utc).isoformat(timespec="seconds"), added))
        self.con.execute("INSERT OR REPLACE INTO duplicates(doc_id, canonical_doc_id, kind) VALUES (?,?,?)",
                         (cur.lastrowid, canonical, "exact"))

    def _relocate(self, doc_id: int, path: Path, rel: str, size: int, mtime: float) -> None:
        """Point an existing record at a file's new location without re-extracting it.

        Path-derived fields (classification flag, tier, type, discipline, top folder) are recomputed
        for the new location; the FTS title (which contains the path) is rewritten. A classification
        flag is never dropped by a move (G2.5: a controlled document moved out of a controlled folder
        stays flagged until it is re-extracted)."""
        con = self.con
        d = con.execute("SELECT title, path, rel_path, ext, classification_flag FROM documents WHERE doc_id=?",
                        (doc_id,)).fetchone()
        chunks = con.execute("SELECT chunk_id, text FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
        texts = [c["text"] or "" for c in chunks]
        head = "\n".join(texts[:2])
        tail = "\n".join(texts[-2:]) if len(texts) > 2 else ""
        flag = self.cfg.path_flag(rel) or detect_markings(head, tail) or d["classification_flag"]
        cls = classify_document(rel, d["ext"] or path.suffix.lower().lstrip("."), d["title"] or path.stem, head)
        rows = [(int(c["chunk_id"]), c["text"]) for c in chunks]
        fv = store.fts_version(con)
        store.fts_delete(con, rows, d["title"], d["path"], d["rel_path"], fv)
        store.fts_insert(con, rows, d["title"], str(path), rel, fv)
        con.execute(
            "UPDATE documents SET path=?, rel_path=?, size=?, mtime=?, classification_flag=?, doc_type=?, "
            "discipline=?, authority_tier=?, top_folder=?, indexed_at=? WHERE doc_id=?",
            (str(path), rel, size, mtime, flag, cls["doc_type"], cls["discipline"], cls["authority_tier"],
             cls["top_folder"], datetime.now(timezone.utc).isoformat(timespec="seconds"), doc_id))

    def _com_extract(self, path: Path, ext: str) -> ExtractResult:
        kind = "word" if ext == "doc" else "powerpoint"
        if kind not in self._com:
            self._com[kind] = ComWorker(kind, float(self.cfg.legacy_office.get("per_file_timeout_s", 120)))
        cls = EXTRACTORS[ext]
        dead = getattr(self, "_com_dead", None)
        if dead is None:
            dead = self._com_dead = {}
        # circuit breaker: after Office fails to start or times out 3x in a row, stop using COM for
        # this run (native parser still runs); failed files are retried on the next run.
        worker = False if dead.get(kind, 0) >= 3 else self._com[kind]
        try:
            res = cls(self.cfg, worker=worker).extract(path)
        except Exception as exc:
            dead[kind] = 99                     # Office cannot start: native reads are final
            self._com[kind].kill()
            self.log(f"  COM {kind} unavailable for this run: {exc}")
            res = cls(self.cfg, worker=False).extract(path)
        if worker is False and dead.get(kind, 0) == 3:
            # skipped only because this run's timeouts tripped the breaker: the next run gives it to Office
            res.meta = {**(res.meta or {}), "com_skipped": True}
        elif "timeout" in (res.error or "") + str((res.meta or {}).get("com_error", "")):
            dead[kind] = min(dead.get(kind, 0) + 1, 3)
        elif res.status != "failed" and not (res.meta or {}).get("com_error") and dead.get(kind, 0) < 3:
            dead[kind] = 0                      # consecutive-failure counter resets on success
        return res

    # -------------------------------------------------------------- run
    def run(self, full: bool = False, limit: int | None = None, subpath: str | None = None,
            ocr: bool = False) -> IndexStats:
        t0 = time.time()
        st = IndexStats()
        con = self.con
        backfill_added(con)
        existing = {r["path"]: r for r in con.execute(
            f"SELECT doc_id, path, size, mtime, sha256, extract_status, {XVER_SQL} AS xver FROM documents")}
        by_sha = {r["sha256"]: r["doc_id"] for r in existing.values()
                  if r["sha256"] and r["extract_status"] != "duplicate"}
        seen_paths: set[str] = set()
        todo: list[tuple[Path, str, int, float, str, int | None]] = []
        dup_items: list[tuple[Path, str, int, float, str, int, int | None]] = []
        # Orphans: 'duplicate' records whose canonical no longer exists (the pre-0.1.5 move bug; until the
        # 2026-10-31 audit also an edited original, or two copies that swapped names)
        orphans = orphan_ids(con)
        unreadable: list[str] = []

        legacy_on = bool(self.cfg.legacy_office.get("enabled", True))
        for p, rel, _root in iter_library(self.cfg, unreadable):
            if subpath and not rel.lower().startswith(subpath.lower().strip("/\\").replace("\\", "/")):
                seen_paths.add(str(p))
                continue
            st.seen += 1
            seen_paths.add(str(p))
            if not _storable(p):
                st.failed += 1
                st.failures.append(f"{ascii(rel)}: the file name cannot be stored (invalid characters): rename it")
                continue
            try:
                s = p.stat()
            except OSError as exc:
                st.failed += 1
                st.failures.append(f"{rel}: stat {exc}")
                continue
            if s.st_size > self.cfg.max_file_mb * 1024 * 1024:
                st.skipped_large += 1
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext in COM_EXTS and not legacy_on:
                continue
            old = existing.get(str(p))
            orphan = old is not None and old["doc_id"] in orphans
            if orphan:
                st.repaired += 1
            stale_x = bool(old) and outdated_extract(ext, old["xver"], old["extract_status"])
            if (old and not full and not orphan and not stale_x and old["extract_status"] != "failed"
                    and old["size"] == s.st_size and abs((old["mtime"] or 0) - s.st_mtime) < 1e-3):
                st.unchanged += 1
                continue
            try:
                sha = sha256_file(p)
            except OSError as exc:
                st.failed += 1
                st.failures.append(f"{rel}: read {exc}")
                continue
            if old and old["sha256"] == sha and not full and not orphan and not stale_x:
                con.execute("UPDATE documents SET mtime=?, size=? WHERE doc_id=?", (s.st_mtime, s.st_size, old["doc_id"]))
                st.unchanged += 1
                continue
            canon = by_sha.get(sha)
            if canon is not None and (not old or canon != old["doc_id"]):
                # decided after the scan: if the canonical's own file is gone this is a move, not a copy
                dup_items.append((p, rel, s.st_size, s.st_mtime, sha, canon, old["doc_id"] if old else None))
                continue
            todo.append((p, rel, s.st_size, s.st_mtime, sha, old["doc_id"] if old else None))
            if limit and len(todo) >= limit:
                break

        # Move/rename: a "duplicate" whose canonical's file is no longer on disk is the same file at a new
        # path. Relocate the canonical record (keeps its text, OCR and vectors) instead of recording a
        # duplicate of a record that the removal pass below would delete. Needs a complete scan.
        missing = ({row["doc_id"] for path, row in existing.items() if path not in seen_paths}
                   if not limit else set())
        relocated: set[int] = set()
        for p, rel, size, mtime, sha, canon, old_id in dup_items:
            if canon in missing and canon not in relocated:
                if old_id is not None:
                    store.delete_document(con, old_id)
                self._relocate(canon, p, rel, size, mtime)
                relocated.add(canon)
                st.moved += 1
            else:
                self._write_duplicate(p, rel, size, mtime, sha, canon, old_id)
                st.duplicates += 1
        con.commit()
        self.log(f"scan: {st.seen} files, {len(todo)} to extract, {st.unchanged} unchanged, "
                 f"{st.duplicates} duplicates, {st.moved} moved, {st.repaired} orphaned records repaired")

        done = 0
        cexts = xcache.cache_exts(self.cfg)

        def handle(item, res: ExtractResult, cached: bool = False, prep: Prepared | None = None):
            nonlocal done
            p, rel, size, mtime, sha, doc_id = item
            ext = p.suffix.lower().lstrip(".")
            # one file's rows in a savepoint: an error writing it (a name or text SQLite cannot take) rolls back
            # that file only, is reported, and the run goes on (correctness audit 2026-10-31: one bad file
            # aborted every run, so no file after it was ever indexed)
            con.execute("SAVEPOINT erla_file")
            try:
                if not cached and ext in cexts:
                    xcache.put(con, sha, ext, res)
                if sha in by_sha and by_sha[sha] != doc_id:      # duplicate found within this run
                    self._write_duplicate(p, rel, size, mtime, sha, by_sha[sha], doc_id)
                    st.duplicates += 1
                else:
                    new_id = self._write(p, rel, size, mtime, sha, res, doc_id, prep)
                    by_sha[sha] = new_id
                    if doc_id is None:
                        st.new += 1
                    else:
                        st.updated += 1
                    st.ocr_used += int(res.ocr_used)
                    st.ocr_pending += int(res.ocr_pending)
                    if res.status == "empty":
                        st.empty += 1
                    elif res.status == "failed":
                        st.failed += 1
                        st.failures.append(f"{rel}: {res.error}")
                con.execute("RELEASE erla_file")
            except Exception as exc:
                con.execute("ROLLBACK TO erla_file")
                con.execute("RELEASE erla_file")
                st.failed += 1
                st.failures.append(f"{ascii(rel)}: write {type(exc).__name__}: {exc}")
            done += 1
            self.heartbeat()
            if done % 25 == 0:
                con.commit()
            if done % 100 == 0:
                rate = done / max(time.time() - t0, 1e-6)
                self.log(f"  {done}/{len(todo)} extracted ({rate:.1f} files/s)")

        if cexts:                                  # S8: serve unchanged legacy content from the cache
            rest = []
            for item in todo:
                ext = item[0].suffix.lower().lstrip(".")
                hit = xcache.get(con, item[4], ext) if ext in cexts else None
                if hit is None:
                    rest.append(item)
                else:
                    st.cache_hits += 1
                    handle(item, hit, cached=True)
            if st.cache_hits:
                self.log(f"  extract cache: {st.cache_hits} legacy files reused without re-extraction")
            todo = rest
        com_items, pool_items = [], []             # one pass; was O(n^2) list membership
        for t in todo:
            (com_items if t[0].suffix.lower().lstrip(".") in COM_EXTS else pool_items).append(t)
        workers = max(1, int(self.cfg.workers))
        if pool_items:
            if workers == 1:
                for item in pool_items:
                    handle(item, _extract_job(str(item[0]), item[0].suffix.lower().lstrip("."), self.cfg, ocr))
            else:
                # S11: the workers also prepare each document's rows (simhash, units, FTS text, references),
                # so the writer is left with the SQL
                fv, with_refs = store.fts_version(con), store.has_table(con, "refs")
                with ProcessPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(_extract_prepare_job, str(it[0]), it[0].suffix.lower().lstrip("."), self.cfg,
                                      ocr, fv, self._ref_rules, with_refs): it
                            for it in pool_items}
                    for fut in as_completed(futs):
                        item = futs.pop(fut)        # frees each result once written, not at the end of the run
                        prep: Prepared | None = None
                        try:
                            res, prep = fut.result()
                        except Exception as exc:
                            res = ExtractResult(status="failed", error=f"worker: {exc}")
                        handle(item, res, prep=prep)
        for item in com_items:
            handle(item, self._com_extract(item[0], item[0].suffix.lower().lstrip(".")))
        # ------------------------------------------------ copies left without their original
        # An original re-read with new content, or turned into a copy of another file (two files that swapped
        # names), takes its exact copies' link with it: their records hold no text. Read them again in this
        # run, so the probe never reports an index CURRENT whose copies cannot be found (audit 2026-10-31).
        repair: list[tuple[Path, str, int, float, str, int | None]] = []
        for did in sorted(orphan_ids(con)):
            r = con.execute("SELECT path, rel_path, size, mtime, sha256 FROM documents WHERE doc_id=?", (did,)).fetchone()
            if r is not None and r["path"] in seen_paths and Path(r["path"]).is_file():
                repair.append((Path(r["path"]), r["rel_path"], r["size"], r["mtime"], r["sha256"], did))
        if repair:
            by_sha.clear()
            by_sha.update({r[0]: r[1] for r in con.execute(
                "SELECT sha256, doc_id FROM documents WHERE sha256 IS NOT NULL AND extract_status != 'duplicate'")})
            for item in repair:
                ext = item[0].suffix.lower().lstrip(".")
                handle(item, self._com_extract(item[0], ext) if ext in COM_EXTS
                       else _extract_job(str(item[0]), ext, self.cfg, ocr))
                st.repaired += 1
            self.log(f"  {len(repair)} copies read again: their original changed in this run")
        for w in self._com.values():
            w.close()
        self._com.clear()
        con.commit()

        # ------------------------------------------------ removed files
        if unreadable:
            st.failed += len(unreadable)
            st.failures += [f"{u}: folder could not be listed: its index records are kept" for u in unreadable[:20]]
            self.log(f"  {len(unreadable)} folder(s) could not be listed; their records are kept, not removed")
        blocked = [os.path.normcase(os.path.join(u, "")) for u in unreadable]
        if not limit:
            for path, row in existing.items():
                did = row["doc_id"]
                if path in seen_paths or did in relocated:
                    continue
                if any(os.path.normcase(path).startswith(b) for b in blocked):
                    continue                    # not seen because its folder could not be listed: not deleted
                # Original deleted but an exact copy survives: promote the copy into the canonical record
                # so the content stays searchable (the copy's own record holds no text).
                heir = next((h for h in con.execute(
                    "SELECT d.doc_id, d.path, d.rel_path, d.size, d.mtime FROM duplicates x "
                    "JOIN documents d ON d.doc_id = x.doc_id WHERE x.canonical_doc_id=? AND x.kind='exact' "
                    "ORDER BY length(d.path), d.path", (did,)) if h["path"] in seen_paths), None)
                if heir is not None:
                    store.delete_document(con, heir["doc_id"])
                    self._relocate(did, Path(heir["path"]), heir["rel_path"], heir["size"], heir["mtime"])
                    st.moved += 1
                    continue
                store.delete_document(con, did)
                st.removed += 1
            con.commit()

        st.near_duplicates = self.mark_near_duplicates()
        st.copy_flags = self.reconcile_copy_flags()
        # P4: last_index_at certifies a COMPLETE scan of every root; partial runs (--limit/--path)
        # are recorded separately so they can never make a partly indexed library look fresh.
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if limit or subpath or unreadable:             # a folder not listed: the scan was not complete
            store.set_meta(con, "last_partial_index_at", now)
        else:
            store.set_meta(con, "last_index_at", now)
            store.set_meta(con, "last_index_files", str(st.seen))
        con.commit()
        st.seconds = round(time.time() - t0, 1)
        self._log_run(st)
        return st

    def run_ocr(self, limit: int | None = None) -> IndexStats:
        """Process documents whose OCR was deferred (scanned PDFs)."""
        t0 = time.time()
        st = IndexStats()
        rows = self.con.execute("SELECT doc_id, path, rel_path, size, mtime, sha256 FROM documents "
                                "WHERE ocr_pending=1 ORDER BY size").fetchall()
        if limit:
            rows = rows[:limit]
        self.log(f"ocr: {len(rows)} scanned documents pending")
        for i, r in enumerate(rows, 1):
            p = Path(r["path"])
            if not p.is_file():
                continue
            res = _extract_job(str(p), "pdf", self.cfg, True, progress=self.heartbeat)
            self.heartbeat()
            self._write(p, r["rel_path"], r["size"], r["mtime"], r["sha256"], res, r["doc_id"])
            st.updated += 1
            st.ocr_used += int(res.ocr_used)
            if res.status == "failed":
                st.failed += 1
                st.failures.append(f"{r['rel_path']}: {res.error}")
            self.con.commit()
            self.log(f"  ocr {i}/{len(rows)}: {r['rel_path']} ({len(res.chunks)} chunks)")
        st.copy_flags = self.reconcile_copy_flags()      # OCR text can carry a marking (G2.5)
        st.seconds = round(time.time() - t0, 1)
        self._log_run(st, kind="ocr")
        return st

    def mark_near_duplicates(self) -> int:
        con = self.con
        con.execute("DELETE FROM duplicates WHERE kind='near'")
        items = [(r["doc_id"], r["simhash"]) for r in con.execute(
            "SELECT doc_id, simhash FROM documents WHERE simhash IS NOT NULL AND extract_status='ok'")]
        pairs = near_duplicate_pairs(items)
        info = {r["doc_id"]: r for r in con.execute(
            "SELECT doc_id, ocr_used, year, length(path) AS plen FROM documents WHERE simhash IS NOT NULL")}
        n = 0
        for a, b, _d in pairs:
            ra, rb = info[a], info[b]
            # canonical: newer year, then non-OCR, then shorter path
            key = lambda r: (-(r["year"] or 0), r["ocr_used"], r["plen"])  # noqa: E731
            canon, dup = (a, b) if key(ra) <= key(rb) else (b, a)
            con.execute("INSERT OR REPLACE INTO duplicates(doc_id, canonical_doc_id, kind) VALUES (?,?,?)",
                        (dup, canon, "near"))
            n += 1
        con.commit()
        return n

    def reconcile_copy_flags(self) -> int:
        """G2.5: every member of a flagged copy group carries the group's most restrictive flag.

        Runs after duplicate marking on every run, so the result does not depend on which copy was
        scanned or extracted first. Raises flags only, never lowers one (a flag is dropped only when the
        file itself is re-extracted with different content, as for a move)."""
        con = self.con
        changed: list[str] = []
        for doc_id, flag in copy_group_flags(con).items():
            row = con.execute("SELECT classification_flag, rel_path FROM documents WHERE doc_id=?",
                              (doc_id,)).fetchone()
            if row is None or most_restrictive([row["classification_flag"], flag]) == row["classification_flag"]:
                continue
            con.execute("UPDATE documents SET classification_flag=? WHERE doc_id=?", (flag, doc_id))
            changed.append(f"{row['rel_path']}: {row['classification_flag'] or '-'} -> {flag}")
        con.commit()
        if changed:
            self.log(f"classification: {len(changed)} copies given their copy group's flag (G2.5)")
            for c in changed[:50]:
                self.log(f"  {c}")
        return len(changed)

    def _log_run(self, st: IndexStats, kind: str = "index") -> None:
        safeio.append_jsonl(self.cfg, self.cfg.log_dir / "index_runs.jsonl",
                            {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "kind": kind, **st.as_dict()})

    def close(self) -> None:
        self.con.close()
