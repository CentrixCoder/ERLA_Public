"""Per-document metadata overrides (protocol review 2026-11-25, F13).

Some documents index with a weak title ("1", a layout file name), a wrong year, or a tier the path and
title rules cannot reach (a textbook with no recognisable title). The owner corrects them in the site file
``<site>/config/doc_overrides.yaml``; ERLA never writes it (the site configuration is write-guarded):

    documents:
      "Textbooks/Trahair/Flexural-Torsional Buckling.pdf":   # the document's path inside its library folder
        title: Flexural-Torsional Buckling of Structures
        year: 1993
        tier: T2
        reason: the 1st edition is 1993 (title page); protocol review 2026-11-25 F13

`reason` is required. `title`, `year` and `tier` are each optional. The indexer applies an entry whenever it
writes the document, and ``erla reclassify --overrides-only`` applies the file to an existing index without
re-applying the tier rules to any other document. An entry that does not validate is skipped and reported,
never half-applied. Overrides never touch a classification flag (G2.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

FILE = "doc_overrides.yaml"
FIELDS = ("title", "year", "tier")
TIERS = ("T1", "T2", "T3", "T4", "T5", "T6")
YEAR_RANGE = (1800, 2100)
TITLE_MAX = 400


def key(rel_path: str) -> str:
    """Match key for a library-relative path: separators and letter case do not matter (Windows paths)."""
    return str(rel_path).replace("\\", "/").strip().strip("/").casefold()


@dataclass(frozen=True)
class Overrides:
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    path: str = ""

    def get(self, rel_path: str | None) -> dict[str, Any]:
        return self.entries.get(key(rel_path), {}) if rel_path else {}

    def apply(self, rel_path: str | None, title: Any, year: Any, tier: Any) -> tuple[Any, Any, Any]:
        """(title, year, tier) with this document's override, if any, in place of the indexed values."""
        e = self.get(rel_path)
        return e.get("title", title), e.get("year", year), e.get("tier", tier)


def _check(rel: Any, e: Any) -> tuple[dict[str, Any] | None, list[str]]:
    where = f"{FILE}: {rel!r}"
    if not isinstance(rel, str) or not rel.strip():
        return None, [f"{FILE}: an entry's key must be the document's path inside its library folder"]
    if not isinstance(e, dict):
        return None, [f"{where}: give a mapping of title / year / tier and a reason"]
    bad = [f"{where}: unknown field {k!r} (title, year, tier, reason)" for k in e if k not in (*FIELDS, "reason")]
    if not isinstance(e.get("reason"), str) or not e["reason"].strip():
        bad.append(f"{where}: a reason is required (why the indexed value is wrong, and the source)")
    out: dict[str, Any] = {}
    if "title" in e:
        t = e["title"]
        if not isinstance(t, str) or not t.strip() or len(t) > TITLE_MAX:
            bad.append(f"{where}: title must be text of 1 to {TITLE_MAX} characters")
        else:
            out["title"] = t.strip()
    if "year" in e:
        y = e["year"]
        if isinstance(y, bool) or not isinstance(y, int) or not YEAR_RANGE[0] <= y <= YEAR_RANGE[1]:
            bad.append(f"{where}: year must be a whole year between {YEAR_RANGE[0]} and {YEAR_RANGE[1]}")
        else:
            out["year"] = y
    if "tier" in e:
        if e["tier"] not in TIERS:
            bad.append(f"{where}: tier must be one of {', '.join(TIERS)}")
        else:
            out["tier"] = e["tier"]
    if not bad and not out:
        bad.append(f"{where}: sets none of title, year, tier")
    return (None if bad else out), bad


@lru_cache(maxsize=4)
def _parse(path: str, mtime_ns: int) -> Overrides:     # mtime in the key: an edited file is read again
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return Overrides(problems=(f"{FILE}: not read ({exc}); no override applied",), path=path)
    docs = raw.get("documents") if isinstance(raw, dict) else None
    if not isinstance(docs, dict):
        return Overrides(problems=(f"{FILE}: expected a top-level 'documents:' mapping; no override applied",),
                         path=path)
    entries: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for rel, e in docs.items():
        ok, bad = _check(rel, e)
        problems += bad
        if ok is None:
            continue
        k = key(rel)
        if k in entries:
            problems.append(f"{FILE}: {rel!r} is listed twice; the first entry is kept")
            continue
        entries[k] = ok
    return Overrides(entries=entries, problems=tuple(problems), path=path)


def load(agent_root: Path) -> Overrides:
    """The site's overrides; empty when the site has no override file."""
    p = Path(agent_root) / "config" / FILE
    try:
        st = p.stat()
    except OSError:
        return Overrides()
    return _parse(str(p), st.st_mtime_ns)
