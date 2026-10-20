"""ERLA command-line interface.

The command list is defined once, in build_parser(); run `erla --help` or
`erla <command> --help`. Protocol reference: ERLA.md "Primary Commands".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, safeio


def _cfg():
    from .config import ConfigError, load_config
    try:
        return load_config()
    except ConfigError as exc:          # first run / moved library: say how to fix it
        sys.exit(f"erla: {exc}")


def _con(cfg, must_exist: bool = True, write: bool = False):
    """Read-only by default (K1). Pass write=True only in commands that change the index."""
    from . import store
    if must_exist and not cfg.index_path.is_file():
        sys.exit(f"Index not found at {cfg.index_path}. Run: erla index")
    return store.connect_writer(cfg) if write else store.connect(cfg.index_path)


def _out(text: str) -> None:
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write((text + "\n").encode("utf-8", errors="replace"))
    else:                       # redirected (e.g. captured by the MCP server)
        sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _site(a) -> Path:
    from .config import AGENT_ROOT
    return Path(a.site).expanduser() if getattr(a, "site", None) else AGENT_ROOT


def _browse(a, cmd: str, initial: str | None = None) -> list[str]:
    """--library values plus any folders picked in the dialog (--browse)."""
    from .site import SiteError, browse_for_folders
    roots = list(a.library or [])
    if getattr(a, "browse", False):
        _out("Opening the folder dialog on this computer's desktop ...")
        try:
            picked = browse_for_folders(initial=initial)
        except SiteError as exc:
            sys.exit(f"erla {cmd}: {exc}")
        if not picked and not roots:
            sys.exit(f"erla {cmd}: no folder selected; nothing changed")
        roots += picked
    if not roots:
        sys.exit(f"erla {cmd}: give the library folder with --browse (dialog) or --library <folder> "
                 f"(repeat for several roots)")
    return roots


def _current_root(site: Path) -> str | None:
    import yaml
    p = site / "config" / "library_config.yaml"
    try:
        roots = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("library_roots") or []
        return str(roots[0]) if roots else None
    except (OSError, yaml.YAMLError):
        return None


def cmd_init(a) -> int:
    from .site import SiteError, init_site, resurvey
    site = _site(a)
    try:
        if a.resurvey:
            res = resurvey(site)
        else:
            roots = _browse(a, "init")
            res = init_site(site, roots, unit_system=a.units, force=a.force, survey=not a.no_survey)
    except SiteError as exc:
        sys.exit(f"erla init: {exc}")
    _out(json.dumps(res, indent=2, default=str))
    return 0


def cmd_set_library(a) -> int:
    from .site import SiteError, set_library_roots
    try:
        site = _site(a)
        roots = _browse(a, "set-library", initial=_current_root(site))
        res = set_library_roots(site, roots, reason=a.reason or "erla set-library")
    except SiteError as exc:
        sys.exit(f"erla set-library: {exc}")
    _out(json.dumps(res, indent=2, default=str))
    return 0


def cmd_web_allow(a) -> int:
    from .site import SiteError, set_web_allow_terms
    try:
        res = set_web_allow_terms(_site(a), add=a.add, remove=a.remove, reason=a.reason or "erla web-allow")
    except SiteError as exc:
        sys.exit(f"erla web-allow: {exc}")
    _out(json.dumps(res, indent=2, default=str))
    return 0


def cmd_check_policy(a) -> int:
    if not a.policy:
        from .config import check_policy, load_config
        cfg = load_config(_site(a) / "config" / "library_config.yaml", validate=False)
        _out(json.dumps({"policy": check_policy(cfg)}))
        return 0
    from .site import SiteError, set_check_policy
    try:
        res = set_check_policy(_site(a), a.policy, reason=a.reason or "erla check-policy")
    except SiteError as exc:
        sys.exit(f"erla check-policy: {exc}")
    _out(json.dumps(res, indent=2, default=str))
    return 0


def cmd_site(a) -> int:
    from .config import core_dir, protocol_dir
    from .site import review_status
    site = _site(a)
    _out(json.dumps({"site": str(site), "core": str(core_dir()), "protocols": str(protocol_dir()),
                     "config_exists": (site / "config" / "library_config.yaml").is_file(),
                     "review": review_status(site)}, indent=2, default=str))
    return 0


def cmd_serve(a) -> int:
    from .mcp_server import run
    run(library=a.library or None)
    return 0


# ------------------------------------------------------------------ commands
def cmd_config(a) -> int:
    from .config import ConfigError, load_config, validate_config
    try:
        cfg = load_config(validate=False)
    except ConfigError as exc:
        sys.exit(f"erla: {exc}")
    _out(f"config file : {cfg.source_path}")
    _out(f"library     : {', '.join(str(r) for r in cfg.library_roots)} (READ-ONLY)")
    _out(f"index       : {cfg.index_path}")
    _out(f"include_ext : {', '.join(sorted(cfg.include_ext))}")
    _out(f"exclude     : {', '.join(cfg.exclude_globs)}")
    _out(f"ocr         : enabled={cfg.ocr.enabled} tesseract={cfg.ocr.tesseract_cmd}")
    _out(f"legacy      : {cfg.legacy_office}")
    _out(f"embeddings  : {cfg.embeddings}")
    problems = validate_config(cfg)
    _out("validation  : " + ("OK" if not problems else "\n  " + "\n  ".join(problems)))
    return 1 if any(p.startswith("ERROR") for p in problems) else 0


def cmd_index(a) -> int:
    from .indexer import Indexer
    cfg = _cfg()
    idx = Indexer(cfg, log=lambda m: _out(m))
    st = idx.run(full=a.full, limit=a.limit, subpath=a.path, ocr=a.ocr)
    idx.close()
    _out(json.dumps(st.as_dict(), indent=2))
    return 0


def cmd_refresh(a) -> int:
    """P3: one-shot refresh = probe -> incremental index -> deferred OCR -> embeddings -> report."""
    from . import store
    from .indexer import Indexer, probe_library
    cfg = _cfg()
    con = _con(cfg)
    before = probe_library(cfg, con)
    con.close()
    summary = {"probe_before": before}
    if not before["stale"] and not a.force:
        summary["action"] = "none - index CURRENT"
        _out(json.dumps(summary, indent=2, default=str))
        return 0
    lock = _lock_path(cfg)
    if not _acquire_refresh_lock(lock):
        sys.exit(f"refresh already running (lock {lock}, {_read_lock(lock)})")
    try:
        return _refresh_body(a, cfg, summary)
    finally:
        safeio.unlink_at(lock)


REFRESH_LOCK_MAX_AGE_S = 24 * 3600  # no legitimate refresh runs this long


def _lock_path(cfg) -> safeio.WritablePath:
    """The single-instance refresh lock, checked against G1.2."""
    return safeio.check(cfg, cfg.log_dir / "refresh.lock")


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (Windows via kernel32, POSIX via signal 0)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # ACCESS_DENIED -> exists
        try:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_lock(lock) -> str:
    try:
        return lock.read_text().strip() or "empty"
    except OSError:
        return "unreadable"


REFRESH_HANG_S = 60 * 60   # S5: alive but no progress heartbeat for this long = hung
HEARTBEAT_EVERY_S = 30     # throttle for lock mtime updates


def _proc_cmdline(pid: int) -> str:
    """Command line of a process ('' if unknown). Used to confirm a PID is an ERLA refresh before killing it."""
    import subprocess
    try:
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                               capture_output=True, text=True, timeout=30)
            return r.stdout.strip()
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="ignore")
    except Exception:
        return ""


def _is_erla_refresh(cmdline: str) -> bool:
    """True only for an `erla[.exe] ... refresh` / `python -m erla ... refresh` command line
    (exact `refresh` argument token; a path merely containing 'erla'/'refresh' does not match)."""
    toks = [t.strip('"\'').lower().replace("\\", "/") for t in cmdline.split()]
    names = {t.rsplit("/", 1)[-1] for t in toks}
    is_erla = bool(names & {"erla", "erla.exe", "erla-script.py"}) or "erla.cli" in toks or (
        "-m" in toks and "erla" in toks)
    return is_erla and "refresh" in toks


def _kill_tree(pid: int) -> bool:
    import subprocess
    try:
        if os.name == "nt":
            return subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                                  capture_output=True, timeout=60).returncode == 0
        import signal
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def _lock_is_stale(lock, hang_s: float | None = None) -> tuple[bool, str]:
    """A lock is stale if its PID is dead, it is unparseable, or it exceeds the max age.

    S5: locks written with a heartbeat (`hb=1`) have their mtime touched while the refresh makes
    progress. If such a lock's holder is alive but the heartbeat is older than `hang_s`, the holder
    is hung: it is killed (only after confirming its command line is an `erla ... refresh`) and the
    lock is stale. Legacy locks without `hb=1` keep the 24 h rule only.
    """
    import time
    hang_s = REFRESH_HANG_S if hang_s is None else hang_s
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        return True, "vanished"
    parts = _read_lock(lock).split()
    try:
        pid = int(parts[0])
    except (IndexError, ValueError):
        return (age > 60), f"unparseable lock, age {age:.0f}s"  # grace for a writer mid-create
    if age > REFRESH_LOCK_MAX_AGE_S:
        return True, f"pid {pid}, age {age/3600:.1f} h > {REFRESH_LOCK_MAX_AGE_S/3600:.0f} h"
    if not _pid_alive(pid):
        return True, f"pid {pid} not running"
    if "hb=1" in parts and age > hang_s:
        if _is_erla_refresh(_proc_cmdline(pid)):
            killed = _kill_tree(pid)
            return killed, (f"pid {pid} hung: no progress for {age/60:.0f} min > {hang_s/60:.0f} min; "
                            f"{'killed' if killed else 'kill FAILED'}")
        return False, f"pid {pid} no heartbeat for {age/60:.0f} min but is not an erla refresh - not touched"
    return False, f"pid {pid} alive, last progress {age:.0f}s ago"


def _acquire_refresh_lock(lock) -> bool:
    """Atomic create; reclaim once if the existing lock is stale (hard-killed run skips `finally`)."""
    import datetime
    for _ in range(2):
        try:
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            safeio.create_exclusive_at(lock, f"{os.getpid()} {stamp} hb=1")
            return True
        except FileExistsError:
            stale, why = _lock_is_stale(lock)
            if not stale:
                return False
            _out(f"reclaiming stale refresh lock ({why})")
            safeio.unlink_at(lock)
    return False


def _make_heartbeat(lock, every_s: float = HEARTBEAT_EVERY_S):
    """Return a cheap callable that touches the lock mtime at most every `every_s` seconds."""
    import time
    last = [0.0]

    def beat() -> None:
        now = time.time()
        if now - last[0] >= every_s:
            last[0] = now
            safeio.touch_at(lock)
    return beat


def _refresh_body(a, cfg, summary) -> int:
    from . import chain, store
    from .indexer import probe_library
    beat = _make_heartbeat(_lock_path(cfg))
    summary.update(chain.run_chain(cfg, run_ocr=not a.no_ocr, run_embed=not a.no_embed, ocr_limit=a.ocr_limit,
                                   heartbeat=beat, log=_out))
    con = _con(cfg, write=True)
    summary["probe_after"] = probe_library(cfg, con)
    summary["ocr_pending_after"] = con.execute("SELECT count(*) FROM documents WHERE ocr_pending=1").fetchone()[0]
    summary["last_index_at"] = store.get_meta(con, "last_index_at")
    summary["fts_integrity"] = store.fts_check(con)[1]
    con.close()
    from . import report
    report.append_log(cfg, {"cmd": "refresh", **{k: summary.get(k) for k in ("index", "ocr", "embedded_chunks")}})
    _out(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_fts(a) -> int:
    """FTS5 integrity check; --rebuild drops and repopulates chunks_fts from chunks (index only)."""
    from . import store
    cfg = _cfg()
    con = _con(cfg, write=True)
    if a.rebuild:
        n = store.fts_rebuild(con, log=_out)
        _out(f"fts rebuilt: {n} chunks")
    ok, msg = store.fts_check(con)
    _out(f"fts integrity: {'OK' if ok else 'CORRUPT - ' + msg}")
    return 0 if ok else 2


def cmd_ocr(a) -> int:
    from .indexer import Indexer
    cfg = _cfg()
    if not cfg.ocr.enabled:
        sys.exit("OCR disabled in config")
    idx = Indexer(cfg, log=lambda m: _out(m))
    st = idx.run_ocr(limit=a.limit)
    idx.close()
    _out(json.dumps(st.as_dict(), indent=2))
    return 0


def cmd_search(a) -> int:
    from . import report, store
    from .search import search
    cfg = _cfg()
    con = _con(cfg)
    hits, exp, mode = search(
        con, a.query, k=a.k,
        types=[t.strip().lower().lstrip(".") for t in a.type.split(",")] if a.type else None,
        tiers=[t.strip().upper() for t in a.tier.split(",")] if a.tier else None,
        year_min=a.year_min, discipline=a.discipline, exclude_flagged=a.exclude_flagged,
        boosts=report.feedback_boosts(cfg), cfg=cfg, hybrid=not a.no_vector,
        rerank=False if a.no_rerank else None, reranker=a.reranker)
    idx_date = store.get_meta(con, "last_index_at")
    hd = [h.as_dict() for h in hits]
    payload = {"query": a.query, "index_date": idx_date, "mode": mode, "expanded": exp.describe(), "hits": hd}
    if a.format == "json":
        _out(json.dumps(payload, indent=2, default=str))
    elif a.format == "table":
        for i, h in enumerate(hits, 1):
            _out(f"{i:>2}. [{h.tier}] {h.title[:70]}  ({h.ext}, doc {h.doc_id}) {', '.join(h.locators)}")
    else:
        _out(report.render_markdown(a.query, hd, exp.describe(), mode, idx_date))
    if a.export:
        for p in report.export(cfg, a.export, payload, hd):
            _out(f"exported: {p}")
    report.append_log(cfg, {"cmd": "search", "query": a.query, "expanded": exp.describe(), "mode": mode,
                            "k": a.k, "returned": [h.doc_id for h in hits],
                            "flags": sorted({h.flag for h in hits if h.flag})})
    return 0


def _quality_lines(qj) -> list[str]:
    """A5/A6 warnings stored with a chunk, for display next to its text (G3.5)."""
    if not qj:
        return []
    try:
        q = json.loads(qj)
    except ValueError:
        return []
    out = []
    if "ocr_conf" in q:
        out.append(f"OCR page, mean word confidence {q['ocr_conf']} - verify values visually (G3.5)")
    if q.get("low_conf_numbers"):
        out.append("OCR_LOW_CONF numbers: " + ", ".join(q["low_conf_numbers"][:20]))
    if q.get("ocr_confusions"):
        out.append("OCR_CONFUSION tokens (letter/digit): " + ", ".join(q["ocr_confusions"][:20]))
    out += [str(w) for w in q.get("warnings") or []]          # extraction warnings (EPUB, 0.5.6)
    t = q.get("table") or {}
    if t:
        out.append(f"table extracted ({t.get('method', 'ruled')}, {t.get('rows')}x{t.get('cols')}) - "
                   f"structure is reconstructed; check row/column alignment against the page")
    w = q.get("docx") or {}
    if w.get("tracked_changes"):
        out.append("Word tracked changes: text shown with revisions accepted (insertions kept, deletions "
                   "dropped) - the document is not final; confirm the value against the issued revision")
    if w.get("ole_equations"):
        out.append(f"{w['ole_equations']} equation object(s) (Equation Editor / MathType) not readable as "
                   f"text - open the file to read them")
    if w.get("unmapped_symbols"):
        out.append("symbol-font characters not mapped (shown as �): " + ", ".join(w["unmapped_symbols"][:20]))
    return out


def cmd_tables(a) -> int:
    """A2/A3 backfill: PDF tables + property records for documents indexed before 0.2.0."""
    from . import tables
    cfg = _cfg()
    lock = _lock_path(cfg)
    if not _acquire_refresh_lock(lock):
        sys.exit(f"refresh running (lock {lock}, {_read_lock(lock)}); run `erla tables` after it finishes")
    try:
        con = _con(cfg, write=True)
        st = tables.backfill(con, cfg, limit=a.limit, log=_out, heartbeat=_make_heartbeat(lock),
                             max_seconds=a.max_minutes * 60 if a.max_minutes else None)
        _out(json.dumps(st, indent=2))
    finally:
        safeio.unlink_at(lock)
    return 0


def cmd_reclassify(a) -> int:
    """K3/T-E: re-apply tier and doc-type rules to indexed documents (no re-extraction)."""
    import time
    from . import reclassify
    cfg = _cfg()
    out_dir = cfg.out_dir
    stamp = time.strftime("%Y-%m-%d")
    out_csv = out_dir / f"reclassify_{'dryrun_' if a.dry_run else ''}{stamp}.csv"
    if a.dry_run:
        con = _con(cfg, write=False)
        st = reclassify.reclassify(con, write=False, out_csv=out_csv, guard=cfg, log=_out)
    else:
        lock = _lock_path(cfg)
        if not _acquire_refresh_lock(lock):
            sys.exit(f"refresh running (lock {lock}, {_read_lock(lock)}); run `erla reclassify` after it finishes")
        try:
            con = _con(cfg, write=True)
            st = reclassify.reclassify(con, write=True, out_csv=out_csv, guard=cfg, log=_out, heartbeat=_make_heartbeat(lock))
        finally:
            safeio.unlink_at(lock)
    _out(f"changes: {out_csv}")
    _out(json.dumps(st, indent=2))
    return 0


def cmd_props(a) -> int:
    """A3: query the structured property store. Every row cites its locator; open it before citing (R3.3)."""
    from . import props, units
    cfg = _cfg()
    con = _con(cfg)
    rows = props.query(con, quantity=a.quantity, material=a.material, basis=a.basis, temper=a.temper,
                       form=a.form, direction=a.direction,
                       tiers=[t.strip().upper() for t in a.tier.split(",")] if a.tier else None,
                       exclude_flagged=a.exclude_flagged, k=a.k)
    if a.format == "json":
        _out(json.dumps(rows, indent=2, default=str))
        return 0
    _out(f"## Property records - {a.quantity or 'any'}"
         f"{' / ' + a.material if a.material else ''}  ({len(rows)} rows; index, not source - open each locator)")
    _out("| # | Qty | Value | Units | Conv. | Material | Temper | Form | Thickness | Dir | Basis | Tier | Doc | Locator | Flags |")
    _out("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        conv = ""
        if r["units"] and r["value"] is not None and not str(r["units"]).startswith("10^"):
            c = units.to_project(r["value"], r["units"], cfg.unit_system)
            conv = f"{c.value:.4g} {c.unit} (x{c.factor:.6g})" if c and c.factor else ""
        flags = "; ".join(f.split(":")[0] for f in r["flags"]) + (" [OCR]" if r["ocr"] else "")
        cell = lambda v: (str(v) if v is not None else "").replace("|", "/")[:40]   # noqa: E731
        _out(f"| {i} | {r['quantity']} | {r['value_text']} | {cell(r['units'])} | {conv} | {cell(r['material'])} | "
             f"{cell(r['temper'])} | {cell(r['form'])} | {cell(r['thickness'])} | {cell(r['direction'])} | "
             f"{cell(r['basis'])} | {r['tier'] or 'T5'}{' ' + r['flag'] if r['flag'] else ''} | {r['doc_id']} | "
             f"{cell(r['locator'])} | {flags} |")
    return 0


def cmd_convert(a) -> int:
    """A4: unit conversion with the factor shown (R4.6)."""
    from . import units
    try:
        c = units.convert(a.value, a.unit, a.to)
    except ValueError as exc:
        sys.exit(str(exc))
    _out(c.text())
    return 0


def cmd_show(a) -> int:
    cfg = _cfg()
    con = _con(cfg)
    d = con.execute("SELECT * FROM documents WHERE doc_id=?", (a.doc_id,)).fetchone()
    if not d:
        sys.exit(f"doc {a.doc_id} not found")
    _out(f"# doc {d['doc_id']}: {d['title']}")
    _out(f"path: {d['path']}")
    from .classify import copy_group_flags, effective_flag
    flag = effective_flag(d["classification_flag"], d["doc_id"], copy_group_flags(con))   # G2.5
    _out(f"type={d['ext']}/{d['doc_type']} tier={d['authority_tier']} flag={flag} "
         f"year={d['year']} std={d['standard_id']} rev={d['revision']} ocr={d['ocr_used']} "
         f"status={d['extract_status']}")
    dup = con.execute("SELECT canonical_doc_id, kind FROM duplicates WHERE doc_id=?", (a.doc_id,)).fetchone()
    if dup:
        _out(f"DUPLICATE ({dup['kind']}) of doc {dup['canonical_doc_id']}")
    from .store import quality_expr
    rows = con.execute(f"SELECT seq, locator, text, {quality_expr(con, 'chunks')} FROM chunks WHERE doc_id=? ORDER BY seq",
                       (a.doc_id,)).fetchall()
    if a.locator:
        from .store import at_locator
        sel = at_locator(rows, a.locator)
        if not sel:
            _out(f"locator '{a.locator}' not found. Available: {', '.join(r['locator'] for r in rows[:60])}")
            return 1
        seqs = {r["seq"] for r in sel}
        if a.context:
            seqs |= {s + o for s in list(seqs) for o in range(-a.context, a.context + 1)}
        for r in rows:
            if r["seq"] in seqs:
                _out(f"\n--- {r['locator']} ---")
                for w in _quality_lines(r["quality_json"]):
                    _out(f"!! {w}")
                _out(r["text"][:a.chars])
    else:
        _out(f"chunks: {len(rows)}")
        _out("locators: " + ", ".join(r["locator"] for r in rows[:200]) + (" …" if len(rows) > 200 else ""))
    return 0


def cmd_files(a) -> int:
    from .search import search
    cfg = _cfg()
    con = _con(cfg)
    hits, exp, mode = search(con, a.query, k=a.k, cfg=cfg)
    groups: dict[str, list] = {}
    for h in hits:
        groups.setdefault(h.doc_type, []).append(h)
    _out(f"## File inventory — {a.query}\nExpanded: {'; '.join(exp.describe())}\n")
    for dt, hs in sorted(groups.items()):
        _out(f"### {dt} ({len(hs)})\n| Doc | Title | Type | Year | Tier | Flag | Dup | Path |\n|---|---|---|---|---|---|---|---|")
        for h in hs:
            _out(f"| {h.doc_id} | {h.title[:60]} | {h.ext} | {h.year or ''} | {h.tier} | {h.flag or ''} | "
                 f"{h.duplicates or ''} | {h.rel_path} |")
        _out("")
    return 0


def cmd_stats(a) -> int:
    from . import store
    cfg = _cfg()
    con = _con(cfg)
    q = lambda s: con.execute(s).fetchall()  # noqa: E731
    _out(f"index: {cfg.index_path} ({cfg.index_path.stat().st_size / 1e6:.1f} MB)")
    last = store.get_meta(con, "last_index_at")
    stale = ""
    if last:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
        stale = f" ({age} days old{' — STALE, run erla index' if age > cfg.stale_days else ''})"
    _out(f"last index: {last}{stale}")
    if not getattr(a, "no_probe", False):
        from .indexer import probe_library
        pr = probe_library(cfg, con)
        _out(f"library probe: {pr['files_on_disk']} eligible files on disk, {pr['new']} new, "
             f"{pr['changed']} changed, {pr['missing']} missing; newest {pr['newest_mtime']} ({pr['newest_file']})")
        _out("index state: " + ("STALE - " + "; ".join(pr["reasons"]) + " -> run `erla refresh`"
                                if pr["stale"] else "CURRENT"))
    _out(f"documents: {q('SELECT count(*) FROM documents')[0][0]}   chunks: {q('SELECT count(*) FROM chunks')[0][0]}")
    _out("by ext: " + ", ".join(f"{r[0]}={r[1]}" for r in q("SELECT ext, count(*) FROM documents GROUP BY ext ORDER BY 2 DESC")))
    _out("by status: " + ", ".join(f"{r[0]}={r[1]}" for r in q("SELECT extract_status, count(*) FROM documents GROUP BY 1 ORDER BY 2 DESC")))
    _out("by tier: " + ", ".join(f"{r[0]}={r[1]}" for r in q("SELECT authority_tier, count(*) FROM documents WHERE extract_status!='duplicate' GROUP BY 1 ORDER BY 1")))
    _out(f"ocr used: {q('SELECT count(*) FROM documents WHERE ocr_used=1')[0][0]}   "
         f"ocr pending: {q('SELECT count(*) FROM documents WHERE ocr_pending=1')[0][0]}")
    _out("duplicates: " + ", ".join(f"{r[0]}={r[1]}" for r in q("SELECT kind, count(*) FROM duplicates GROUP BY kind")))
    _out("flags: " + (", ".join(f"{r[0]}={r[1]}" for r in q("SELECT classification_flag, count(*) FROM documents WHERE classification_flag IS NOT NULL GROUP BY 1")) or "none"))
    from . import xcache
    emb = q("SELECT count(*) FROM embeddings")[0][0]
    _out(f"vectors: {emb} embedded; search index {store.get_meta(con, 'vector_backend', 'none')} "
         f"({store.get_meta(con, 'vector_rows', '0')} rows, built {store.get_meta(con, 'vector_built_at', '-')})")
    xs = xcache.stats(con)
    _out(f"extract cache: {xs['entries']} entries, {xs['mb']} MB ({xs['version']})")
    from . import props, tables
    ps = props.stats(con)
    pend = len(tables.pending_docs(con))
    _out(f"properties (A3): {ps['records']} records from {ps['documents']} documents, {ps['flagged']} flagged; "
         f"PDFs awaiting table scan: {pend}")
    return 0


def cmd_cache(a) -> int:
    """S8 extract cache: stats; --seed fills it from already-indexed legacy documents (no Office started)."""
    from . import xcache
    cfg = _cfg()
    con = _con(cfg, write=bool(a.seed))
    if a.seed:
        xcache.seed_from_index(con, xcache.cache_exts(cfg), log=_out)
    _out(json.dumps(xcache.stats(con)))
    return 0


def cmd_eval(a) -> int:
    from .evaluate import run_eval
    cfg = _cfg()
    con = _con(cfg)
    from .config import core_dir
    golden = Path(a.golden) if a.golden else cfg.agent_root / "eval" / "golden_queries.yaml"
    if not golden.is_file():
        golden = core_dir() / "eval" / "golden_queries.yaml"
    res = run_eval(con, cfg, golden, hybrid=not a.no_vector, rerank=False if a.no_rerank else None,
                   reranker=a.reranker, memory=a.memory)
    _out(json.dumps(res, indent=2, default=str))
    return 0


def cmd_memory(a) -> int:
    from . import memory
    cfg = _cfg()
    if a.action == "forget":
        if not a.entry:
            sys.exit("give the entry id to forget (erla memory list shows them)")
        if not memory.forget(cfg, a.entry):
            sys.exit(f"no source-memory entry {a.entry}")
        _out(f"forgotten: {a.entry}")
        return 0
    for e in memory.load(cfg):
        _out(f"{e['id']}  doc {e['doc_id']} {e['locator']}  [{' '.join(e['topic'])}]  {e['evidence']}  {e['ts']}")
    return 0


def cmd_embed(a) -> int:
    from .embed import embed_pending, export_matrix, prune_duplicate_embeddings, vector_backend
    cfg = _cfg()
    if not cfg.embeddings.get("enabled"):
        sys.exit("embeddings disabled in config")
    con = _con(cfg, write=True)
    p = prune_duplicate_embeddings(con)
    n = embed_pending(con, cfg, limit=a.limit, log=_out)
    m = export_matrix(con, cfg)
    _out(f"pruned {p} duplicate-document vectors; embedded {n} new chunks; "
         f"vector index rows: {m} ({vector_backend(cfg)})")
    return 0


def cmd_webcheck(a) -> int:
    from .classify import web_allow_terms, web_query_check
    cfg = _cfg()
    con = _con(cfg, must_exist=False)
    reasons = web_query_check(a.query, con, web_allow_terms(cfg))
    _out("BLOCKED: " + "; ".join(reasons) if reasons else "SAFE")
    return 2 if reasons else 0


def cmd_tier(a) -> int:
    from .classify import tier_for_url
    _out(tier_for_url(a.url))
    return 0


def cmd_log(a) -> int:
    from .report import append_log
    try:
        rec = json.loads(a.json)
    except json.JSONDecodeError as exc:
        sys.exit(f"invalid JSON: {exc}")
    _out(str(append_log(_cfg(), {"cmd": "agent", **rec})))
    return 0


def cmd_lesson(a) -> int:
    from .specialists import record_lesson
    res = record_lesson(_cfg(), a.specialist, a.kind, a.lesson, a.evidence, a.rule)
    if "error" in res:
        sys.exit(res["error"])
    _out(res["logged_to"])
    return 0


def cmd_feedback(a) -> int:
    from .report import append_log
    _out(str(append_log(_cfg(), {"doc_id": a.doc_id, "verdict": a.verdict, "note": a.note}, "feedback.jsonl")))
    return 0


def cmd_tools(a) -> int:
    """Engineer Tools (ADR-0003). ``accept`` exists only here: no MCP tool can accept a tool (ET3)."""
    import json

    from . import engtools
    cfg = _cfg()
    try:
        if a.action == "list":
            res = engtools.list_tools(cfg)
        elif not a.target:
            raise engtools.ToolError(f"erla tools {a.action} needs a tool id" + (" or folder" if a.action == "add" else ""))
        elif a.action == "add":
            res = engtools.add(cfg, a.target, env=not a.no_env)
        elif a.action == "accept":
            if not a.sha:
                raise engtools.ToolError("accept needs --sha: the hash of the files you checked (erla tools list)")
            res = engtools.accept(cfg, a.target, a.sha, a.by)
        else:
            res = {"test": engtools.test, "start": engtools.start, "stop": engtools.stop}[a.action](cfg, a.target)
    except engtools.ToolError as exc:
        res = {"ok": False, "error": str(exc)}
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok", True) else 1


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="erla", description="ERLA - Engineering Research Library Agent toolkit")
    p.add_argument("--version", action="version", version=f"erla {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("config", help="show resolved configuration").set_defaults(fn=cmd_config)

    s = sub.add_parser("init", help="set up this machine: library folder(s), site config, SITE_NOTES")
    s.add_argument("--library", action="append", help="library root folder (READ-ONLY); repeat for several")
    s.add_argument("--browse", action="store_true", help="choose the library folder(s) in a folder dialog")
    s.add_argument("--site", help="site folder (default: ERLA_ROOT or per-user ERLA folder)")
    s.add_argument("--units", choices=["US", "SI"], default="US")
    s.add_argument("--force", action="store_true", help="rebuild an existing site config")
    s.add_argument("--no-survey", action="store_true", help="skip the library survey")
    s.add_argument("--resurvey", action="store_true", help="only re-measure the library and rewrite SITE_NOTES")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("set-library", help="change the library folder(s) of this site")
    s.add_argument("--library", action="append", help="new library root folder; repeat for several")
    s.add_argument("--browse", action="store_true", help="choose the library folder(s) in a folder dialog")
    s.add_argument("--site")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_set_library)

    s = sub.add_parser("web-allow", help="list or edit the web gate's allowed generic terms (G2)")
    s.add_argument("--add", action="append", help="generic term a flagged document is named exactly; repeat")
    s.add_argument("--remove", action="append", help="term to take off the list; repeat")
    s.add_argument("--site")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_web_allow)

    s = sub.add_parser("check-policy", help="show or set when the Professor checks a Result Packet (ADR-0002)")
    s.add_argument("policy", nargs="?", choices=["auto", "always", "off"],
                   help="auto: computed values, discipline work, design values; always; off. No value: show it")
    s.add_argument("--site")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_check_policy)

    s = sub.add_parser("site", help="show site folder, core location, protocol-review status")
    s.add_argument("--site")
    s.set_defaults(fn=cmd_site)

    s = sub.add_parser("serve", help="run the ERLA MCP server (stdio)")
    s.add_argument("--library", action="append", help="library root to use if the site is not yet set up")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("index", help="build/update the library index")
    s.add_argument("--full", action="store_true", help="re-extract every file")
    s.add_argument("--ocr", action="store_true", help="OCR scanned PDFs during this run (slow)")
    s.add_argument("--limit", type=int, help="extract at most N changed files")
    s.add_argument("--path", help="only this library subfolder (relative)")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("refresh", help="probe library; if changed: index -> ocr -> embed, then report (P3)")
    s.add_argument("--force", action="store_true", help="run even if the probe reports CURRENT")
    s.add_argument("--no-ocr", action="store_true")
    s.add_argument("--no-embed", action="store_true")
    s.add_argument("--ocr-limit", type=int)
    s.set_defaults(fn=cmd_refresh)

    s = sub.add_parser("fts", help="full-text index integrity check; --rebuild to repair")
    s.add_argument("--rebuild", action="store_true")
    s.set_defaults(fn=cmd_fts)

    s = sub.add_parser("ocr", help="OCR scanned PDFs deferred during indexing")
    s.add_argument("--limit", type=int)
    s.set_defaults(fn=cmd_ocr)

    s = sub.add_parser("search", help="search the index")
    s.add_argument("query")
    s.add_argument("--type")
    s.add_argument("--tier")
    s.add_argument("--year-min", type=int)
    s.add_argument("--discipline")
    s.add_argument("--exclude-flagged", action="store_true")
    s.add_argument("--k", type=int, default=20)
    s.add_argument("--format", choices=["md", "json", "table"], default="md")
    s.add_argument("--export", help="write out/<NAME>.json and .csv")
    s.add_argument("--no-vector", action="store_true", help="BM25 only (disable dense retrieval)")
    s.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder re-ranker (A1)")
    s.add_argument("--reranker", help="ms-marco (default, CPU, fast), bge-v2-m3 (CPU, slower), qwen3-4b (GPU), custom")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("props", help="query extracted material properties (A3); cite the locator, not this table")
    s.add_argument("quantity", nargs="?", help="Ftu, Fty, Fcy, Fsu, Fbru, Fbry, E, Ec, G, poisson, elongation, density, KIc")
    s.add_argument("--material")
    s.add_argument("--temper")
    s.add_argument("--form")
    s.add_argument("--basis", help="A, B, S or typical")
    s.add_argument("--direction", help="L, LT, ST")
    s.add_argument("--tier")
    s.add_argument("--exclude-flagged", action="store_true")
    s.add_argument("--k", type=int, default=50)
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.set_defaults(fn=cmd_props)

    s = sub.add_parser("tables", help="A2/A3 backfill: PDF tables + property records for already-indexed files")
    s.add_argument("--limit", type=int, help="at most N PDFs this run (resumable)")
    s.add_argument("--max-minutes", type=float, help="stop after this many minutes (resumable)")
    s.set_defaults(fn=cmd_tables)

    s = sub.add_parser("reclassify", help="K3: re-apply authority-tier and doc-type rules to indexed documents")
    s.add_argument("--dry-run", action="store_true", help="read-only: report changes to out/reclassify_dryrun_<date>.csv")
    s.set_defaults(fn=cmd_reclassify)

    s = sub.add_parser("convert", help="unit conversion with factor (A4), e.g. erla convert 72 ksi MPa")
    s.add_argument("value", type=float)
    s.add_argument("unit")
    s.add_argument("to")
    s.set_defaults(fn=cmd_convert)

    s = sub.add_parser("embed", help="compute local embeddings for new chunks (Phase 4)")
    s.add_argument("--limit", type=int)
    s.set_defaults(fn=cmd_embed)

    s = sub.add_parser("cache", help="legacy-format extract cache (S8): stats; --seed from the current index")
    s.add_argument("--seed", action="store_true")
    s.set_defaults(fn=cmd_cache)

    s = sub.add_parser("show", help="show a document / chunk at a locator")
    s.add_argument("doc_id", type=int)
    s.add_argument("--locator")
    s.add_argument("--context", type=int, default=0, help="neighbouring chunks to include")
    s.add_argument("--chars", type=int, default=6000)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("files", help="file inventory for a topic (W1)")
    s.add_argument("query")
    s.add_argument("--k", type=int, default=50)
    s.set_defaults(fn=cmd_files)

    sub.add_parser("stats", help="index statistics").set_defaults(fn=cmd_stats)

    s = sub.add_parser("eval", help="run golden-query evaluation")
    s.add_argument("--golden")
    s.add_argument("--no-vector", action="store_true")
    s.add_argument("--no-rerank", action="store_true", help="measure without the cross-encoder (A1)")
    s.add_argument("--reranker", help="measure this re-ranker instead of the site's (see erla search --reranker)")
    s.add_argument("--memory", action="store_true", help="include the source memory (off: it would learn the answers)")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("memory", help="source memory: list what verified answers taught the ranking, or forget one")
    s.add_argument("action", choices=["list", "forget"])
    s.add_argument("entry", nargs="?", help="entry id to forget")
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("webcheck", help="check a query is safe to send to the web (G2)")
    s.add_argument("query")
    s.set_defaults(fn=cmd_webcheck)

    s = sub.add_parser("tier", help="authority tier for a URL")
    s.add_argument("url")
    s.set_defaults(fn=cmd_tier)

    s = sub.add_parser("log", help="append a retrieval-log record (JSON)")
    s.add_argument("json")
    s.set_defaults(fn=cmd_log)

    from .specialists import SPECIALISTS
    if SPECIALISTS:                                 # the free core edition has no specialists
        s = sub.add_parser("lesson", help="record a specialist lesson and open a review trigger")
        s.add_argument("specialist", choices=sorted(SPECIALISTS))
        s.add_argument("kind", choices=["mistake", "correction", "method", "resource"])
        s.add_argument("lesson")
        s.add_argument("--evidence", default="")
        s.add_argument("--rule", default="")
        s.set_defaults(fn=cmd_lesson)

    from .safeio import TOOLS_SHIPPED
    if TOOLS_SHIPPED:                               # Engineer Tools: full edition only (ADR-0003)
        s = sub.add_parser("tools", help="Engineer Tools: programs ERLA built for you (list, add, test, accept, "
                                         "start, stop); output is JSON")
        s.add_argument("action", choices=["list", "add", "test", "accept", "start", "stop"])
        s.add_argument("target", nargs="?", help="tool id, or for add the folder holding erla-tool.toml")
        s.add_argument("--sha", help="accept: the hash of the files you checked (from list)")
        s.add_argument("--by", default="", help="accept: who accepts (default: your user name)")
        s.add_argument("--no-env", action="store_true", help="add: do not build the tool's environment")
        s.set_defaults(fn=cmd_tools)

    s = sub.add_parser("feedback", help="record result feedback")
    s.add_argument("doc_id", type=int)
    s.add_argument("verdict", choices=["useful", "not_useful"])
    s.add_argument("--note")
    s.set_defaults(fn=cmd_feedback)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
