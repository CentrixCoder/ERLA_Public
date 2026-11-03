"""The verification gate before a recorded Result Packet (CLAUDE.md: "verify_packet stays the gate before every
Result Packet"; laws L3, L10).

``verify_packet`` checks the values it is given. This module makes its result a gate for the two tools that
record a packet: ``log_request`` and ``export_packet`` refuse a packet that reports values unless it names a
``verification_id`` that

* this server process issued (``register`` is called only by ``verify_packet``): a forged id, an id from
  another site or from an earlier server run is unknown here and refused;
* was made against the index as it is now (``last_index_at``): after a refresh the values are verified again;
* covers the packet's values: for ``log_request`` the PASS / COMPUTED items cover ``values_reported`` (less
  the values the record declares ``values_unverified``); for ``export_packet`` every number in the Value
  cell of each row of section B (and of the CSV's value column) matches a PASS / COMPUTED item, at the
  row's locator when the row gives one. A row marked NOT VERIFIED is exempt (rules: a FAIL value may be
  reported NOT VERIFIED). A packet whose numbers sit only in its answer must hold at least one verified
  value of each verification it names.

The records live only in this process's memory: values from the library are never written to a log for
this (the logs hold no more library content than before). Limits, by design: code cannot stop an answer
shown in the chat without verification; it gates what is logged and exported. ``log_request`` carries no
values, so there the binding is the count of verified items, not the values themselves.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import OrderedDict
from typing import Any

from .verify import numbers

KEEP = 1000                    # verifications held per server process (oldest dropped first)
COVERS = ("PASS", "COMPUTED")  # verify_packet statuses that may be reported (COMPUTED labelled as such)
NOT_VERIFIED = re.compile(r"\bNOT\s+VERIFIED\b", re.I)
NO_ANSWER = re.compile(r"^\s*(?:\*\*)?\s*(?:NOT FOUND|UNRESOLVED)\b", re.I)
SECTION = re.compile(r"^#{2,4}[ \t]*([A-G])\.[^\n]*$", re.M)
EMPTY_CELL = {"", "-", "--", "—", "–", "n/a", "na", "none"}

_REGISTRY: OrderedDict[str, dict[str, Any]] = OrderedDict()

FIX = ("call verify_packet on every value, quote and conversion this packet reports, then pass the "
       "verification_id it returns (MCP_MODE step 5)")


class GateError(Exception):
    """A packet the gate refuses; the message says what to do."""


def _nums(v: Any) -> list[float]:
    return [n for n, *_ in numbers(str(v))] if v not in (None, "") else []


def register(verification_id: str, items: list[dict[str, Any]], results: list[dict[str, Any]],
             index_stamp: str | None) -> None:
    """Record a verification verify_packet issued (called by verify_packet only)."""
    covered: list[dict[str, Any]] = []
    examined: list[float] = []
    seen: set[tuple[Any, ...]] = set()
    for item, res in zip(items, results):
        if not isinstance(item, dict):
            continue
        if str(res.get("kind") or "value") != "quote":
            examined += _nums(item.get("value")) + _nums(item.get("source_value"))
        if res.get("status") not in COVERS:
            continue
        kind = str(res.get("kind") or "value")
        nums = _nums(item.get("value")) + _nums(item.get("source_value")) if kind != "quote" else []
        loc = str(item.get("locator") or "").strip()
        key = (kind, str(item.get("doc_id")), loc, tuple(nums), str(item.get("calc_id") or ""),
               str(item.get("text") or "") if kind == "quote" else "")
        if key in seen:                  # the same value verified twice counts once
            continue
        seen.add(key)
        covered.append({"kind": kind, "status": res.get("status"), "nums": nums, "locator": loc})
    statuses = [str(r.get("status")) for r in results]
    _REGISTRY[verification_id] = {"overall": "FAIL" if "FAIL" in statuses or not results else
                                  "UNCONFIRMED" if "UNCONFIRMED" in statuses else "PASS",
                                  "index": index_stamp, "covered": covered, "examined": examined}
    _REGISTRY.move_to_end(verification_id)
    while len(_REGISTRY) > KEEP:
        _REGISTRY.popitem(last=False)


def ids_of(value: Any) -> list[str]:
    """verification_id as given: one id, a comma/space separated list, or a JSON list (a packet whose FAIL items
    were corrected and verified again carries two ids)."""
    raw = value if isinstance(value, list) else re.split(r"[,\s]+", str(value or ""))
    out: list[str] = []
    for x in raw:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def known(ids: list[str]) -> bool:
    """Every id was issued by this server and none of them failed (log_request's ``verified`` flag)."""
    return bool(ids) and all(i in _REGISTRY and _REGISTRY[i]["overall"] == "PASS" for i in ids)


def verified_values(ids: list[str]) -> list[float] | None:
    """The PASS / COMPUTED numbers of the verifications this server issued among ``ids``, or None when it issued
    none of them (the check gate binds a logged check to them, F-36)."""
    mine = [i for i in ids if i in _REGISTRY]
    return [x for i in mine for c in _REGISTRY[i]["covered"] for x in c["nums"]] if mine else None


def examined_values(ids: list[str]) -> list[float] | None:
    """Every number the verifications this server issued among ``ids`` were asked about, whatever their status, or
    None when it issued none of them. The check gate binds a logged check to its packet with these: a packet whose
    values are all reported NOT VERIFIED (UNCONFIRMED, a table not extracted) is still the packet that was checked,
    and whether its values count as verified is ``check_record``'s decision, not the binding's (agent eval A01,
    2026-11-02: such a packet could never be logged)."""
    mine = [i for i in ids if i in _REGISTRY]
    return [x for i in mine for x in _REGISTRY[i].get("examined", [])] if mine else None


def _entries(ids: list[str], index_stamp: str | None) -> list[dict[str, Any]]:
    if not ids:
        raise GateError(f"this packet reports values but names no verification_id: {FIX}")
    unknown = [i for i in ids if i not in _REGISTRY]
    if unknown:
        raise GateError(f"verification_id {', '.join(unknown)} was not issued by this ERLA server's verify_packet "
                        f"(forged, from another site or from an earlier server run): {FIX}")
    stale = [i for i in ids if _REGISTRY[i]["index"] != index_stamp]
    if stale:
        raise GateError(f"verification_id {', '.join(stale)} was made against an earlier state of the index "
                        f"(re-indexed since): {FIX}")
    return [c for i in ids for c in _REGISTRY[i]["covered"]]


def check_record(record: dict[str, Any], index_stamp: str | None) -> dict[str, Any]:
    """log_request with ``values_reported`` > 0: what to record, or GateError."""
    n = int(record["values_reported"])
    u = record.get("values_unverified", 0)
    if type(u) is not int or u < 0 or u > n:
        raise GateError("values_unverified must be a whole number from 0 to values_reported (the values the "
                        "packet reports as NOT VERIFIED)")
    ids = ids_of(record.get("verification_id"))
    covered = _entries(ids, index_stamp)
    if len(covered) < n - u:
        raise GateError(f"the packet reports {n - u} verified value(s) but verification {', '.join(ids)} passed "
                        f"{len(covered)} item(s): correct each FAIL or UNCONFIRMED value and call verify_packet "
                        "again, or report it NOT VERIFIED and count it in values_unverified")
    # a packet that claims every value verified while citing a run that did not pass is either hiding a FAIL or
    # UNCONFIRMED item or citing a run it superseded; logged, it was marked unverified without a word (agent eval
    # A01, 2026-11-02), so it is refused with both ways out
    open_runs = [i for i in ids if _REGISTRY[i]["overall"] != "PASS"]
    if u == 0 and open_runs:
        named = ", ".join("%s (%s)" % (i, _REGISTRY[i]["overall"]) for i in open_runs)
        raise GateError(f"the packet reports every value verified but cites verification {named}: if the packet still "
                        "holds that run's FAIL or UNCONFIRMED value, report it NOT VERIFIED and count it in "
                        "values_unverified; if you corrected it, call verify_packet on the final packet's items and "
                        "cite only that run's verification_id")
    return {"verified": u == 0 and known(ids), "verification_overall": [_REGISTRY[i]["overall"] for i in ids]}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _match(num: float, cell_loc: str, covered: list[dict[str, Any]]) -> bool:
    for c in covered:
        if not any(math.isclose(num, x, rel_tol=1e-9, abs_tol=1e-12) for x in c["nums"]):
            continue
        if c["kind"] == "computed" or not cell_loc or (
                c["locator"] and re.search(rf"(?<![0-9a-z]){re.escape(_norm(c['locator']))}(?![0-9a-z])",
                                           _norm(cell_loc))):
            return True
    return False


def _sections(markdown: str) -> dict[str, str]:
    marks = list(SECTION.finditer(markdown))
    if not marks:
        return {"A": markdown}
    out: dict[str, str] = {}
    for m, nxt in zip(marks, [*marks[1:], None]):
        out[m.group(1)] = out.get(m.group(1), "") + markdown[m.end():nxt.start() if nxt else len(markdown)]
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_rows(section: str) -> list[tuple[str, str, str]]:
    """(value cell, locator cell, whole row) for each data row of the section's table."""
    lines = [ln for ln in section.splitlines() if ln.strip().startswith("|")]
    val_i, loc_i, start = 2, 6, 0
    if len(lines) > 1 and re.fullmatch(r"[\s|:-]+", lines[1]):
        head = [c.lower() for c in _cells(lines[0])]
        val_i = head.index("value") if "value" in head else val_i
        loc_i = head.index("locator") if "locator" in head else -1
        start = 2
    rows: list[tuple[str, str, str]] = []
    for ln in lines[start:]:
        if re.fullmatch(r"[\s|:-]+", ln):
            continue
        cells = _cells(ln)
        val = cells[val_i] if val_i < len(cells) else ""
        loc = cells[loc_i] if 0 <= loc_i < len(cells) else ""
        rows.append((val, "" if loc.lower() in EMPTY_CELL else loc, ln))
    return rows


def _csv_rows(csv_text: str) -> tuple[list[tuple[str, str, str]], bool]:
    """(value, locator, row) for the CSV's value column; second: whether a value column was found."""
    data = list(csv.reader(io.StringIO(csv_text)))
    if not data:
        return [], True
    head = [c.strip().lower() for c in data[0]]
    if "value" not in head:
        return [], False
    vi, li = head.index("value"), head.index("locator") if "locator" in head else -1
    return [(r[vi] if vi < len(r) else "", r[li] if 0 <= li < len(r) else "", ",".join(r)) for r in data[1:]], True


def check_export(markdown: str, csv_text: str, verification_id: str, index_stamp: str | None) -> dict[str, Any]:
    """export_packet: {} when the packet reports no values, else what to record; GateError when refused."""
    sec = _sections(markdown)
    # rows that report a number; a row marked NOT VERIFIED reports it as unverified, which the rules allow
    rows = [r for r in _table_rows(sec.get("B", "")) if _nums(r[0]) and not NOT_VERIFIED.search(r[2])]
    csv_rows, csv_has_col = _csv_rows(csv_text) if csv_text.strip() else ([], True)
    csv_rows = [r for r in csv_rows if _nums(r[0]) and not NOT_VERIFIED.search(r[2])]
    answer = sec.get("A", "")
    no_answer = bool(NO_ANSWER.search(answer.strip().split("\n", 1)[0]))
    answer_nums = bool(_nums(answer)) and not no_answer
    loose_csv = bool(csv_text.strip()) and not csv_has_col and bool(_nums(csv_text))
    if not (rows or csv_rows or answer_nums or loose_csv):
        return {}
    ids = ids_of(verification_id)
    covered = _entries(ids, index_stamp)
    missing: list[str] = []
    for val, loc, _line in rows + csv_rows:
        for num in _nums(val):
            if not _match(num, loc, covered):
                missing.append(f"{num:g}" + (f" at {loc}" if loc else ""))
    if missing:
        raise GateError(f"value(s) {', '.join(missing[:8])} are not PASS or COMPUTED items of verification "
                        f"{', '.join(ids)} (at the row's locator): verify them with verify_packet and pass that "
                        "verification_id too, or mark the row NOT VERIFIED")
    if not rows and not csv_rows:      # numbers only in the answer: each verification must belong to this text
        text_nums = _nums(markdown) + _nums(csv_text)
        for i in ids:
            vals = [x for c in _REGISTRY[i]["covered"] for x in c["nums"]]
            if vals and not any(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) for a in vals for b in text_nums):
                raise GateError(f"verification {i} verified none of the values in this packet (it belongs to "
                                f"another packet): {FIX}")
    return {"verification_id": ids, "verified": known(ids)}


