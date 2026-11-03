"""Configuration loading, validation, and library read-only protection.

Two layers:

* **Core** (same on every machine, versioned with the package): protocol ``.md``
  files and ``source_authority.yaml`` / ``synonyms.yaml`` defaults. Packaged
  builds carry them in ``erla/core/``; a source checkout uses the repo root.
* **Site** (one per machine): ``<site>/config/library_config.yaml`` (library
  roots, classification rules, tool paths), plus the index, logs, ``out/``,
  ``web_cache/``, models, ``SITE_NOTES.md``. A site may override the core YAML
  files by placing its own copy in ``<site>/config/``.

Site directory (``AGENT_ROOT``) resolution order:
  1. ``ERLA_ROOT`` environment variable
  2. the source checkout, if it holds ``config/library_config.yaml`` (development site)
  3. the per-user default: ``%LOCALAPPDATA%\\ERLA`` (Windows) or
     ``~/.local/share/erla``

YAML paths use forward slashes and are resolved to absolute paths.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent


def default_site_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ERLA"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "erla"


def resolve_agent_root() -> Path:
    env = os.environ.get("ERLA_ROOT")
    if env:
        return Path(env).expanduser()
    if (REPO_ROOT / "config" / "library_config.yaml").is_file():
        return REPO_ROOT
    return default_site_dir()


def core_dir() -> Path:
    """Directory holding the core layer (``protocols/`` and ``config/``)."""
    packaged = PACKAGE_DIR / "core"
    return packaged if packaged.is_dir() else REPO_ROOT


def protocol_dir() -> Path:
    c = core_dir()
    return c / "protocols" if (c / "protocols").is_dir() else c


def config_file(name: str, root: Path | None = None) -> Path:
    """Site override first (``<site>/config/<name>``), else the core copy."""
    site = (root or AGENT_ROOT) / "config" / name
    if site.is_file():
        return site
    core = core_dir() / "config" / name
    if core.is_file():
        return core
    tmpl = REPO_ROOT / "packaging" / "core_templates" / name    # source checkout without a site config
    return tmpl if tmpl.is_file() else core


AGENT_ROOT = resolve_agent_root()
DEFAULT_CONFIG = AGENT_ROOT / "config" / "library_config.yaml"


SETUP_HINT = ('Choose the library folder with: erla init --browse   (folder dialog)\n'
              '                           or: erla init --library "<library folder>"')
CHANGE_HINT = ('Change the library folder with: erla set-library --browse   (folder dialog)\n'
               '                            or: erla set-library --library "<library folder>"')
# When the Professor (independent checker, ADR-0002) must vet a Result Packet. The user's decision (L13):
# set only with ``erla check-policy`` (or the panel), never by an MCP tool. An unknown value falls back to
# 'auto', never to 'off' (fail closed).
CHECK_POLICIES = ("auto", "always", "off")


def check_policy(cfg: Config) -> str:
    pol = cfg.check.get("policy")
    return pol if pol in CHECK_POLICIES else "auto"


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


class ConfigFormatError(ConfigError):
    """The site config exists but a key or value in it is malformed (an unknown key, a section that is not a
    mapping, a number that is not one). Not "no library on this computer": callers that fall back to a
    no-library mode on ConfigError (the web gate) must not do so for this one."""


# Every top-level key of library_config.yaml: the ones load_config reads, and protocol_review (written by
# `erla init`, read by the protocol review). Loading rejects any other key: `unit_sytem: SI` loaded silently
# and left the site on US units (2026-11-02).
SECTIONS = ("ocr", "legacy_office", "embeddings", "tables", "rerank", "search", "sparse", "refs", "web", "check",
            "protocol_review")
KNOWN_KEYS = frozenset({"library_roots", "agent_root", "index_path", "out_dir", "log_dir", "web_cache_dir",
                        "include_ext", "exclude_globs", "max_file_mb", "large_library_gb", "stale_days",
                        "unit_system", "workers", "classification_path_rules", *SECTIONS})


def _check_keys(raw: dict[str, Any], cfg_path: Path) -> None:
    import difflib
    problems = []
    for key in raw:
        if key in KNOWN_KEYS:
            continue
        near = difflib.get_close_matches(str(key), sorted(KNOWN_KEYS), n=1)
        problems.append(f"unknown setting '{key}'" + (f" (did you mean '{near[0]}'?)" if near else ""))
    for key in SECTIONS:
        value = raw.get(key)
        if value is not None and not isinstance(value, dict):
            problems.append(f"'{key}' must be a section of settings (key: value lines), got {value!r}")
    if problems:
        raise ConfigFormatError(f"{'; '.join(problems)} in {cfg_path}: fix or remove it")


def _number(kind: type[int] | type[float], value: Any, key: str, cfg_path: Path) -> Any:
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise ConfigFormatError(f"'{key}' must be a number, got {value!r} in {cfg_path}") from None


class ReadOnlyViolation(PermissionError):
    """Raised when code attempts to write inside a library root (guardrail G1.1)."""


@dataclass
class OcrConfig:
    enabled: bool = False
    tesseract_cmd: Path | None = None
    dpi: int = 300
    lang: str = "eng"
    max_pages: int = 0          # 0 = no limit per document
    min_chars_per_page: int = 50
    # S2 page triage: blank-page skip, fast text pass, high-res table/low-confidence escalation
    triage: bool = True
    text_dpi: int = 200         # first pass; `dpi` is the escalation resolution
    blank_ink: float = 0.001    # dark-pixel fraction (36-dpi thumbnail) below which a page is blank
    escalate_conf: float = 70.0 # mean Tesseract word confidence below which the page is re-run at `dpi`
    table_digit_ratio: float = 0.25  # digit fraction of non-space chars above which a page is a table
    low_conf_numeric: float = 80.0   # A5: numeric OCR tokens below this word confidence are flagged


# Formats ERLA learned to read after sites were set up. A site's include_ext lists the formats of the
# release that created it, so these are added on every site (0.5.6: EPUB); opt out with an exclude glob
# such as "**/*.epub".
ADDED_EXT = frozenset({"epub"})


def include_extensions(listed: Any) -> set[str]:
    """A site's include_ext plus ADDED_EXT (an empty list stays empty: validate_config reports it)."""
    exts = {str(e).lower().lstrip(".") for e in (listed or [])}
    return exts | ADDED_EXT if exts else exts


