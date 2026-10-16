"""Result Packet verification (G3.1/G3.2/G3.5, law L3: the model proposes, code verifies).

``verify_items`` re-opens every cited locator and checks, deterministically, that each reported
value is actually there. The agent's own re-reading (rules section 2) stays; this is
the check that does not depend on the model.

Item kinds:
  value     (default) ``value`` (a number, a range "60-65", or text with a number) must appear at
            ``doc_id`` + ``locator``. ``units`` is looked for near the number (warning if absent:
            tables carry units in headers). A converted value names its source with
            ``source_value`` + ``source_units``: the source value is checked at the locator and
            the conversion is recomputed with erla.units.
  quote     ``text`` must appear at the locator (case, spacing, quote marks and dashes normalized);
            over 15 words is a G5.2 warning.
  computed  a calculated value (0.2.19, law L1). With ``calc_id`` (returned by an installed specialist's
            calculation tool) and ``calc_output`` (a result name) the value is checked against that
            recorded run; ``scale`` 2 or 0.5 converts a half length to a total one or back.
            Without a run it needs ``not_covered`` (why no tool applies): status COMPUTED, model
            arithmetic not checked by code. Neither: FAIL - a hand calculation cannot pass silently.
            Verify each input as its own value item either way.

Conditions (0.2.16): a value item may claim ``temper`` (or ``condition``), ``direction``, ``basis``,
``thickness`` and ``form``. The value must then belong to those conditions in the property records at
that page - a number printed for H1150 fails when reported as H900. A condition the parser could not
read for that column is a warning, never a failure.

Table rows (0.2.20): a value that sits in an extracted table row is checked against the row the item
names (``quantity``, ``material``, ``temper``, ``form``, ``direction`` or free-text ``row``), unless a
property record already confirmed its conditions. A number printed in another row fails; one printed in
several rows that the claim does not tell apart fails as ambiguous. The result names the ``row``.

Locators resolve as in ``library_show``: exact match, else prefix. A value found only in the chunk
next to the cited one passes with a warning naming where it is (``found_at``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from typing import Any, cast

from .classify import copy_group_flags, effective_flag

NUM = re.compile(r"(?<![\w.])([-−–]?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d*|\.\d+|\d+)"
                 r"(?:[eE]([-+]?\d+))?(?!\d)")
UNIT_WINDOW = 80          # characters either side of the number in which the units are looked for
SNIPPET = 60
QUOTE_MAX_WORDS = 15      # G5.2

RULE = ("Report as verified only items with status PASS. FAIL: correct the value or locator and verify "
        "again, or report the value as NOT VERIFIED; never report it as sourced (G3.1/G3.2). A computed "
        "value passes only against a recorded calc run (calc_id + calc_output); COMPUTED (not_covered) "
        "is model arithmetic: show the equation, say why the calc tool did not apply, and verify each input as its own "
        "item. Carry every warning into the packet "
        "(OCR values: mark OCR, lower confidence, recommend visual check, G3.5; flagged: label, G2.4). "
        "Pass verification_id to log_request.")


def numbers(text: str) -> list[tuple[float, int, int, str]]:
    """Numbers in ``text`` as (value, start, end, as written). Thousands commas and a unicode minus
    are understood; a hyphen between two numbers is a range, not a sign."""
    out: list[tuple[float, int, int, str]] = []
    for m in NUM.finditer(text):
        sign, digits, exp = m.group(1), m.group(2).replace(",", ""), m.group(3)
        try:
            v = float(digits + (f"e{exp}" if exp else ""))
        except ValueError:
            continue
        out.append((-v if sign else v, m.start(), m.end(), m.group(0)))
    return out


def decimals(written: str) -> int:
    s = written.split("e")[0].split("E")[0]
    return len(s.split(".", 1)[1]) if "." in s else 0


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", s).strip()


def _snippet(text: str, start: int, end: int) -> str:
    a, b = max(0, start - SNIPPET), min(len(text), end + SNIPPET)
    return ("…" if a else "") + re.sub(r"\s+", " ", text[a:b]).strip() + ("…" if b < len(text) else "")


def _units_near(text: str, start: int, end: int, unit: str) -> bool:
    window = _norm(text[max(0, start - UNIT_WINDOW):end + UNIT_WINDOW])
    u = _norm(unit).rstrip(".")
    if not u:
        return True
    return re.search(r"(?<![a-z])" + re.escape(u) + r"(?![a-z])", window) is not None


class _Doc:
    def __init__(self, con: sqlite3.Connection, doc_id: int):
        from .store import quality_expr
        self.row = con.execute("SELECT doc_id, title, path, classification_flag, ocr_used FROM documents "
                               "WHERE doc_id=?", (doc_id,)).fetchone()
        self.chunks = con.execute(f"SELECT seq, locator, text, {quality_expr(con, 'chunks')} FROM chunks "
                                  "WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall() if self.row else []

    def at(self, locator: str) -> list[sqlite3.Row]:
        want = locator.lower().strip()
        return [r for r in self.chunks if r["locator"].lower() == want] or \
               [r for r in self.chunks if r["locator"].lower().startswith(want)]

    def near(self, sel: list[sqlite3.Row]) -> list[sqlite3.Row]:
        seqs = {r["seq"] for r in sel}
        return [r for r in self.chunks if r["seq"] not in seqs and any(abs(r["seq"] - s) == 1 for s in seqs)]


def _quality(doc: _Doc, row: sqlite3.Row, written: str | None) -> list[str]:
    w: list[str] = []
    q: dict[str, Any] = {}
    if row["quality_json"]:
        try:
            q = json.loads(row["quality_json"])
        except ValueError:
            q = {}
    if doc.row["ocr_used"] or "ocr_conf" in q:
        w.append("OCR-derived text: mark the value OCR, lower confidence one level, recommend a visual check (G3.5)")
    if written and written in (q.get("low_conf_numbers") or []):
        w.append(f"OCR_LOW_CONF: '{written}' was read with low OCR confidence - verify it on the page")
    return w


def _find_value(rows: list[sqlite3.Row], want: list[float], unit: str | None
                ) -> tuple[sqlite3.Row, int, int, str, bool] | None:
    """The first chunk holding every wanted number; returns (chunk, start, end, as written, units near)."""
    for r in rows:
        found = numbers(r["text"])
        hits: list[tuple[float, int, int, str]] = []
        for v in want:
            hit = next((n for n in found if math.isclose(n[0], v, rel_tol=1e-9, abs_tol=1e-12)), None)
            if hit is None:
                break
            hits.append(hit)
        else:
            if not hits:
                continue
            start, end = min(h[1] for h in hits), max(h[2] for h in hits)
            near = True if not unit else any(_units_near(r["text"], h[1], h[2], unit) for h in hits)
            return r, start, end, hits[0][3], near
    return None


def _check_value(con: sqlite3.Connection, doc: _Doc, sel: list[sqlite3.Row], item: dict[str, Any],
                 res: dict[str, Any]) -> None:
    converted = item.get("source_value") is not None
    raw = item.get("source_value") if converted else item.get("value")
    unit = str(item.get("source_units") if converted else item.get("units") or "") or None
    want = [n[0] for n in numbers(str(raw))] if raw is not None else []
    if not want:
        res.update(status="FAIL", reason=f"no number in the reported value {raw!r}")
        return

    hit = _find_value(sel, want, unit)
    if hit is None:
        hit = _find_value(doc.near(sel), want, unit)
        if hit is not None:
            res["warnings"].append(f"found in the next chunk ({hit[0]['locator']}), not at the cited locator: cite that locator")
            res["found_at"] = hit[0]["locator"]
    if hit is None:
        shown = sorted({n[3] for r in sel for n in numbers(r["text"])})[:40]
        res.update(status="FAIL", reason=f"{raw} not found at {item.get('locator')}",
                   numbers_at_locator=shown)
        return

    row, start, end, written, near = hit
    res.update(status="PASS", evidence=_snippet(row["text"], start, end))
    if unit and not near:
        res["warnings"].append(f"units '{unit}' not found next to the value (a table may give them in a header): check")
    res["warnings"] += _quality(doc, row, written)

    if converted:
        _check_conversion(item, res)
    if res["status"] == "PASS" and len(want) == 1:
        _check_conditions(con, int(doc.row["doc_id"]), str(row["locator"]), want[0], item, res)
    if res["status"] == "PASS" and "conditions" not in res:      # not already confirmed by a property record
        _check_row(doc, str(row["locator"]), want, item, res)


# ------------------------------------------------------------------ table rows (0.2.20)
TableRow = tuple[list[str], list[str], dict[str, int]]     # (labels, data cells, claim field -> label position)
ROW_FIELDS = ("row", "quantity", "material", "temper", "form", "direction")   # quantity: transposed tables (Fty, Fcy rows)
# a data cell: numbers, ranges and bounds, each optionally carrying a footnote letter ("80a", "62a 64a")
NUMERIC_CELL = re.compile(r"^(?:[\s$≤≥<>~±+\-−–(]*\d[\d.,/:\-−–]*[a-z]{0,2}[)%*]*\s*)+$")


def _seq(text: str) -> str:
    """Ordered tokens: 'L-T' -> 'l t' (not equal to 't l'), '2024-T351' -> '2024 t351'."""
    return " ".join(re.findall(r"[a-z0-9.]+", _norm(text).replace("-", " ")))


def _has(claim: str, text: str) -> bool:
    c = _seq(claim)
    return bool(c) and f" {c} " in f" {_seq(text)} "


def _table_rows(doc: _Doc, page: str) -> list[list[tuple[list[str], list[str]]]]:
    """Data rows of each extracted table on the page, as (label cells, data cells). Label cells are the
    current group heading (a row such as "Fcy, ksi:" with no numbers) plus the row's leading non-numeric
    cells, an empty one inheriting the cell above (merged group labels)."""
    tables: list[list[tuple[list[str], list[str]]]] = []
    for ch in doc.chunks:
        loc = str(ch["locator"]).lower()
        if not (loc == page or loc.startswith(page + " ")):
            continue
        rows: list[tuple[list[str], list[str]]] = []
        above: list[str] = []
        group: list[str] = []
        for line in str(ch["text"] or "").splitlines():
            if line.count("|") < 2:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            first_num = next((i for i, c in enumerate(cells) if c and NUMERIC_CELL.match(c) and numbers(c)), None)
            if first_num is None:
                filled = [c for c in cells if c]
                if 0 < len(filled) <= 2:                      # a group heading ("Fcy, ksi:") for the rows below
                    group, above = filled, []
                continue                                      # a column-heading or text row: no data
            label = [c or (above[i] if i < len(above) else "") for i, c in enumerate(cells[:first_num])]
            above = label
            rows.append(([g for g in group if g not in label] + label, cells[first_num:]))
        if rows:
            tables.append(rows)
    return tables


def _check_row(doc: _Doc, locator: str, want: list[float], item: dict[str, Any], res: dict[str, Any]) -> None:
    """A table value must sit in the row the item claims (0.2.20). The page text flattens tables, so the
    number alone can be found in another row (a KIc printed for one alloy passed for another). The claim
    names its row by material, temper (or condition), form, direction or free-text ``row``; a value in
    rows with different labels and no claim that tells them apart fails as ambiguous."""
    page = re.sub(r"\s+table\s+\d+$", "", locator.strip().lower())
    tables = _table_rows(doc, page)
    if not tables:
        return
    claims = {f: str(item.get("condition") if f == "temper" and not item.get("temper") else item.get(f))
              for f in ROW_FIELDS if item.get(f) or (f == "temper" and item.get("condition"))}
    if "quantity" in claims:                                  # the stored code (Fty) and the user's word (yield)
        from .props import canonical_quantity
        claims["quantity"] = canonical_quantity(claims["quantity"]) or claims["quantity"]

    def where(field: str, claim: str, label: list[str], data: list[str]) -> int | None:
        """Label position the claim names in this row (-1: free-text row over the whole row), else None."""
        if field == "row":
            return -1 if _has(claim, " ".join(label + data)) else None
        if field == "quantity":                              # 'µ' is poisson, 'ω, lb/in.3' density (props labels)
            from .props import parse_label
            return next((i for i, c in enumerate(label) if parse_label(c)[0] == claim or _has(claim, c)), None)
        return next((i for i, c in enumerate(label) if _has(claim, c)), None)

    def contradicts(other: dict[str, int], hit: dict[str, int], hit_label: list[str]) -> bool:
        """Another row carries more of the claim, and where it does, the value's row has a different label:
        the claim describes that other row, which holds a different number."""
        extra = set(other) - set(hit)
        return set(hit) <= set(other) and bool(extra) and any(
            0 <= other[f] < len(hit_label) and hit_label[other[f]] for f in extra)

    # Each row records which claims its labels carry, and where. A value row contradicted by another row of
    # the same table is out; among the rest the row carrying most of the claim is the row, and a tie between
    # differently labelled rows is ambiguous.
    hits: list[TableRow] = []
    candidates: list[TableRow] = []
    seen: set[str] = set()
    for rows in tables:
        scored: list[TableRow] = []
        for lab, dat in rows:
            pos = {f: p for f, c in claims.items() if (p := where(f, c, lab, dat)) is not None}
            scored.append((lab, dat, pos))
            seen |= set(pos)
        held = {i for i, (_, dat, _) in enumerate(scored)
                if all(any(math.isclose(v, x, rel_tol=1e-9, abs_tol=1e-12) for c in dat for x, *_ in numbers(c))
                       for v in want)}
        found_in = [scored[i] for i in sorted(held)]
        others = [p for i, (_, _, p) in enumerate(scored) if i not in held]
        hits += found_in
        candidates += [h for h in found_in if not any(contradicts(o, h[2], h[0]) for o in others)]
    if not hits:
        return                                                # the number is in prose, not in a table row
    for f in sorted(set(claims) - seen):
        res["warnings"].append(f"{f} '{claims[f]}' labels no table row here (a column heading?): confirm it on the page")
    shown = "; ".join(" ".join(x for x in lab if x) or "(unlabelled row)" for lab, _, _ in hits[:5])
    if not candidates:
        res.update(status="FAIL", reason=(f"{item.get('value')} is printed in the row(s) {shown}, not in the row for "
                                          f"{', '.join(f'{k} {v}' for k, v in claims.items())}: cite the right "
                                          "row's value or correct the claim"))
        return
    top = max(len(s) for _, _, s in candidates)
    best = [(lab, dat) for lab, dat, s in candidates if len(s) == top]
    labels = {tuple(x for x in lab if x) for lab, _ in best}
    if len(labels) > 1:
        res.update(status="FAIL", reason=(f"{item.get('value')} appears in {len(labels)} rows here that the claim does "
                                          f"not tell apart ({'; '.join(' '.join(x) or '(unlabelled row)' for x in sorted(labels)[:5])}): "
                                          "name the row (quantity, material, temper, form, direction or row)"))
        return
    lab, dat = best[0]
    res["row"] = " ".join(x for x in lab if x)
    res["evidence"] = " | ".join(lab + dat)[:240]


CONDITIONS = ("temper", "direction", "basis", "thickness", "form")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9.]+", _norm(text).replace("-", " ")))


def _span(text: str) -> tuple[float, float] | None:
    """Thickness as an interval: '0.020-0.062' -> (0.020, 0.062); '≤0.187' / '<4.000' -> (0, 4.0);
    '>4.000' -> (4.0, inf); '0.050' -> (0.050, 0.050). None when no number or more than two."""
    t = _norm(text)
    vals = [abs(n[0]) for n in numbers(t)]          # a hyphen between two numbers is a range, not a sign
    if len(vals) == 2:
        return min(vals), max(vals)
    if len(vals) != 1:
        return None
    if re.match(r"^\s*(?:[<≤#]|up to\b)", t):
        return 0.0, vals[0]
    if re.match(r"^\s*(?:[>≥]|over\b)", t):
        return vals[0], math.inf
    return vals[0], vals[0]


def _compatible(field: str, claim: str, rec: str | None) -> bool | None:
    """True/False when the record confirms/contradicts the claimed condition; None when the record
    does not state it (the parser could not read that heading), which never fails an item."""
    if not rec:
        return None
    if field == "thickness":                    # the claimed thickness (a point or a range) lies in the column's
        a, b = _span(claim), _span(rec)         # printed range: 0.050 in 0.020-0.062 (0.3.1)
        return a is not None and b is not None and b[0] <= a[0] and a[1] <= b[1]
    if field == "basis":
        return _norm(claim).rstrip(".").removesuffix("-basis").strip() == _norm(rec)
    c, r = _tokens(claim), _tokens(rec)
    return bool(c) and (c <= r or r <= c)       # "T6" in "T6, T62a"; "plate" in "sheet and plate"


def _check_conditions(con: sqlite3.Connection, doc_id: int, locator: str, value: float, item: dict[str, Any],
                      res: dict[str, Any]) -> None:
    """A value can be on the cited page and still belong to another column (H1150, not H900). When the
    item claims conditions, the property records for that value at that page must agree (0.2.16)."""
    claims = {f: str(item.get(f) if f != "temper" else item.get("temper") or item.get("condition"))
              for f in CONDITIONS if item.get(f) or (f == "temper" and item.get("condition"))}
    if not claims:
        return
    from .store import has_table
    rows: list[sqlite3.Row] = []
    if has_table(con, "properties"):
        rows = con.execute(
            "SELECT quantity, value, temper, direction, basis, thickness, form, flags FROM properties "
            "WHERE doc_id=? AND (lower(locator) = lower(?) OR lower(locator) LIKE lower(?) || ' %')",
            (doc_id, locator, locator)).fetchall()
    rows = [r for r in rows if r["value"] is not None and math.isclose(r["value"], value, rel_tol=1e-9)]
    if item.get("quantity"):
        from .props import canonical_quantity
        q = canonical_quantity(str(item["quantity"]))
        rows = [r for r in rows if r["quantity"] == q] or rows
    if not rows:
        res["warnings"].append(f"conditions ({', '.join(f'{k} {v}' for k, v in claims.items())}) not checked: "
                               "no property record for this value here - confirm the column heading on the page")
        return
    best: sqlite3.Row | None = None
    unknown: set[str] = set()
    for r in rows:
        unresolved = "TEMPER_UNRESOLVED" in (r["flags"] or "")
        verdicts = {f: (None if f == "temper" and unresolved else _compatible(f, c, r[f])) for f, c in claims.items()}
        if all(v is not False for v in verdicts.values()):
            best, unknown = r, {f for f, v in verdicts.items() if v is None}
            if not unknown:
                break
    if best is None:
        seen = "; ".join(sorted({", ".join(f"{f} {r[f]}" for f in claims if r[f]) for r in rows}))[:300]
        res.update(status="FAIL", reason=(f"{item.get('value')} at {locator} is printed for {seen or 'other conditions'}, "
                                          f"not {', '.join(f'{k} {v}' for k, v in claims.items())}: "
                                          "read the column heading and correct the conditions or the value"))
        return
    res["conditions"] = {f: best[f] for f in claims}
    if unknown:
        ambiguous = [x for x in json.loads(best["flags"] or "[]")
                     if x.startswith("CONDITION_AMBIGUOUS: ") and x.split()[1] in unknown]
        res["warnings"].append(f"{', '.join(sorted(unknown))} not confirmed (not parsed for this column): "
                               "check the heading on the page" + "".join(f" [{x}]" for x in ambiguous))


def _check_conversion(item: dict[str, Any], res: dict[str, Any]) -> None:
    from .units import convert
    src = numbers(str(item["source_value"]))
    rep = numbers(str(item.get("value", "")))
    if len(src) != 1 or len(rep) != 1 or not item.get("units") or not item.get("source_units"):
        res.update(status="FAIL", reason="a converted value needs one number each in value and source_value, "
                                          "plus units and source_units")
        return
    try:
        c = convert(src[0][0], str(item["source_units"]), str(item["units"]))
    except ValueError as exc:
        res.update(status="FAIL", reason=f"conversion not recomputable: {exc}")
        return
    reported, places = rep[0][0], decimals(rep[0][3])
    res["conversion"] = c.text()
    half_ulp = 0.5 * 10 ** -places + 1e-12
    if abs(c.value - reported) <= half_ulp:
        return
    if math.isclose(c.value, reported, rel_tol=0.01):
        res["warnings"].append(f"conversion gives {c.value:.6g}; the reported {rep[0][3]} is rounded more "
                               "coarsely than its digits suggest")
        return
    res.update(status="FAIL", reason=f"conversion gives {c.value:.6g} {item['units']}, not {rep[0][3]}")


def _check_quote(sel: list[sqlite3.Row], item: dict[str, Any], res: dict[str, Any]) -> None:
    text = str(item.get("text") or "")
    if not text.strip():
        res.update(status="FAIL", reason="quote item without text")
        return
    if len(text.split()) > QUOTE_MAX_WORDS:
        res["warnings"].append(f"quote is {len(text.split())} words; G5.2 allows at most {QUOTE_MAX_WORDS}")
    want = _norm(text)
    for r in sel:
        hay = _norm(r["text"])
        i = hay.find(want)
        if i >= 0:
            res.update(status="PASS", evidence=_snippet(hay, i, i + len(want)))
            return
    res.update(status="FAIL", reason="quote text not found at the locator")


CALC_SCALES = (1.0, 2.0, 0.5)     # as computed; half length -> total crack length; total -> half

def _calc_tools() -> dict[str, tuple[re.Pattern[str], re.Pattern[str]]]:
    """Calc tools of the installed specialists (none in the free core edition): the quantities each certainly
    computes (a hand calculation of one fails) and may compute (claiming not_covered is flagged)."""
    from .specialists import installed
    return {str(s["tool"]): (s["required"], s["covered"]) for s in installed().values()}


def _tool(run: dict[str, Any]) -> str:
    return str(run.get("cmd") or "mcp.calc").removeprefix("mcp.")


def _check_computed(item: dict[str, Any], res: dict[str, Any], calc_runs: dict[str, dict[str, Any]]) -> None:
    calc_id = str(item.get("calc_id") or "").strip()
    reason = str(item.get("not_covered") or "").strip()
    tools = _calc_tools()
    if not calc_id:
        if not reason:
            if tools:
                res.update(status="FAIL", reason="computed value without a calc run: call the calc tool ("
                                                 f"{' or '.join(tools)}) and pass calc_id + calc_output, or state "
                                                 "not_covered (why no tool applies)")
            else:
                res.update(status="FAIL", reason="computed value without a calc run: no calculation tool is installed, "
                                                 "so state not_covered with the method used")
            return
        quantity = str(item.get("quantity") or "")
        for tool, (required, _) in tools.items():
            if required.search(quantity):                    # 0.2.20 / 0.3.0: no hand calculation of these
                res.update(status="FAIL", reason=f"{tool} computes '{quantity}': call {tool} and pass calc_id + "
                                                 f"calc_output (not_covered is for methods {tool} does not cover)")
                return
        res.update(status="COMPUTED", reason=f"outside {' and '.join(tools)}: {reason}" if tools
                   else f"no calculation tool installed: {reason}")
        res["warnings"].append("model arithmetic, not checked by code (L3): show the equation; verify each input")
        for tool, (_, covered) in tools.items():
            if covered.search(quantity):
                res["warnings"].append(f"{tool} may compute '{quantity}': use it unless the stated reason holds")
        return
    run = calc_runs.get(calc_id)
    if run is None:
        res.update(status="FAIL", reason=f"calc_id {calc_id} is not a calc run recorded by this server; call the "
                                         "calc tool and pass the calc_id it returns")
        return
    outputs: dict[str, float] = run.get("outputs") or {}
    name = str(item.get("calc_output") or "").strip()
    if name not in outputs:
        res.update(status="FAIL", reason=f"calc_output '{name}' is not a result of {calc_id}",
                   available=sorted(outputs)[:40])
        return
    try:
        scale = float(item.get("scale") or 1)
    except (TypeError, ValueError):
        scale = math.nan
    if scale not in CALC_SCALES:
        res.update(status="FAIL", reason="scale must be 1, 2 (half length to total crack length) or 0.5")
        return
    found = numbers(str(item.get("value") or ""))
    if not found:
        res.update(status="FAIL", reason="value has no number")
        return
    v, written = found[0][0], found[0][3]
    expect = outputs[name] * scale
    tol = max(0.5 * 10 ** -decimals(written), 0.005 * abs(expect))
    res["calc"] = {"calc_id": calc_id, "task": run.get("task"), "output": name, "result": outputs[name],
                   "scale": scale, "units": run.get("units"), "module": run.get("module")}
    if abs(v - expect) <= tol * (1 + 1e-9):
        res.update(status="PASS", evidence=f"{_tool(run)} {calc_id} {name} = {outputs[name]:.6g}"
                                           + (f" x {scale:g}" if scale != 1 else "") + f" ({run.get('module')})")
    else:
        res.update(status="FAIL", reason=f"{written} does not match {_tool(run)} {name} = {outputs[name]:.6g}"
                                         + (f" x {scale:g}" if scale != 1 else "") + f" = {expect:.6g}")


def _check_chart(con: sqlite3.Connection, cfg: Any, item: dict[str, Any], res: dict[str, Any]) -> None:
    """A chart reading passes only if the figure, read again through every gate, gives the same value
    within the reading's own uncertainty (Plan B, 0.4.0)."""
    from .chart import read_chart
    if cfg is None:
        res.update(status="FAIL", reason="chart items are checked by the server (chart_read)")
        return
    try:
        doc_id, locator, x = int(item["doc_id"]), str(item["locator"]), float(item["x"])
        color = item["color"]
    except (KeyError, TypeError, ValueError):
        res.update(status="FAIL", reason="chart items need doc_id, locator, x, color and value")
        return
    found = numbers(str(item.get("value") or ""))
    if not found:
        res.update(status="FAIL", reason="value has no number")
        return
    region = item.get("region")
    r = read_chart(con, cfg, doc_id, locator, x, color,
                   region=[float(v) for v in cast(list[Any], region)] if isinstance(region, list) else None,
                   cross_check=bool(item.get("cross_check")))
    if r.get("status") != "READ":
        res.update(status="FAIL", reason=f"chart not readable: {r.get('reason')}")
        return
    v, y, dy = found[0][0], float(r["y"]), float(r["uncertainty"])
    res["chart"] = {"y": y, "uncertainty": dy, "x": x}
    if abs(v - y) <= dy * (1 + 1e-9):
        res.update(status="PASS", evidence=f"chart re-read at x = {x:g}: {y:.4g} +/- {dy:.2g}")
        res["warnings"].append("chart reading: lower confidence than a printed value (rules section 2)")
    else:
        res.update(status="FAIL", reason=f"{v:g} is not the chart's {y:.4g} +/- {dy:.2g} at x = {x:g}")


