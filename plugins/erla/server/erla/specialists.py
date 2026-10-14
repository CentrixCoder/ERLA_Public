"""Specialist sub-agents (S17): lessons ledger and library-addition triggers.

ERLA is the lead agent; discipline specialists (the damage-tolerance agent ``dt-agent``, specialist key ``dt``, and the joints
agent ``joint-agent``, key ``joint``) are workers it delegates to. Their skill files are protocol files (precedence below skills.md) and
change only through the protocol review (rules section 9) after the user approves each change.
This module records the two events that make a specialist skill file due for review:

* a lesson (a mistake, a user correction, a method clarification) - appended to
  ``logs/lessons.jsonl`` and opened as a ``specialist_lesson`` trigger;
* new library documents on the specialist's topic found by a refresh - opened as a
  ``specialist_library_additions`` trigger.

Both are ``logs/`` writes (G1.2), so no approval is needed to record them; the triggers surface
in ``protocol_status`` / ``site_info`` until the next recorded review.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import safeio
from .config import Config, protocol_dir

SPECIALISTS: dict[str, dict[str, Any]] = {
    "dt": {
        "name": "Damage Tolerance",
        "agent": "dt-agent",          # plugin agent name (the Claude app refuses names under 3 characters)
        "skills": "dt_skills.md",
        "calc": "dtcalc", "tool": "dt_calc",
        "terms": re.compile(
            r"damage[\s-]*toleran|fracture[\s-]*mechanic|fracture[\s-]*toughness|fracture[\s-]*control|"
            r"crack[\s-]*(?:growth|propagation|closure|arrest|retardation)|fatigue[\s-]*crack|"
            r"stress[\s-]*intensity|da\s*/\s*dn|residual[\s-]*strength|j[\s-]*integral|\bctod\b|"
            r"fail[\s-]*safe|widespread[\s-]*fatigue|multiple[\s-]*site[\s-]*damage|leak[\s-]*before[\s-]*burst|"
            r"probability[\s-]*of[\s-]*detection|inspection[\s-]*interval|\bnasgro\b|\bafgrow\b|\bfastran\b|"
            r"\bk_?ic\b|\bk_?iscc\b|r[\s-]*curve|weight[\s-]*function", re.I),
    },
    "joint": {
        "name": "Joints",
        "agent": "joint-agent",
        "skills": "joint_skills.md",
        "calc": "jointcalc", "tool": "joint_calc",
        "terms": re.compile(
            r"(?:bolted|riveted|fastened|welded|bonded|adhesive|mechanical|composite|lap|butt|pin)[\s-]*joints?\b|"
            r"\bfasteners?\b|\brivets?\b|\blugs?\b|\bweld(?:s|ed|ing|ment)?\b|preload|torque[\s-]*tension|"
            r"bearing[\s-]*bypass|fretting|faying[\s-]*surface|thread(?:ed)?[\s-]*(?:fastener|joint|engagement)|"
            r"shear[\s-]*out|pull[\s-]*through|splice|galvanic|adhesively|bond[\s-]*line|scarf", re.I),
    },
}
LESSON_KINDS = ("mistake", "correction", "method", "resource")
MAX_LISTED = 25


def installed() -> dict[str, dict[str, Any]]:
    """Specialists present in this install: skill file served and calc module shipped. The free core
    edition (packaging/build.py --edition core) ships neither, so ERLA runs without delegation."""
    from importlib.util import find_spec
    pdir = protocol_dir()
    return {k: s for k, s in SPECIALISTS.items()
            if (pdir / s["skills"]).is_file() and find_spec(f"{__package__}.{s['calc']}") is not None}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_lesson(cfg: Config, specialist: str, kind: str, lesson: str, evidence: str = "",
                  rule: str = "") -> dict[str, Any]:
    """Append a lesson to ``logs/lessons.jsonl`` and open a review trigger for the specialist."""
    if specialist not in installed():
        return {"error": f"unknown specialist {specialist!r}; installed: {sorted(installed())}"}
    if kind not in LESSON_KINDS:
        return {"error": f"kind must be one of {list(LESSON_KINDS)}"}
    if not lesson.strip():
        return {"error": "lesson text is empty"}
    ts = now()
    rec = {"ts": ts, "specialist": specialist, "kind": kind, "lesson": lesson.strip(),
           "evidence": evidence.strip(), "rule": rule.strip()}
    p = safeio.append_jsonl(cfg, cfg.log_dir / "lessons.jsonl", rec)
    safeio.append_jsonl(cfg, cfg.log_dir / "protocol_review_log.jsonl", {
        "ts": ts, "type": "trigger_open", "trigger": "specialist_lesson", "specialist": specialist,
        "file": SPECIALISTS[specialist]["skills"], "detail": f"{kind}: {lesson.strip()[:200]}"})
    return {"logged_to": str(p), "trigger": "specialist_lesson", "skills_file": SPECIALISTS[specialist]["skills"]}


def sha_snapshot(con: sqlite3.Connection) -> set[str]:
    """Content hashes in the index before a run (moves keep their hash and are not additions)."""
    return {r[0] for r in con.execute("SELECT sha256 FROM documents WHERE sha256 IS NOT NULL")}


def topic_additions(con: sqlite3.Connection, before: set[str], since: str) -> dict[str, list[dict[str, Any]]]:
    """Documents indexed at/after ``since`` whose content hash is new, matched to specialist topics
    on title, library-relative path and the first two chunks."""
    rows = con.execute(
        "SELECT doc_id, title, rel_path, sha256, classification_flag FROM documents "
        "WHERE indexed_at >= ? AND COALESCE(extract_status, '') != 'duplicate'", (since,)).fetchall()
    found: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r[3] in before:
            continue
        head = " ".join(t or "" for (t,) in con.execute(
            "SELECT text FROM chunks WHERE doc_id=? ORDER BY seq LIMIT 2", (r[0],)))
        hay = f"{r[1] or ''}\n{r[2] or ''}\n{head[:6000]}"
        for key, spec in installed().items():
            if spec["terms"].search(hay):
                found.setdefault(key, []).append(
                    {"doc_id": r[0], "title": r[1], "rel_path": r[2], "flag": r[4]})
    return found


def open_addition_triggers(cfg: Config, found: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """One ``specialist_library_additions`` trigger per specialist with new topic documents."""
    counts: dict[str, int] = {}
    for key, docs in found.items():
        if not docs:
            continue
        safeio.append_jsonl(cfg, cfg.log_dir / "protocol_review_log.jsonl", {
            "ts": now(), "type": "trigger_open", "trigger": "specialist_library_additions",
            "specialist": key, "file": SPECIALISTS[key]["skills"], "count": len(docs),
            "docs": docs[:MAX_LISTED], "detail": f"{len(docs)} new {SPECIALISTS[key]['name']} document(s) indexed"})
        counts[key] = len(docs)
    return counts