@dataclass
class Config:
    library_roots: list[Path]
    agent_root: Path
    index_path: Path
    out_dir: Path
    log_dir: Path
    web_cache_dir: Path
    include_ext: set[str]
    exclude_globs: list[str]
    max_file_mb: float = 500
    large_library_gb: float = 50
    stale_days: int = 7
    unit_system: str = "US"
    workers: int = 4
    ocr: OcrConfig = field(default_factory=OcrConfig)
    legacy_office: dict[str, Any] = field(default_factory=dict)
    embeddings: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, Any] = field(default_factory=dict)     # A2/A3: PDF table extraction, property store
    rerank: dict[str, Any] = field(default_factory=dict)     # A1: cross-encoder re-ranking
    search: dict[str, Any] = field(default_factory=dict)     # 0.7.0: fusion, aggregation, diversity, profiles, evidence
    sparse: dict[str, Any] = field(default_factory=dict)     # 0.7.0: opt-in learned sparse channel (erla.sparse)
    refs: dict[str, Any] = field(default_factory=dict)       # 0.7.0: citation graph: site book names and spec patterns
    classification_path_rules: list[dict[str, str]] = field(default_factory=list)
    web: dict[str, Any] = field(default_factory=dict)
    check: dict[str, Any] = field(default_factory=dict)      # Professor check policy (ADR-0002; erla check-policy)
    source_path: Path | None = None

    # ------------------------------------------------------------------ filters
    @staticmethod
    def _posix(path: Path | str) -> str:
        return "/" + Path(path).as_posix().lstrip("/")

    def is_excluded(self, path: Path | str) -> bool:
        """True if ``path`` matches any exclude glob (case-insensitive)."""
        p = self._posix(path).lower()
        return any(fnmatch.fnmatch(p, g.lower()) for g in self.exclude_globs)

    def is_included(self, path: Path | str) -> bool:
        """True if the extension is indexable and the path is not excluded."""
        ext = Path(path).suffix.lower().lstrip(".")
        return ext in self.include_ext and not self.is_excluded(path)

    def path_flag(self, path: Path | str) -> str | None:
        """Classification flag from path rules (guardrail G2), else None."""
        p = self._posix(path).lower()
        for rule in self.classification_path_rules:
            if fnmatch.fnmatch(p, str(rule.get("glob", "")).lower()):
                return str(rule.get("flag", "PROPRIETARY")).upper()
        return None

    # --------------------------------------------------------------- guardrail
    def in_library(self, path: Path | str) -> bool:
        target = Path(path).resolve()
        for root in self.library_roots:
            try:
                target.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    def writable(self, path: Path | str) -> Path:
        """Guardrails G1.1/G1.2: return the resolved path if it is a G1.2 write location
        (out/, logs/, web_cache/, models/, the index and vector files - see erla.safeio),
        else raise ReadOnlyViolation."""
        from . import safeio
        return safeio.check(self, path)

    assert_writable = writable   # name used before 0.2.2


