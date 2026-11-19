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

Claims and consistency (2026-11-18, ADR-0006; owner decision: a gate). In a Result Packet's section A, every factual
sentence carries one to three ``[S#]`` tags (rules section 6, ALCE's format); each tag is listed under Citations; and
the sentence, tags and markup removed, equals the text of a PASS ``claim`` item of a ``verify_packet`` of the session
whose sources carry those tags. Exempt: sentences under four words, lines labelled NOT VERIFIED, sentences labelled
ESTIMATE, ASSUMPTION or COMPUTED or naming a calculation tool or calc_id (their values are gated as values), packet
header lines (Scope, Query, Index date), and pointers of ten words or fewer to a packet section with no digit. The answer must also agree with itself and with what was verified: section A's
``Q = v`` for a property symbol Q matches a section B row for Q when B has one; two section B rows with the same
quantity, conditions and units hold the same value unless section D names both values (G3.4); and a value whose
covering verified items all claimed another property symbol is not reported as this one.

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

# claims (ADR-0006): TAG is erla.attribution.TAG (tests/test_claim_gate.py checks they stay equal)
TAG = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S?\d+)*)\s*\]")
CITATION_LINE = re.compile(r"^\s*(?:[-*]\s*)?\[\s*(S\d+)\s*\]", re.M)
EXEMPT_LABEL = re.compile(r"\b(?:NOT\s+VERIFIED|UNVERIFIED|ESTIMATE|ASSUMPTIONS?|ASSUMED|COMPUTED)\b|"
                          r"^\s*(?:\*\*)?\s*Assumptions?\b|\b(?:dt|joint|stress)_calc\b|\bcalc_id\b|\bunit_convert\b")
POINTER = re.compile(r"\b(?:sections?|table)\s+[A-G]\b|\bsee\s+[A-G]\b|\bbelow\b|\babove\b", re.I)
ABBREV = re.compile(r"\b(?:e\.g|i\.e|fig|figs|eq|eqs|no|nos|ref|refs|vs|approx|ca|cf|al|vol|sec|ch|pp|p|min|max|"
                    r"incl|resp|etc)\.$", re.I)
SPLIT = re.compile(r"[.!?](?:\s*\[\s*S\d[^\]]*\])*(?=\s+(?:[(A-Z0-9]|\[(?!\s*S\d)))")
MIN_WORDS = 4
POINTER_MAX_WORDS = 10        # "See section B." is a pointer; a factual clause with "see section B" attached is not
META_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:Scope|Query|Index(?:\s+date)?|Search budget|Expanded queries|"
                       r"Protocol review|Check policy)\b", re.I)
SYMBOL = re.compile(r"(?<![\w-])(Ftu|Fty|Fcy|Fsu|Fbru|Fbry|KIc|KIC|KIscc|Kc|Ec|E|G)(?![\w-])")
PHRASE = ((re.compile(r"\b(?:ultimate tensile|tensile ultimate)\b", re.I), "Ftu"),
          (re.compile(r"\btensile yield\b", re.I), "Fty"), (re.compile(r"\bcompressive yield\b", re.I), "Fcy"),
          (re.compile(r"\b(?:ultimate shear|shear ultimate)\b", re.I), "Fsu"),
          (re.compile(r"\b(?:ultimate bearing|bearing ultimate)\b", re.I), "Fbru"),
          (re.compile(r"\b(?:bearing yield|yield bearing)\b", re.I), "Fbry"))
BINDING = re.compile(r"(?<![\w-])(Ftu|Fty|Fcy|Fsu|Fbru|Fbry|KIc|KIC|KIscc|Kc|Ec|E|G)(?![\w-])[^=≈\n]{0,40}?[=≈][ \t]*")
GROUP_HEADS = ("quantity", "condition", "basis", "unit", "material", "temper", "form", "direction", "thickness",
               "temperature")


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


# --- claims (ADR-0006) ----------------------------------------------------------------------------------------

def claim_key(text: str) -> str:
    """A sentence as the gate compares it: tags, markdown emphasis, case, quote and dash styles and spacing removed."""
    s = TAG.sub(" ", text)
    s = re.sub(r"[*_`]", "", s).lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    return s.rstrip(".;:!? ")


