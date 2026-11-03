"""The verification gate for answers shown in the chat (G3; owner decision 2026-11-02). Standard library only.

``erla.packetgate`` gates what ``log_request`` and ``export_packet`` record; it cannot see the answer the agent
writes in the chat. This module is that check. The plugin's Stop and SubagentStop hooks (``scripts/answergate.py``)
call ``decide(hook_input)`` when the lead agent ends a turn, with its final text (``last_assistant_message``) and
the session transcript. The transcript, written by Claude Code and not by the model, holds every ``verify_packet``
call of the session and its result, so the gate rebuilds what was verified without the server.

An answer is in scope when its session used an ERLA library or calculation tool or delegated to the ERLA agent, or
when it is shaped like a Result Packet (sections ``A.`` to ``G.``), and always for the ERLA agent itself
(SubagentStop). Ordinary chats pass at once. In scope, each reported number must match a number a PASS or COMPUTED
item of a ``verify_packet`` result in the session vouches for (its value, a conversion's source value, the thickness
or chart x it was checked at; never the numbers of a quote), or the factor of a ``unit_convert`` result; a number
written with fewer decimals matches its rounded value. Reported numbers are, as ``packetgate.check_export`` reads a
packet:

* in a Result Packet, every number in a Value cell of section B, and in section A every number written with units
  after it (``73 ksi``, ``503 MPa``, ``0.83 in``) or after ``=``, ``≈`` or ``~`` (``Kt = 1.62``); sections C to G
  (sources, conflicts, gaps, anomalies, the check) are not the answer;
* in an answer without packet sections, the same prose numbers anywhere, and the Value column of any table that
  has one. Working shown in the answer is reported: verify it, or label its line NOT VERIFIED.

Exempt: a number in a user request of the session (an input, not a reported value; never the gate's own feedback), a number on a line or table
row that says NOT VERIFIED (rules: a FAIL value may be reported so), page, table, figure, section, equation,
revision and document numbers, and an answer that starts NOT FOUND or UNRESOLVED.

A Stop hook cannot take back text already on the screen. On a miss the hook blocks the stop once, so the agent
verifies the values or restates them as NOT VERIFIED before the turn ends. When the turn is already a continuation
(``stop_hook_active``) and values are still uncovered, the turn ends with a warning shown to the user listing them,
rather than looping (Claude Code also caps stop-hook continuations). Limits (documented, not silent): a reported
number with no units and no ``=`` in front of it is not recognised in prose; a number that also appears in the
user's requests in the session is taken as an input; index freshness is not checked here (``packetgate`` checks it
for recorded packets); the desktop extension has no hooks, so this gate runs only in the Claude Code plugin.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any, cast

# Same patterns as erla.verify.NUM, erla.packetgate.SECTION / NOT_VERIFIED / NO_ANSWER (tests/test_answergate.py
# checks they stay equal): this file is loaded by path from the hook, so it cannot import them.
NUM = re.compile(r"(?<![\w.])([-−–]?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d*|\.\d+|\d+)"
                 r"(?:[eE]([-+]?\d+))?(?!\d)")
SECTION = re.compile(r"^#{2,4}[ \t]*([A-G])\.[^\n]*$", re.M)
NOT_VERIFIED = re.compile(r"\bNOT\s+VERIFIED\b", re.I)
NO_ANSWER = re.compile(r"^\s*(?:\*\*)?\s*(?:NOT FOUND|UNRESOLVED)\b", re.I)
UNVERIFIED_LINE = re.compile(r"\bNOT\s+VERIFIED\b|\bUNVERIFIED\b", re.I)

COVERS = ("PASS", "COMPUTED")
# what a passed value item vouches for: its value, a conversion's source value, and the conditions the value was
# checked against (a thickness band, a chart's x). Never a quote's numbers: a quote passes when its text is on the
# page, so a quoted "61" would stand in for the row and column check (packetgate counts no quote numbers either)
VERIFIED_FIELDS = ("value", "source_value", "thickness", "x")
ERLA_TOOL = re.compile(r"(?:^|__)plugin_erla_erla__(?:library_show|library_search|library_properties|chart_read|"
                       r"verify_packet|[a-z]+_calc)$")
ERLA_AGENT = "erla:erla"
VERIFY = re.compile(r"(?:^|__)plugin_erla_erla__verify_packet$")
CONVERT = re.compile(r"(?:^|__)plugin_erla_erla__unit_convert$")
GATE_TAG = "ERLA answer gate (G3)"     # starts every message the gate sends; its own feedback is never an input

UNIT = re.compile(
    r"[ \t ]?(?:ksi|psi|msi|mpa|gpa|kpa|n/mm(?:2|²|\^2)|kips?|lbf|lbs?|kn|mn|n|mm|cm|µm|um|"
    r"in(?:ch(?:es)?)?|ft|m|°[ \t]?[fck]|deg[ \t]?[fc]|%|g/cm(?:3|³|\^3)|g/cc|lb/in(?:3|³|\^3)|kg/m(?:3|³|\^3)|"
    r"cycles)(?![a-z0-9])",
    re.I)
NOT_UNIT_IN = re.compile(r"[ \t ]?in[ \t]+(?:the|a|an|this|that|these|those|each|which|its|their|our|your|"
                         r"table|fig|figure|section|ref|eq)\b", re.I)
ASSIGNED = re.compile(r"(?:=|≈|≅|~)[ \t]*$")
LOCATOR_BEFORE = re.compile(r"(?:\bp|\bpp|\bpage|\btable|\bfig|\bfigure|\bsection|\bsec|§|\beq|\bequation|\brev|"
                            r"\bref|\bdoc(?:_id)?|\bchapter|\bch|\bno|#|\bstep|\bitem|\bv)[ \t.:]*$", re.I)

MAX_LISTED = 8


def numbers(text: str) -> list[tuple[float, int, int, str]]:
    """Numbers in ``text`` as (value, start, end, as written), the way erla.verify.numbers reads them."""
    out: list[tuple[float, int, int, str]] = []
    for m in NUM.finditer(text):
        sign, digits, exp = m.group(1), m.group(2).replace(",", ""), m.group(3)
        try:
            v = float(digits + (f"e{exp}" if exp else ""))
        except ValueError:
            continue
        out.append((-v if sign else v, m.start(), m.end(), m.group(0)))
    return out


def _decimals(written: str) -> int:
    s = written.split("e")[0].split("E")[0]
    return len(s.split(".", 1)[1]) if "." in s else 0


def _same(written: str, num: float, covered: Iterable[float]) -> bool:
    places = _decimals(written)
    for c in covered:
        if math.isclose(num, c, rel_tol=1e-9, abs_tol=1e-12):
            return True
        if places < 6 and math.isclose(round(c, places), num, rel_tol=1e-9, abs_tol=1e-12):
            return True                     # written with fewer decimals than verified: its rounded value
    return False


# --- the transcript -------------------------------------------------------------------------------------------

def _dict(v: object) -> dict[str, Any]:
    return cast(dict[str, Any], v) if isinstance(v, dict) else {}


def _list(v: object) -> list[Any]:
    return cast(list[Any], v) if isinstance(v, list) else []


def _read(path: str | None, needles: tuple[str, ...]) -> list[dict[str, Any]]:
    """Transcript entries whose line holds one of ``needles`` (JSON-parsed only then: transcripts can be large)."""
    if not path or not os.path.isfile(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if needles and not any(n in line for n in needles):
                continue
            try:
                ev = _dict(json.loads(line))
            except ValueError:
                continue
            if ev:
                out.append(ev)
    return out


def _blocks(ev: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [d for d in map(_dict, _list(_dict(ev.get("message")).get("content"))) if d]


def _user_text(ev: Mapping[str, Any]) -> str:
    """The text of a user entry that is a request (not a tool result)."""
    if ev.get("type") != "user":
        return ""
    content = _dict(ev.get("message")).get("content")
    if isinstance(content, str):
        return content
    blocks = _blocks(ev)
    if any(b.get("type") == "tool_result" for b in blocks):
        return ""
    return "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")


def _result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(_dict(c).get("text") or "") for c in _list(content))


def verified_numbers(entries: Iterable[Mapping[str, Any]]) -> list[float]:
    """Every number a PASS or COMPUTED item of the transcript's verify_packet results vouches for
    (``VERIFIED_FIELDS``), and the factor of each unit_convert result (a constant the tested tool applied, R4.6)."""
    entries = list(entries)
    calls: dict[str, list[Any]] = {}
    factors: set[str] = set()
    for ev in entries:
        for b in _blocks(ev):
            if b.get("type") != "tool_use":
                continue
            name = str(b.get("name") or "")
            if VERIFY.search(name):
                calls[str(b.get("id"))] = _list(_dict(b.get("input")).get("items"))
            elif CONVERT.search(name):
                factors.add(str(b.get("id")))
    out: list[float] = []
    for ev in entries:
        for b in _blocks(ev):
            tid = str(b.get("tool_use_id"))
            if b.get("type") != "tool_result" or tid not in calls and tid not in factors or b.get("is_error"):
                continue
            try:
                res = _dict(json.loads(_result_text(b)))
            except ValueError:
                continue
            if tid in factors:
                f = res.get("factor")
                out += [n for n, *_ in numbers(str(f))] if isinstance(f, (int, float)) else []
                continue
            asked = calls[tid]
            for k, raw in enumerate(_list(res.get("items"))):
                item = _dict(raw)
                if item.get("status") not in COVERS or item.get("kind") == "quote":
                    continue
                given = _dict(asked[k]) if k < len(asked) else {}
                for field in VERIFIED_FIELDS:
                    for v in (item.get(field), given.get(field)):
                        if v not in (None, ""):
                            out += [n for n, *_ in numbers(str(v))]
    return out


def _uses_erla(entries: Iterable[Mapping[str, Any]]) -> bool:
    for ev in entries:
        for b in _blocks(ev):
            if b.get("type") != "tool_use":
                continue
            name = str(b.get("name") or "")
            inp = _dict(b.get("input"))
            if ERLA_TOOL.search(name):
                return True
            if name in ("Agent", "Task") and str(inp.get("subagent_type") or "") == ERLA_AGENT:
                return True
    return False


# --- the answer -----------------------------------------------------------------------------------------------

def _sections(text: str) -> dict[str, str]:
    marks = list(SECTION.finditer(text))
    out: dict[str, str] = {}
    for m, nxt in zip(marks, [*marks[1:], None]):
        out[m.group(1)] = out.get(m.group(1), "") + text[m.end():nxt.start() if nxt else len(text)]
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _value_cells(text: str, section_b: bool) -> list[str]:
    """Value cells of the tables in ``text`` (rows marked NOT VERIFIED left out). Section B's table has its Value
    column third when the header does not name it (rules §6); any other table counts only with a Value header."""
    cells: list[str] = []
    block: list[str] = []
    for ln in [*text.splitlines(), ""]:
        if ln.strip().startswith("|"):
            block.append(ln)
            continue
        if block:
            has_head = len(block) > 1 and bool(re.fullmatch(r"[\s|:-]+", block[1]))
            head = [c.lower() for c in _cells(block[0])] if has_head else []
            val_i = head.index("value") if "value" in head else (2 if section_b else -1)
            for row_line in block[2 if has_head else 0:] if val_i >= 0 else []:
                if re.fullmatch(r"[\s|:-]+", row_line) or UNVERIFIED_LINE.search(row_line):
                    continue
                row = _cells(row_line)
                if val_i < len(row):
                    cells.append(row[val_i])
            block = []
    return cells


def _reported_in_prose(line: str) -> list[tuple[float, str, int]]:
    """Numbers a line reports as values: written with units after them, or after =, ≈ or ~."""
    out: list[tuple[float, str, int]] = []
    for num, start, end, written in numbers(line):
        before, after = line[:start], line[end:]
        if LOCATOR_BEFORE.search(before):
            continue
        unit = UNIT.match(after)
        if unit and NOT_UNIT_IN.match(after):
            unit = None
        if unit or ASSIGNED.search(before):
            out.append((num, written, start))
    return out


def reported_values(text: str) -> list[tuple[float, str, str]]:
    """(value, as written, context) for each value the answer reports, before exemptions for inputs."""
    if NO_ANSWER.search(text.strip().split("\n", 1)[0]):
        return []
    sec = _sections(text)
    if "A" in sec and NO_ANSWER.search(sec["A"].strip().split("\n", 1)[0]):
        return []
    # a Result Packet reports its values in A (the answer) and B (the values table), as export_packet reads it;
    # C to G hold sources, conflicts, gaps and the check, whose numbers are not the answer
    packet = "A" in sec or "B" in sec
    prose, tables = (sec.get("A", ""), sec.get("B", "")) if packet else (text, text)
    out: list[tuple[float, str, str]] = []
    for cell in _value_cells(tables, section_b=packet):
        out += [(n, w, cell) for n, _s, _e, w in numbers(cell)]
    for line in prose.splitlines():
        if line.strip().startswith("|") or UNVERIFIED_LINE.search(line):
            continue
        out += [(n, w, line.strip()[:120]) for n, w, _s in _reported_in_prose(line)]
    return out


def unverified(text: str, covered: list[float], inputs: str) -> list[tuple[str, str]]:
    """(as written, context) for each reported value that no verified item covers and the request did not give."""
    given = [n for n, *_ in numbers(inputs)]
    missing: list[tuple[str, str]] = []
    seen: set[str] = set()
    for num, written, ctx in reported_values(text):
        if _same(written, num, covered) or any(math.isclose(num, g, rel_tol=1e-9, abs_tol=1e-12) for g in given):
            continue
        if written not in seen:
            seen.add(written)
            missing.append((written, ctx))
    return missing


# --- the hook decision ----------------------------------------------------------------------------------------

def _subagent_transcripts(transcript: str) -> list[str]:
    base = transcript[:-len(".jsonl")] if transcript.endswith(".jsonl") else transcript
    return sorted(glob.glob(os.path.join(glob.escape(base), "subagents", "*.jsonl")))


def decide(inp: Mapping[str, Any]) -> dict[str, Any] | None:
    """The hook's output for a Stop or SubagentStop input, or None to let the turn end silently."""
    text = str(inp.get("last_assistant_message") or "")
    if not reported_values(text):                     # nothing reported: no transcript read (most turns)
        return None
    sub = inp.get("hook_event_name") == "SubagentStop"
    if sub and inp.get("agent_type") != ERLA_AGENT:   # the hook entry also matches the Professor: its report is
        return None                                   # checked by the check gate, not an answer to the user
    main = str(inp.get("agent_transcript_path") or "") if sub else str(inp.get("transcript_path") or "")
    # The whole session, not the turn: in a real transcript few lines carry the turn's promptId and the tool calls
    # are not among them (agent eval A01, 2026-11-02), so a turn found by promptId held no ERLA call. A session that
    # used ERLA is in scope for every answer, and every request in it holds inputs.
    entries = _read(main, ())
    packet = len(_sections(text)) >= 2
    if not (sub or packet or _uses_erla(entries)):
        return None
    sources = entries + [e for p in ([] if sub else _subagent_transcripts(main))
                         for e in _read(p, ("verify_packet", "verification_id"))]
    inputs = "\n".join(t for t in map(_user_text, entries) if GATE_TAG not in t)   # never the gate's own feedback
    missing = unverified(text, verified_numbers(sources), inputs)
    if not missing:
        return None
    listed = "; ".join(f"{w} ({ctx})" for w, ctx in missing[:MAX_LISTED]) + (
        f"; and {len(missing) - MAX_LISTED} more" if len(missing) > MAX_LISTED else "")
    if inp.get("stop_hook_active"):
        return {"systemMessage": f"{GATE_TAG}: this answer reports value(s) that no verify_packet of this "
                                 f"session passed: {listed}. Treat them as NOT VERIFIED."}
    return {"decision": "block",
            "reason": f"{GATE_TAG}: your answer reports value(s) that no PASS or COMPUTED verify_packet "
                      f"item of this session covers: {listed}. Call verify_packet on each (MCP_MODE step 5) and "
                      "restate the answer, or mark each one NOT VERIFIED on its line. A number the user gave as an "
                      "input is exempt."}