def verify_items(con: sqlite3.Connection, items: list[dict[str, Any]],
                 calc_runs: dict[str, dict[str, Any]] | None = None, cfg: Any = None) -> dict[str, Any]:
    """``calc_runs``: calculation runs recorded by the server (erla.calcrun), by calc_id."""
    results: list[dict[str, Any]] = []
    docs: dict[int, _Doc] = {}
    groups = copy_group_flags(con)       # G2.5: copies carry their copy group's flag
    for i, raw in enumerate(items):
        item = dict(raw)
        kind = str(item.get("kind") or "value")
        res: dict[str, Any] = {"i": i, "kind": kind, "doc_id": item.get("doc_id"), "locator": item.get("locator"),
                               "warnings": []}
        for k in ("quantity", "value", "units", *CONDITIONS):
            if item.get(k) is not None:
                res[k] = item[k]
        results.append(res)
        if kind == "computed":
            _check_computed(item, res, calc_runs or {})
            continue
        if kind == "chart":
            _check_chart(con, cfg, item, res)
            continue
        if kind not in ("value", "quote"):
            res.update(status="FAIL", reason=f"unknown kind {kind!r} (value, quote, computed, chart)")
            continue
        try:
            doc_id = int(item["doc_id"])
            locator = str(item["locator"])
        except (KeyError, TypeError, ValueError):
            res.update(status="FAIL", reason="doc_id and locator are required")
            continue
        doc = docs.get(doc_id) or docs.setdefault(doc_id, _Doc(con, doc_id))
        if doc.row is None:
            res.update(status="FAIL", reason=f"document {doc_id} not in the index")
            continue
        res["title"] = doc.row["title"]
        flag = effective_flag(doc.row["classification_flag"], doc_id, groups)
        if flag:
            res["flag"] = flag
            res["warnings"].append(f"FLAGGED {flag}: label it, local-only (G2.4)")
        sel = doc.at(locator)
        if not sel:
            res.update(status="FAIL", reason=f"locator '{locator}' not found in document {doc_id}",
                       available=[r["locator"] for r in doc.chunks[:20]])
            continue
        if kind == "quote":
            _check_quote(sel, item, res)
        else:
            _check_value(con, doc, sel, item, res)

    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "COMPUTED")}
    overall = "FAIL" if counts["FAIL"] or not results else "PASS"
    digest = hashlib.sha256(json.dumps([items, [r["status"] for r in results]], sort_keys=True,
                                       default=str).encode()).hexdigest()[:12]
    return {"overall": overall, "counts": counts, "verification_id": f"v-{digest}", "items": results, "rule": RULE}
