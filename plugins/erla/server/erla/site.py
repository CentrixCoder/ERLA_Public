"""Per-machine site layer: ``erla init``, library survey, library-root changes.

A *site* is one machine's ERLA working folder (``AGENT_ROOT``)::

    <site>/config/library_config.yaml   library roots + site settings (written here)
    <site>/config/*.yaml                optional overrides of core YAML files
    <site>/SITE_NOTES.md                generated survey of library + installed tools
    <site>/PROTOCOL_REVIEW.md           site protocol-review ledger (rules section 9)
    <site>/index.db, logs/, out/, web_cache/, models/, eval/

Nothing in this module writes inside a library root (guardrail G1.1); every
write target is checked with :meth:`Config.assert_writable` or is inside the
site folder, which is itself refused if it lies inside a library root.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from . import __version__, safeio
from .config import core_dir

REVIEW_INTERVAL_DAYS = 90

BASE_EXT = ["pdf", "docx", "xlsx", "xlsm", "xls", "csv", "pptx", "txt", "md", "json",
            "f06", "bdf", "dat", "op2", "inp"]
LEGACY_COM_EXT = ["doc", "ppt"]

DEFAULT_EXCLUDES = ["**/~$*", "**/.git/**", "**/__pycache__/**", "**/*.py", "**/*.pyc",
                    "**/*.pyi", "**/*.pyd", "**/scratch/**"]

# Conservative defaults (G2.5): anything that looks controlled is flagged local-only.
DEFAULT_CLASSIFICATION_RULES = [
    {"glob": "**/ITAR/**", "flag": "ITAR"},
    {"glob": "**/*ITAR*/**", "flag": "ITAR"},
    {"glob": "**/EAR/**", "flag": "EAR"},
    {"glob": "**/*Export*Control*/**", "flag": "EXPORT_CONTROLLED"},
    {"glob": "**/CUI/**", "flag": "CUI"},
    {"glob": "**/*Proprietary*/**", "flag": "PROPRIETARY"},
    {"glob": "**/Customer_*/**", "flag": "PROPRIETARY"},
]

TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract", "/usr/local/bin/tesseract", "/opt/homebrew/bin/tesseract",
]


class SiteError(ValueError):
    pass


# --------------------------------------------------------------- detection
def detect_tesseract() -> Path | None:
    found = shutil.which("tesseract")
    if found:
        return Path(found)
    local = os.environ.get("LOCALAPPDATA")
    cands = TESSERACT_CANDIDATES + ([str(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe")] if local else [])
    for c in cands:
        if Path(c).is_file():
            return Path(c)
    return None


def detect_office() -> dict[str, bool]:
    """Which MS Office COM servers are registered (Windows only). Read-only registry probe."""
    out = {"word": False, "powerpoint": False, "excel": False, "pywin32": False}
    if sys.platform != "win32":
        return out
    try:
        import win32com.client  # noqa: F401
        out["pywin32"] = True
    except Exception:
        return out
    import winreg
    for key, prog in (("word", "Word.Application"), ("powerpoint", "PowerPoint.Application"),
                      ("excel", "Excel.Application")):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r"\CLSID"))
            out[key] = True
        except OSError:
            pass
    return out


def runtime_info() -> dict[str, Any]:
    info = {"python": platform.python_version(), "platform": platform.platform(),
            "machine": platform.node(), "erla": __version__}
    try:
        import pymupdf
        info["pymupdf"] = pymupdf.VersionBind
    except Exception:
        info["pymupdf"] = None
    return info


# ------------------------------------------------------------------ survey
def survey_library(roots: list[Path], include_ext: list[str], excludes: list[str],
                   max_seconds: float = 900) -> dict[str, Any]:
    """Count files and bytes per extension (read-only walk; never opens files)."""
    from .config import Config
    probe = Config(library_roots=roots, agent_root=Path("."), index_path=Path("x"), out_dir=Path("x"),
                   log_dir=Path("x"), web_cache_dir=Path("x"), include_ext=set(include_ext),
                   exclude_globs=excludes)
    t0 = time.time()
    n_all: Counter = Counter()
    b_all: Counter = Counter()
    top: Counter = Counter()
    truncated = False
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                p = Path(dirpath) / fn
                ext = p.suffix.lower().lstrip(".") or "(none)"
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                n_all[ext] += 1
                b_all[ext] += size
                if probe.is_included(p):
                    rel = p.relative_to(root)
                    top[rel.parts[0] if len(rel.parts) > 1 else "(root)"] += 1
            if time.time() - t0 > max_seconds:
                truncated = True
                break
    inc = {e: (n_all[e], b_all[e]) for e in include_ext if n_all[e]}
    return {
        "indexable_files": sum(v[0] for v in inc.values()),
        "indexable_gb": round(sum(v[1] for v in inc.values()) / 1e9, 2),
        "by_ext": {e: {"files": n, "gb": round(b / 1e9, 3)} for e, (n, b) in
                   sorted(inc.items(), key=lambda kv: -kv[1][1])},
        "not_indexed_top": {e: n_all[e] for e, _ in (n_all - Counter({e: n_all[e] for e in include_ext})).most_common(12)},
        "top_folders": sorted(top),
        "seconds": round(time.time() - t0, 1),
        "truncated": truncated,
    }


# ------------------------------------------------------------------ dialog
def browse_for_folders(initial: str | Path | None = None, multiple: bool = True) -> list[str]:
    """Open the native folder picker (Tk) and return the chosen folder(s); [] if cancelled.

    Read-only: the dialog only returns paths; the caller validates and writes the site config.
    Raises SiteError when no dialog can be shown (Tk missing, or no interactive desktop,
    e.g. a scheduled task or service session).
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception as exc:  # pragma: no cover - Tk missing from this Python build
        raise SiteError(f"folder dialog unavailable ({exc}); use --library \"<folder>\" instead") from exc
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - no interactive desktop
        raise SiteError(f"folder dialog cannot open here ({exc}); use --library \"<folder>\" instead") from exc
    root.withdraw()
    root.attributes("-topmost", True)
    picked: list[str] = []
    start = str(initial) if initial and Path(initial).is_dir() else str(Path.home())
    try:
        while True:
            title = ("ERLA - select the engineering library folder (read-only)" if not picked
                     else "ERLA - select another library folder")
            d = filedialog.askdirectory(parent=root, title=title, initialdir=start, mustexist=True)
            if not d:
                break
            p = str(Path(d))
            if p not in picked:
                picked.append(p)
            if not multiple or not messagebox.askyesno(
                    "ERLA", "Library folder(s) selected:\n\n" + "\n".join(picked)
                    + "\n\nAdd another library folder?", parent=root):
                break
            start = str(Path(p).parent)
    finally:
        root.destroy()
    return picked