def reset() -> None:
    """Forget every verification (a new server process starts empty; tests)."""
    _REGISTRY.clear()


# G6.1 (guardrails.md): the statement every exported packet carries; its first sentence is the guardrail's own.
NOT_CERTIFIED = "ERLA retrieves and verifies data; it does not certify designs."
CERTIFICATION_NOTICE = (
    f"> {NOT_CERTIFIED} Allowables used in flight, pressure-vessel, lifting or life-safety design must be "
    "verified against the governing, current revision of the controlling standard or specification. An "
    "independent check by ERLA's Professor is an AI check: not a certification and not a substitute for the "
    "checker of record.")


def with_certification_notice(markdown: str) -> tuple[str, bool]:
    """``markdown`` carrying the G6.1 statement, and whether it was added (production loop F-42).

    The statement is required text, so code guarantees it in what export_packet writes rather than trusting the
    prompt: when the packet does not already say it (whitespace and case ignored), the notice goes under the
    packet's title line (or at the top when the packet starts with no title), outside every A-G section, which
    keep their text and order."""
    if " ".join(NOT_CERTIFIED.lower().split()) in " ".join(markdown.lower().split()):
        return markdown, False
    first, _nl, rest = markdown.partition("\n")
    if first.lstrip().startswith("#") and not SECTION.match(first):      # a title, not the A-G section headings
        return f"{first}\n\n{CERTIFICATION_NOTICE}\n\n{rest.lstrip(chr(10))}", True
    return f"{CERTIFICATION_NOTICE}\n\n{markdown}", True