def tags_of(text: str) -> list[str]:
    """The [S#] tags of a sentence in order: '[S1, S3]' and '[S1][S3]' give ['S1', 'S3'] (as erla.attribution.tags)."""
    out: list[str] = []
    for m in TAG.finditer(text):
        for t in re.split(r"\s*[,;]\s*", m.group(1)):
            t = "S" + (t[1:] if t.upper().startswith("S") else t).strip()
            if t not in out:
                out.append(t)
    return out


def _split(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for m in SPLIT.finditer(text):
        if ABBREV.search(text[start:m.start() + 1]):
            continue
        parts.append(text[start:m.end()].strip())
        start = m.end()
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def sentences(text: str) -> list[str]:
    """Prose sentences of a section (table rows and headings left out; list items and paragraphs split after . ! ?
    before a capital, a digit or a bracket, not after common abbreviations). The prose rule of
    erla.attribution.sentences, which tests/test_claim_gate.py compares."""
    out: list[str] = []
    para: list[str] = []
    for line in [*text.splitlines(), ""]:
        s = line.strip()
        if not s or s.startswith("|") or s.startswith("#") or re.match(r"^(?:[-*•]|\d+[.)])\s+", s):
            if para:
                out += _split(" ".join(para))
                para = []
            if s and not s.startswith("|") and not s.startswith("#"):
                out += _split(re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", s))
            continue
        para.append(s)
    return out


def needs_citation(sentence: str) -> bool:
    """A factual sentence of section A: four words or more, not labelled NOT VERIFIED, ESTIMATE, ASSUMPTION or
    COMPUTED, not naming a calculation tool, and not a pointer to another section with no digit in it."""
    words = len(re.findall(r"[A-Za-z0-9]+", TAG.sub(" ", sentence)))
    if words < MIN_WORDS or EXEMPT_LABEL.search(sentence):
        return False
    return not (words <= POINTER_MAX_WORDS and POINTER.search(sentence) and not re.search(r"\d", TAG.sub(" ", sentence)))


def claim_prose(section_a: str) -> str:
    """Section A without the lines no claim check reads: lines labelled NOT VERIFIED (the label covers its line) and
    packet header lines (Scope, Query, Index date, ...), which describe the search, not the library."""
    return "\n".join(ln for ln in section_a.splitlines() if not UNVERIFIED_LINE.search(ln) and not META_LINE.match(ln))


def verified_claims(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each claim item of the transcript's verify_packet results: {'key', 'tags', 'status'} (from the call's input,
    paired with the result's status by position)."""
    entries = list(entries)
    calls: dict[str, list[Any]] = {}
    for ev in entries:
        for b in _blocks(ev):
            if b.get("type") == "tool_use" and VERIFY.search(str(b.get("name") or "")):
                calls[str(b.get("id"))] = _list(_dict(b.get("input")).get("items"))
    out: list[dict[str, Any]] = []
    for ev in entries:
        for b in _blocks(ev):
            tid = str(b.get("tool_use_id"))
            if b.get("type") != "tool_result" or tid not in calls or b.get("is_error"):
                continue
            try:
                res = _dict(json.loads(_result_text(b)))
            except ValueError:
                continue
            for k, raw in enumerate(_list(res.get("items"))):
                item = _dict(raw)
                given = _dict(calls[tid][k]) if k < len(calls[tid]) else {}
                if item.get("kind") != "claim" or str(given.get("kind") or "") != "claim":
                    continue
                srcs = _list(given.get("sources")) or [given]
                tags = {str(_dict(s).get("tag") or "").strip().upper() for s in srcs} - {""}
                out.append({"key": claim_key(str(given.get("text") or "")), "tags": tags,
                            "status": str(item.get("status") or "")})
    return out


def unverified_claims(text: str, claims: list[dict[str, Any]]) -> list[str]:
    """What is wrong with the citations of section A of a Result Packet (empty when nothing is)."""
    sec = _sections(text)
    if "A" not in sec or NO_ANSWER.search(text.strip().split("\n", 1)[0]) or \
            NO_ANSWER.search(sec["A"].strip().split("\n", 1)[0]):
        return []
    listed = set(CITATION_LINE.findall(text))
    problems: list[str] = []
    for sent in sentences(claim_prose(sec["A"])):
        tags = tags_of(sent)
        short = TAG.sub("", sent).strip()[:90]
        if not tags:
            if needs_citation(sent):
                problems.append(f"no [S#] tag: '{short}'")
            continue
        if len(tags) > 3:
            problems.append(f"more than three tags (split the sentence): '{short}'")
        phantom = [t for t in tags if t not in listed]
        if phantom:
            problems.append(f"{', '.join(phantom)} not listed under Citations: '{short}'")
        if EXEMPT_LABEL.search(sent):
            continue
        key = claim_key(sent)
        same = [c for c in claims if c["key"] == key]
        if not same:
            problems.append(f"not verified as a claim (or changed since): '{short}'")
        elif not any(c["status"] == "PASS" for c in same):
            problems.append(f"its claim check did not pass ({'/'.join(sorted({c['status'] for c in same}))}): '{short}'")
        elif not any(c["status"] == "PASS" and set(tags) == c["tags"] for c in same):
            # exactly the verified set: sources that support a sentence together may not support it apart
            problems.append(f"{', '.join(tags)} not the tags of the sources it was verified against "
                            f"({' / '.join(', '.join(sorted(c['tags'])) for c in same if c['status'] == 'PASS')}); "
                            f"show the tags it was verified with, or verify it again with these: '{short}'")
    return problems


# --- consistency (ADR-0006) -----------------------------------------------------------------------------------

def symbol(text: str) -> str | None:
    """The property symbol a quantity names (Ftu, Fty, ..., KIc, E), or None when it names none or several."""
    found = {("KIc" if m.group(1) == "KIC" else m.group(1)) for m in SYMBOL.finditer(text)}
    found |= {sym for rx, sym in PHRASE if rx.search(text)}
    return found.pop() if len(found) == 1 else None


def _unit_key(text: str) -> str:
    """The units written at the start of ``text`` ('ksi', ' MPa', 'N/mm²'), normalised; '' when none is."""
    m = UNIT.match(text)
    if not m or NOT_UNIT_IN.match(text):
        return ""
    u = re.sub(r"[ \t ]", "", m.group(0).lower())
    for a, b in (("^2", "2"), ("²", "2"), ("^3", "3"), ("³", "3"), ("deg", "°")):
        u = u.replace(a, b)
    return {"n/mm2": "mpa", "kips": "kip", "lbs": "lb", "um": "µm", "inch": "in", "inches": "in"}.get(u, u)


def _comparable(unit_a: str, unit_b: str) -> bool:
    """Two values can be compared as numbers: same units, or units not known on one side (PR #136 review: 1000 MPa
    and 1 GPa are the same value, so values in different units are never called a clash)."""
    return not unit_a or not unit_b or unit_a == unit_b


def _rows(section_b: str) -> list[dict[str, Any]]:
    """Section B's table rows (NOT VERIFIED rows left out) as {'quantity', 'symbol', 'value' (first number),
    'written', 'unit' (from a units column, else written after the value), 'group' (quantity, conditions,
    units...), 'cell'}."""
    out: list[dict[str, Any]] = []
    lines = [ln for ln in section_b.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3 or not re.fullmatch(r"[\s|:-]+", lines[1]):
        return out
    head = [c.lower() for c in _cells(lines[0])]
    qi = next((i for i, h in enumerate(head) if "quantity" in h), 1)
    vi = head.index("value") if "value" in head else 2
    gi = [i for i, h in enumerate(head) if i != qi and any(g in h for g in GROUP_HEADS)]
    ui = next((i for i, h in enumerate(head) if "unit" in h), None)
    for ln in lines[2:]:
        if UNVERIFIED_LINE.search(ln):
            continue
        row = _cells(ln)
        if vi >= len(row) or qi >= len(row):
            continue
        nums = numbers(row[vi])
        if not nums:
            continue
        group = tuple(re.sub(r"\s+", " ", row[i].lower()).strip() for i in gi if i < len(row))
        unit = _unit_key(row[ui].strip()) if ui is not None and ui < len(row) else ""
        out.append({"quantity": row[qi], "symbol": symbol(row[qi]), "value": nums[0][0], "written": nums[0][3],
                    "unit": unit or _unit_key(row[vi][nums[0][2]:]), "group": group, "cell": row[vi]})
    return out


def _bindings(prose: str) -> list[tuple[str, float, str, str, str]]:
    """(symbol, value, as written, line, units written after it or '') for each 'Q = v' in prose lines that are not
    NOT VERIFIED."""
    out: list[tuple[str, float, str, str, str]] = []
    for line in prose.splitlines():
        if line.strip().startswith("|") or UNVERIFIED_LINE.search(line):
            continue
        for m in BINDING.finditer(line):
            nums = numbers(line[m.end():m.end() + 30])
            if nums and nums[0][1] == 0:
                sym = "KIc" if m.group(1) == "KIC" else m.group(1)
                out.append((sym, nums[0][0], nums[0][3], line.strip()[:100],
                            _unit_key(line[m.end() + nums[0][2]:])))
    return out


def verified_bindings(entries: Iterable[Mapping[str, Any]]) -> list[tuple[float, str | None]]:
    """(number, claimed property symbol or None) for each number a PASS value item of the session vouches for."""
    entries = list(entries)
    calls: dict[str, list[Any]] = {}
    for ev in entries:
        for b in _blocks(ev):
            if b.get("type") == "tool_use" and VERIFY.search(str(b.get("name") or "")):
                calls[str(b.get("id"))] = _list(_dict(b.get("input")).get("items"))
    out: list[tuple[float, str | None]] = []
    for ev in entries:
        for b in _blocks(ev):
            tid = str(b.get("tool_use_id"))
            if b.get("type") != "tool_result" or tid not in calls or b.get("is_error"):
                continue
            try:
                res = _dict(json.loads(_result_text(b)))
            except ValueError:
                continue
            for k, raw in enumerate(_list(res.get("items"))):
                item = _dict(raw)
                if item.get("status") != "PASS" or str(item.get("kind") or "value") != "value":
                    continue
                given = _dict(calls[tid][k]) if k < len(calls[tid]) else {}
                sym = symbol(str(given.get("quantity") or item.get("quantity") or ""))
                for field in ("value", "source_value"):
                    v = given.get(field, item.get(field))
                    if v not in (None, ""):
                        out += [(n, sym) for n, *_ in numbers(str(v))]
    return out


def inconsistencies(text: str, bindings: list[tuple[float, str | None]], inputs: str) -> list[str]:
    """Where the answer disagrees with itself or with what was verified (empty when it does not)."""
    sec = _sections(text)
    packet = "A" in sec or "B" in sec
    if NO_ANSWER.search(text.strip().split("\n", 1)[0]) or (packet and NO_ANSWER.search(
            sec.get("A", "").strip().split("\n", 1)[0])):
        return []
    rows = _rows(sec.get("B", "")) if packet else []
    prose = sec.get("A", "") if packet else text
    given = [n for n, *_ in numbers(inputs)]
    out: list[str] = []
    claims = _bindings(prose)
    for sym, v, written, _line, unit in claims:        # (i) section A against section B, in the same units
        mine = [r for r in rows if r["symbol"] == sym and _comparable(unit, r["unit"])]
        if mine and not any(_same(written, v, [r["value"]]) or _same(r["written"], r["value"], [v]) for r in mine):
            out.append(f"section A gives {sym} = {written}, section B gives {sym} = "
                       f"{', '.join(sorted({r['written'] for r in mine}))}")
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    d_text = sec.get("D", "")
    for r in rows:                                     # (ii) two rows, same quantity and conditions, two values
        key = (r["symbol"] or r["quantity"].lower(), r["unit"], *r["group"])
        first = seen.setdefault(key, r)
        if first is r or math.isclose(first["value"], r["value"], rel_tol=1e-9, abs_tol=1e-12):
            continue
        if not all(re.search(r"(?<![\d.])" + re.escape(w) + r"(?![\d])", d_text) for w in (first["written"], r["written"])):
            out.append(f"section B gives {r['quantity']} as {first['written']} and {r['written']} for the same "
                       "conditions, and section D does not name the conflict (G3.4: report each with its source)")
    for sym, v, written, _line, _unit in [*claims, *((r["symbol"], r["value"], r["written"], r["cell"], r["unit"])
                                                     for r in rows)]:
        if not sym or any(math.isclose(v, g, rel_tol=1e-9, abs_tol=1e-12) for g in given):
            continue                                   # (iii) verified as another quantity
        cover = [s for n, s in bindings if _same(written, v, [n])]
        if cover and all(cover) and sym not in cover:
            out.append(f"{written} is reported as {sym}, but it was verified as {', '.join(sorted(set(c for c in cover if c)))}")
    return list(dict.fromkeys(out))


# --- the hook decision ----------------------------------------------------------------------------------------

def _subagent_transcripts(transcript: str) -> list[str]:
    base = transcript[:-len(".jsonl")] if transcript.endswith(".jsonl") else transcript
    return sorted(glob.glob(os.path.join(glob.escape(base), "subagents", "*.jsonl")))


def decide(inp: Mapping[str, Any]) -> dict[str, Any] | None:
    """The hook's output for a Stop or SubagentStop input, or None to let the turn end silently."""
    text = str(inp.get("last_assistant_message") or "")
    packet = len(_sections(text)) >= 2
    if not packet and not reported_values(text):      # nothing reported: no transcript read (most turns)
        return None
    sub = inp.get("hook_event_name") == "SubagentStop"
    if sub and inp.get("agent_type") != ERLA_AGENT:   # the hook entry also matches the Professor: its report is
        return None                                   # checked by the check gate, not an answer to the user
    main = str(inp.get("agent_transcript_path") or "") if sub else str(inp.get("transcript_path") or "")
    # The whole session, not the turn: in a real transcript few lines carry the turn's promptId and the tool calls
    # are not among them (agent eval A01, 2026-11-02), so a turn found by promptId held no ERLA call. A session that
    # used ERLA is in scope for every answer, and every request in it holds inputs.
    entries = _read(main, ())
    if not (sub or packet or _uses_erla(entries)):
        return None
    sources = entries + [e for p in ([] if sub else _subagent_transcripts(main))
                         for e in _read(p, ("verify_packet", "verification_id"))]
    inputs = "\n".join(t for t in map(_user_text, entries) if GATE_TAG not in t)   # never the gate's own feedback
    missing = unverified(text, verified_numbers(sources), inputs)
    claims = unverified_claims(text, verified_claims(sources)) if packet else []
    clashes = inconsistencies(text, verified_bindings(sources), inputs)
    if not (missing or claims or clashes):
        return None

    def listing(xs: list[str]) -> str:
        return "; ".join(xs[:MAX_LISTED]) + (f"; and {len(xs) - MAX_LISTED} more" if len(xs) > MAX_LISTED else "")

    listed = listing([f"{w} ({ctx})" for w, ctx in missing])
    if inp.get("stop_hook_active"):
        said: list[str] = []
        if missing:
            said.append(f"this answer reports value(s) that no verify_packet of this session passed: {listed}. "
                        "Treat them as NOT VERIFIED.")
        if claims:
            said.append(f"sentence(s) of section A are not backed by a cited source that code checked: "
                        f"{listing(claims)}. Treat them as NOT VERIFIED.")
        if clashes:
            said.append(f"the answer contradicts itself or what was verified: {listing(clashes)}.")
        return {"systemMessage": f"{GATE_TAG}: " + " ".join(said)}
    parts: list[str] = []
    if missing:
        parts.append(f"your answer reports value(s) that no PASS or COMPUTED verify_packet item of this session "
                     f"covers: {listed}. Call verify_packet on each (MCP_MODE step 5) and restate the answer, or mark "
                     "each one NOT VERIFIED on its line. A number the user gave as an input is exempt.")
    if claims:
        parts.append(f"section A's citations: {listing(claims)}. Give each factual sentence of section A one to three "
                     "[S#] tags listed under Citations, verify each tagged sentence with verify_packet kind 'claim' "
                     "(text = the sentence without its tags, sources = [{doc_id, locator, tag}]) and show it exactly as "
                     "verified, or label its line NOT VERIFIED (rules section 6).")
    if clashes:
        parts.append(f"the answer is not consistent: {listing(clashes)}. Correct the value or quantity, or report the "
                     "conflict in section D with each source (G3.4).")
    return {"decision": "block", "reason": f"{GATE_TAG}: " + " ".join(parts)}
