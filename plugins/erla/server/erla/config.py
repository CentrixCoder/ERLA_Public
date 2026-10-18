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
        include_ext={str(e).lower().lstrip(".") for e in raw.get("include_ext", [])},
        exclude_globs=[str(g) for g in raw.get("exclude_globs", [])],
        max_file_mb=float(raw.get("max_file_mb", 500)),
        large_library_gb=float(raw.get("large_library_gb", 50)),
        stale_days=int(raw.get("stale_days", 7)),
        unit_system=str(raw.get("unit_system", "US")).upper(),
        workers=int(raw.get("workers", 4)),
        ocr=OcrConfig(
            enabled=bool(o.get("enabled", False)),
            tesseract_cmd=Path(o["tesseract_cmd"]) if o.get("tesseract_cmd") else None,
            dpi=int(o.get("dpi", 300)),
            lang=str(o.get("lang", "eng")),
            max_pages=int(o.get("max_pages", 0)),
            min_chars_per_page=int(o.get("min_chars_per_page", 50)),
            triage=bool(o.get("triage", True)),
            text_dpi=int(o.get("text_dpi", 200)),
            blank_ink=float(o.get("blank_ink", 0.001)),
            escalate_conf=float(o.get("escalate_conf", 70.0)),
            table_digit_ratio=float(o.get("table_digit_ratio", 0.25)),
            low_conf_numeric=float(o.get("low_conf_numeric", 80.0)),
        ),
        legacy_office=raw.get("legacy_office") or {},
        embeddings=raw.get("embeddings") or {},
        tables=raw.get("tables") or {},
        rerank=raw.get("rerank") or {},
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
    pol = cfg.check.get("policy")
    if pol is not None and pol not in CHECK_POLICIES:
        out.append(f"WARN: check.policy must be one of {', '.join(CHECK_POLICIES)}; got {pol!r}, so 'auto' applies "
                   "(edit it with erla check-policy)")
    if cfg.embeddings.get("enabled") and "<" in str(cfg.embeddings.get("model", "<")):
        out.append("WARN: embeddings enabled but no model chosen")
    return out