# ------------------------------------------------------------------ config
def _validate_roots(roots: list[str | Path], site: Path) -> list[Path]:
    out: list[Path] = []
    s = site.resolve()
    for r in roots:
        p = Path(str(r).strip().strip('"')).expanduser()
        if not p.is_dir():
            raise SiteError(f"library folder not found or not a folder: {p}")
        p = p.resolve()
        if s == p or p in s.parents:
            raise SiteError(f"the ERLA site folder {site} is inside library folder {p}; "
                            f"choose a site folder outside the library (G1.1)")
        if s in p.parents:
            raise SiteError(f"library folder {p} is inside the ERLA site folder {site}; "
                            f"the library and ERLA's working folder must not overlap (G1.1)")
        if p not in out:
            out.append(p)
    if not out:
        raise SiteError("at least one library folder is required")
    return out


def build_site_config(site: Path, roots: list[Path], unit_system: str = "US",
                      tesseract: Path | None = None, office: dict[str, bool] | None = None) -> dict:
    office = office or {}
    com_ok = office.get("pywin32") and (office.get("word") or office.get("powerpoint"))
    include = BASE_EXT + (LEGACY_COM_EXT if com_ok else [])
    return {
        "library_roots": [r.as_posix() for r in roots],
        "include_ext": include,
        "exclude_globs": list(DEFAULT_EXCLUDES),
        "max_file_mb": 500,
        "large_library_gb": 50,
        "stale_days": 7,
        "unit_system": unit_system.upper(),
        "workers": max(1, min(4, (os.cpu_count() or 2) // 2)),
        "ocr": {"enabled": bool(tesseract), "tesseract_cmd": tesseract.as_posix() if tesseract else None,
                "dpi": 300, "lang": "eng"},
        "legacy_office": {"enabled": bool(com_ok), "per_file_timeout_s": 120},
        "embeddings": {"enabled": True, "model": "BAAI/bge-small-en-v1.5", "ann": "flat"},
        "rerank": {"enabled": True, "model": "Xenova/ms-marco-MiniLM-L-12-v2", "top_n": 30},
        "tables": {"enabled": True, "max_per_page": 8, "refresh_budget_minutes": 60},
        "classification_path_rules": [dict(r) for r in DEFAULT_CLASSIFICATION_RULES],
        "web": {"max_searches": 15, "max_fetches": 15, "cache": True},
        "protocol_review": {"interval_days": REVIEW_INTERVAL_DAYS, "eval_regression_threshold": 0.05,
                            "correction_trigger_count": 3, "library_change_trigger_pct": 10},
    }


def _write_yaml(g: safeio.Guard, path: Path, data: dict, header: str) -> None:
    safeio.write_text(g, path, header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), setup=True)


def _log(site: Path, record: dict, name: str = "protocol_review_log.jsonl") -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    safeio.append_jsonl(safeio.Guard.for_site(site, ()), site / "logs" / name, rec)


def write_site_notes(site: Path, cfg: dict, surv: dict | None, tess: Path | None, office: dict) -> Path:
    rt = runtime_info()
    lines = [
        f"# SITE_NOTES.md — Site-specific facts (machine: {rt['machine']})", "",
        f"Generated by `erla init` on {date.today().isoformat()} (erla {rt['erla']}). "
        "Regenerate with `erla init --resurvey`. These override generic statements in the core protocols.", "",
        "## Paths", "| Item | Path |", "|---|---|",
        *[f"| Library (READ-ONLY) | `{r}` |" for r in cfg["library_roots"]],
        f"| ERLA site folder | `{site}` |",
        f"| Tesseract | `{tess}` |" if tess else "| Tesseract | not found — OCR disabled |", "",
        "## Installed software",
        f"- Python {rt['python']} on {rt['platform']}",
        f"- PyMuPDF {rt.get('pymupdf')}",
        "- MS Office COM: " + (", ".join(k for k in ("word", "powerpoint", "excel") if office.get(k)) or "not available")
        + (" (legacy .doc/.ppt extraction enabled)" if cfg["legacy_office"]["enabled"] else " — legacy .doc/.ppt NOT indexed"),
        f"- Embeddings: {cfg['embeddings']['model']} (local; downloaded to `{site / 'models'}` on first `erla embed`)", "",
    ]
    if surv:
        lines += [f"## Library composition (measured {date.today().isoformat()})",
                  "| Ext | Files | Size (GB) |", "|---|---|---|",
                  *[f"| .{e} | {v['files']:,} | {v['gb']} |" for e, v in surv["by_ext"].items()],
                  f"| **Indexable total** | **{surv['indexable_files']:,}** | **{surv['indexable_gb']}** |", ""]
        if surv["not_indexed_top"]:
            lines.append("Not indexed (most common other types): " +
                         ", ".join(f".{e} ({n})" for e, n in surv["not_indexed_top"].items()))
        if surv["top_folders"]:
            lines += ["", "Top-level folders: " + ", ".join(surv["top_folders"])]
        if surv["truncated"]:
            lines += ["", "**Survey truncated by time limit — counts are partial.**"]
        lines.append("")
    lines += ["## Classification path rules (G2 — local-only flags)",
              "| Glob | Flag |", "|---|---|",
              *[f"| `{r['glob']}` | {r['flag']} |" for r in cfg["classification_path_rules"]], "",
              "Review these rules before the first index: add folders that hold controlled or customer data.", ""]
    g = safeio.Guard.for_site(site, cfg["library_roots"])
    return safeio.write_text(g, site / "SITE_NOTES.md", "\n".join(lines), setup=True)


def write_review_ledger(site: Path) -> Path:
    p = site / "PROTOCOL_REVIEW.md"
    if p.is_file():
        return p
    today = date.today()
    nxt = today + timedelta(days=REVIEW_INTERVAL_DAYS)
    safeio.write_text(
        safeio.Guard.for_site(site, ()), p,
        "# PROTOCOL_REVIEW.md — Protocol Review Ledger (site)\n\n"
        "Governed by rules_and_guidelines.md §9, instructions.md Step 0 / W6, skills.md S16.\n"
        "Core protocols ship with the ERLA package; this ledger covers this site's layer "
        "(site config, SITE_NOTES, classification rules) and records the core version in use.\n\n"
        "## Status\n\n| Item | Value |\n|---|---|\n"
        f"| Review interval | {REVIEW_INTERVAL_DAYS} days |\n"
        f"| Core protocol version | erla {__version__} |\n"
        f"| Last completed review | {today.isoformat()} (site initialised) |\n"
        f"| **Next scheduled review** | **{nxt.isoformat()}** |\n"
        "| Open event triggers | see `logs/protocol_review_log.jsonl` |\n\n"
        "## Change History\n\n| Date | Trigger | Change | Approved by |\n|---|---|---|---|\n"
        f"| {today.isoformat()} | erla init | Site created | User (install) |\n",
        setup=True)
    return p


# -------------------------------------------------------------- operations
def init_site(site: Path, library_roots: list[str | Path], unit_system: str = "US",
              force: bool = False, survey: bool = True, max_survey_seconds: float = 900) -> dict:
    """Create or refresh a site. Never indexes (indexing is a separate, confirmed step)."""
    site = Path(site).expanduser().resolve()
    roots = _validate_roots(library_roots, site)
    cfg_path = site / "config" / "library_config.yaml"
    if cfg_path.is_file() and not force:
        raise SiteError(f"site already initialised: {cfg_path}. Use --force to rebuild it, "
                        f"or `erla set-library --browse` to change only the library folder.")
    g = safeio.Guard.for_site(site, roots)
    for d in ("config", "logs", "out", "web_cache", "models", "eval"):
        safeio.mkdir(g, site / d, setup=True)
    tess, office = detect_tesseract(), detect_office()
    cfg = build_site_config(site, roots, unit_system, tess, office)
    _write_yaml(safeio.Guard.for_site(site, roots), cfg_path, cfg, f"# ERLA site configuration — generated by erla init {date.today().isoformat()}\n"
                               "# library_roots are READ-ONLY. Edit classification_path_rules to flag controlled folders.\n")
    surv = survey_library(roots, cfg["include_ext"], cfg["exclude_globs"], max_survey_seconds) if survey else None
    notes = write_site_notes(site, cfg, surv, tess, office)
    ledger = write_review_ledger(site)
    golden = site / "eval" / "golden_queries.yaml"
    ex = core_dir() / "eval" / "golden_queries.yaml"
    if not golden.is_file() and ex.is_file():
        safeio.copyfile(g, ex, golden, setup=True)
    _log(site, {"type": "site_init", "roots": cfg["library_roots"], "force": force})
    warn = []
    if surv and surv["indexable_gb"] > cfg["large_library_gb"]:
        warn.append(f"library is {surv['indexable_gb']} GB (> large_library_gb); first index runs automatically; a full re-index needs confirmation (G8.1)")
    if not tess:
        warn.append("Tesseract not found: scanned PDFs will not be OCR'd")
    if not cfg["legacy_office"]["enabled"]:
        warn.append("MS Office COM not available: legacy .doc/.ppt files are excluded")
    return {"site": str(site), "config": str(cfg_path), "site_notes": str(notes), "ledger": str(ledger),
            "library_roots": cfg["library_roots"], "survey": surv, "warnings": warn,
            "next_step": "review classification rules in SITE_NOTES.md, then run: erla index"}


def set_library_roots(site: Path, library_roots: list[str | Path], reason: str = "user request") -> dict:
    """Replace the library roots of an existing site. Opens a protocol-review trigger (rules R9.3)."""
    site = Path(site).expanduser().resolve()
    cfg_path = site / "config" / "library_config.yaml"
    if not cfg_path.is_file():
        raise SiteError(f"site not initialised: {cfg_path}. Run erla init --browse first.")
    roots = _validate_roots(library_roots, site)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    old = list(raw.get("library_roots") or [])
    new = [r.as_posix() for r in roots]
    if [str(o).lower() for o in old] == [n.lower() for n in new]:
        return {"changed": False, "library_roots": new}
    backup = site / "out" / f"protocol_backup_{date.today().isoformat()}"
    safeio.copyfile(safeio.Guard.for_site(site, roots), cfg_path, backup / f"library_config_{int(time.time())}.yaml")
    raw["library_roots"] = new
    text = cfg_path.read_text(encoding="utf-8")
    header = "".join(l + "\n" for l in text.splitlines() if l.startswith("#"))
    _write_yaml(safeio.Guard.for_site(site, roots), cfg_path, raw, header)
    _log(site, {"type": "trigger_open", "trigger": "site_change", "detail": "library_roots changed",
                "old": old, "new": new, "reason": reason})
    return {"changed": True, "old": old, "library_roots": new, "backup": str(backup),
            "next_step": "run erla index (entries for files outside the new roots are dropped from the index; "
                         "library files are never touched), then erla init --resurvey to refresh SITE_NOTES"}


def set_web_allow_terms(site: Path, add: list[str] | None = None, remove: list[str] | None = None,
                        reason: str = "user request") -> dict:
    """Edit ``web.allow_terms`` (G2 web gate): generic technical terms that may be sent to the web
    even though a flagged document is named exactly that. A user decision (L13), so it is a CLI
    command only - no MCP tool can change it. Backs up the config to out/ and opens a
    protocol-review trigger, like a library change."""
    site = Path(site).expanduser().resolve()
    cfg_path = site / "config" / "library_config.yaml"
    if not cfg_path.is_file():
        raise SiteError(f"site not initialised: {cfg_path}. Run erla init --browse first.")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    web_raw = raw.get("web")
    web: dict[str, Any] = web_raw if isinstance(web_raw, dict) else {}
    from .classify import _fold as key    # the gate's own folding, so "Shear_Lag" == "shear lag"
    old = [str(t) for t in web.get("allow_terms") or []]
    gone = {key(t) for t in remove or []}
    new = [t for t in old if key(t) not in gone]
    for t in add or []:
        t = " ".join(t.split())
        if len(t) < 3:
            raise SiteError(f"allow term too short: {t!r}")
        if key(t) not in {key(x) for x in new}:
            new.append(t)
    if new == old:
        return {"changed": False, "allow_terms": new}
    roots = [Path(str(r)) for r in raw.get("library_roots") or []]
    backup = site / "out" / f"protocol_backup_{date.today().isoformat()}"
    safeio.copyfile(safeio.Guard.for_site(site, roots), cfg_path, backup / f"library_config_{int(time.time())}.yaml")
    web["allow_terms"] = new
    raw["web"] = web
    header = "".join(l + "\n" for l in cfg_path.read_text(encoding="utf-8").splitlines() if l.startswith("#"))
    _write_yaml(safeio.Guard.for_site(site, roots), cfg_path, raw, header)
    _log(site, {"type": "trigger_open", "trigger": "site_change", "detail": "web.allow_terms changed",
                "old": old, "new": new, "reason": reason})
    return {"changed": True, "old": old, "allow_terms": new, "backup": str(backup)}


def _roots_of(site: Path) -> list[str]:
    raw = yaml.safe_load((site / "config" / "library_config.yaml").read_text(encoding="utf-8")) or {}
    return [str(r) for r in raw.get("library_roots") or []]


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(str(Path(a).expanduser().resolve())) == os.path.normcase(str(Path(b).expanduser().resolve()))


def add_library_roots(site: Path, new_roots: list[str | Path], reason: str = "user added library folder") -> dict:
    """Add library folder(s). Creates the site on first use. One step; no preview (the user chose the folders)."""
    site = Path(site).expanduser().resolve()
    if not (site / "config" / "library_config.yaml").is_file():
        res = init_site(site, new_roots, survey=True, max_survey_seconds=120)
        return {"created_site": True, "library_roots": res["library_roots"], "survey": res["survey"],
                "warnings": res["warnings"]}
    old = _roots_of(site)
    kept = [r for r in old if Path(r).is_dir()]
    res = set_library_roots(site, kept + [r for r in new_roots if not any(_same(r, k) for k in kept)],
                            reason=reason)
    surv = resurvey(site, max_seconds=120)["survey"] if res["changed"] else None
    return {"created_site": False, "changed": res["changed"], "library_roots": res["library_roots"],
            "dropped_missing": [r for r in old if r not in kept], "survey": surv}


def remove_library_root(site: Path, root: str | Path, reason: str = "user removed library folder") -> dict:
    """Remove one library folder from the site (the folder itself is never touched)."""
    site = Path(site).expanduser().resolve()
    if not (site / "config" / "library_config.yaml").is_file():
        raise SiteError("no library is configured on this computer yet")
    old = _roots_of(site)
    if not any(_same(r, root) for r in old if Path(r).is_dir()) and str(root) not in old:
        raise SiteError(f"{root} is not one of the library folders: {old}")
    rest = [r for r in old if Path(r).is_dir() and not _same(r, root)]
    if not rest:
        raise SiteError("ERLA needs at least one library folder; add the new folder first, then remove this one")
    res = set_library_roots(site, rest, reason=reason)
    return {"changed": res["changed"], "library_roots": res["library_roots"],
            "survey": resurvey(site, max_seconds=120)["survey"]}


def resurvey(site: Path, max_seconds: float = 900) -> dict:
    site = Path(site).expanduser().resolve()
    cfg_path = site / "config" / "library_config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    roots = [Path(r) for r in raw["library_roots"]]
    surv = survey_library(roots, raw.get("include_ext", BASE_EXT), raw.get("exclude_globs", DEFAULT_EXCLUDES),
                          max_seconds)
    tess = Path(raw["ocr"]["tesseract_cmd"]) if (raw.get("ocr") or {}).get("tesseract_cmd") else detect_tesseract()
    cfg = {**build_site_config(site, roots), **raw}
    notes = write_site_notes(site, cfg, surv, tess, detect_office())
    _log(site, {"type": "resurvey", "indexable_files": surv["indexable_files"], "indexable_gb": surv["indexable_gb"]})
    return {"site_notes": str(notes), "survey": surv}


def review_status(site: Path) -> dict:
    """Protocol currency (instructions Step 0): next due date + open triggers."""
    site = Path(site)
    led = site / "PROTOCOL_REVIEW.md"
    status: dict[str, Any] = {"ledger": str(led), "exists": led.is_file(), "next_due": None,
                              "overdue": None, "open_triggers": []}
    if led.is_file():
        import re
        m = re.search(r"Next scheduled review\*?\*?\s*\|\s*\*?\*?(\d{4}-\d{2}-\d{2})", led.read_text(encoding="utf-8"))
        if m:
            status["next_due"] = m.group(1)
            status["overdue"] = date.fromisoformat(m.group(1)) < date.today()
    log = site / "logs" / "protocol_review_log.jsonl"
    if log.is_file():
        opened, closed = [], set()
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") == "trigger_open":
                opened.append(r)
            elif r.get("type") == "review":
                closed.add(r.get("ts", ""))
        last_review = max(closed) if closed else ""
        status["open_triggers"] = [t for t in opened if t.get("ts", "") > last_review]
    return status