def _path(value: Any, name: str) -> Path:
    if not value:
        raise ConfigError(f"'{name}' is required")
    return Path(str(value)).expanduser()


def load_config(path: Path | str | None = None, validate: bool = True) -> Config:
    """Load ``library_config.yaml`` and return a :class:`Config`."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_file():
        raise ConfigError(f"No library configured on this computer (no {cfg_path}).\n{SETUP_HINT}")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigFormatError(f"{cfg_path} must hold settings (key: value lines), got {type(raw).__name__}")
    _check_keys(raw, cfg_path)

    roots = raw.get("library_roots") or []
    if not isinstance(roots, list) or not roots:
        raise ConfigError(f"No library configured: 'library_roots' is empty in {cfg_path}.\n{CHANGE_HINT}")

    # Site dir defaults to the folder that holds config/library_config.yaml.
    site_default = cfg_path.resolve().parent.parent if cfg_path.parent.name == "config" else AGENT_ROOT
    agent_root = _path(raw.get("agent_root", site_default), "agent_root")
    o = raw.get("ocr") or {}
    cfg = Config(
        library_roots=[Path(str(r)) for r in roots],
        agent_root=agent_root,
        index_path=_path(raw.get("index_path", agent_root / "index.db"), "index_path"),
        out_dir=_path(raw.get("out_dir", agent_root / "out"), "out_dir"),
        log_dir=_path(raw.get("log_dir", agent_root / "logs"), "log_dir"),
        web_cache_dir=_path(raw.get("web_cache_dir", agent_root / "web_cache"), "web_cache_dir"),
        include_ext=include_extensions(raw.get("include_ext")),
        exclude_globs=[str(g) for g in raw.get("exclude_globs", [])],
        max_file_mb=_number(float, raw.get("max_file_mb", 500), "max_file_mb", cfg_path),
        large_library_gb=_number(float, raw.get("large_library_gb", 50), "large_library_gb", cfg_path),
        stale_days=_number(int, raw.get("stale_days", 7), "stale_days", cfg_path),
        unit_system=str(raw.get("unit_system", "US")).upper(),
        workers=_number(int, raw.get("workers", 4), "workers", cfg_path),
        ocr=OcrConfig(
            enabled=bool(o.get("enabled", False)),
            tesseract_cmd=Path(o["tesseract_cmd"]) if o.get("tesseract_cmd") else None,
            dpi=_number(int, o.get("dpi", 300), "ocr.dpi", cfg_path),
            lang=str(o.get("lang", "eng")),
            max_pages=_number(int, o.get("max_pages", 0), "ocr.max_pages", cfg_path),
            min_chars_per_page=_number(int, o.get("min_chars_per_page", 50), "ocr.min_chars_per_page", cfg_path),
            triage=bool(o.get("triage", True)),
            text_dpi=_number(int, o.get("text_dpi", 200), "ocr.text_dpi", cfg_path),
            blank_ink=_number(float, o.get("blank_ink", 0.001), "ocr.blank_ink", cfg_path),
            escalate_conf=_number(float, o.get("escalate_conf", 70.0), "ocr.escalate_conf", cfg_path),
            table_digit_ratio=_number(float, o.get("table_digit_ratio", 0.25), "ocr.table_digit_ratio", cfg_path),
            low_conf_numeric=_number(float, o.get("low_conf_numeric", 80.0), "ocr.low_conf_numeric", cfg_path),
        ),
        legacy_office=raw.get("legacy_office") or {},
        embeddings=raw.get("embeddings") or {},
        tables=raw.get("tables") or {},
        rerank=raw.get("rerank") or {},
        search=raw.get("search") or {},
        sparse=raw.get("sparse") or {},
        refs=raw.get("refs") or {},
        classification_path_rules=raw.get("classification_path_rules") or [],
        web=raw.get("web") or {},
        check=chk if isinstance(chk := raw.get("check"), dict) else {},
        source_path=cfg_path,
    )
    if validate:
        errors = [p for p in validate_config(cfg) if p.startswith("ERROR")]
        if errors:
            hint = f"\n{CHANGE_HINT}" if any("library root" in e for e in errors) else ""
            raise ConfigError("; ".join(errors) + hint)
    return cfg


def validate_config(cfg: Config) -> list[str]:
    """Return a list of 'ERROR: ...' / 'WARN: ...' strings."""
    out: list[str] = []
    for root in cfg.library_roots:
        if not root.is_dir():
            out.append(f"ERROR: library root not found: {root}")
    if not cfg.include_ext:
        out.append("ERROR: include_ext is empty")
    if cfg.unit_system not in {"US", "SI"}:
        out.append(f"ERROR: unit_system must be US or SI, got {cfg.unit_system}")
    for d in (cfg.index_path.parent, cfg.out_dir, cfg.log_dir, cfg.web_cache_dir):
        if cfg.in_library(d):
            out.append(f"ERROR: writable location is inside the library: {d}")
    if cfg.ocr.enabled and (not cfg.ocr.tesseract_cmd or not cfg.ocr.tesseract_cmd.is_file()):
        out.append(f"WARN: OCR enabled but tesseract not found at {cfg.ocr.tesseract_cmd}")
    allow = cfg.web.get("allow_terms")
    if allow is not None and not (isinstance(allow, list) and all(isinstance(t, str) for t in allow)):
        out.append("WARN: web.allow_terms must be a list of terms; it is ignored (edit it with erla web-allow)")
    for cap in ("max_searches", "max_fetches"):        # G8.2: a malformed cap is ignored, so 15 applies
        v = cfg.web.get(cap)
        if v is not None and not (type(v) is int and v >= 1):
            out.append(f"WARN: web.{cap} must be a whole number of at least 1; got {v!r}, so 15 applies")
    pol = cfg.check.get("policy")
    if pol is not None and pol not in CHECK_POLICIES:
        out.append(f"WARN: check.policy must be one of {', '.join(CHECK_POLICIES)}; got {pol!r}, so 'auto' applies "
                   "(edit it with erla check-policy)")
    if cfg.embeddings.get("enabled") and "<" in str(cfg.embeddings.get("model", "<")):
        out.append("WARN: embeddings enabled but no model chosen")
    return out
