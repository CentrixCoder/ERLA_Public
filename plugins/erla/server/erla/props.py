"""A3: structured material-property store built from markdown table chunks.

Sources: PDF table chunks (A2), DOCX tables, XLSX/XLS/CSV sheet blocks - any chunk whose text holds a
markdown table. Two layouts are recognised:

* rows = properties (MMPDS / MIL-HDBK-5 "Design Mechanical and Physical Properties"): context rows
  (Alloy, Specification, Form, Condition/Temper, Thickness, Basis) give per-column conditions;
  property rows ("Ftu, ksi:" with child rows L / LT / ST or "(e/D = 2.0)") give values.
* columns = properties (spreadsheets, simple handbook tables): a header row names properties with
  units ("Fty (ksi)"); other header columns (Alloy, Temper, Basis, Form, Thickness) give conditions.

Guardrails: values are stored exactly as printed (`value_text`) next to the parsed number; nothing is
interpolated or inferred except per-column conditions copied across a merged header cell, which is
recorded in `flags` (CONTEXT_FILLED). Units from a label are never altered; a printed multiplier
("E, 10^3 ksi") is kept in `units` and applied only to `value_si`. Every record carries its chunk
locator so the value can be read in its source (R3.3) - this store is an index, not a source.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from . import quality, units
from .models import PropertyRecord

VERSION = "props-3"     # 0.3.1: no condition copied into a column between two different merged headings
#                         (props-2, 0.2.16: spanned condition cells split by column; wrapped thickness ranges)
RANGE_UNIT = {"E": "msi", "Ec": "msi", "G": "msi", "KIc": "ksi√in", "density": "lb/in3"}

# Order matters: the first match wins (specific before general).
QUANTITY_PATTERNS = [
    ("Fcy", r"^f\s*cy\b|^fcy\b|compressive yield"),
    ("Fbru", r"^f\s*bru\b|^fbru\b|bearing ultimate"),
    ("Fbry", r"^f\s*bry\b|^fbry\b|bearing yield"),
    ("Fsu", r"^f\s*su\b|^fsu\b|ultimate shear strength|shear ultimate|shear strength"),
    ("Ftu", r"^f\s*tu\b|^ftu\b|ultimate tensile strength|tensile ultimate|tensile strength|^uts\b|^sut\b|^rm\b"),
    ("Fty", r"^f\s*ty\b|^fty\b|tensile yield|^yield strength|^yield\b|^sy\b|^rp0\.?2\b"),
    ("elongation", r"^e\s*,\s*percent|^elongation|^elong\b|^e\s*,\s*%|^e\s*\(%\)"),
    ("Ec", r"^ec\b|compressive modulus|modulus of elasticity in compression"),
    ("KIc", r"^k\s*ic\b|^kic\b|fracture toughness"),
    ("E", r"^e\s*[,(]|^e$|young'?s modulus|elastic modulus|modulus of elasticity|tensile modulus"),
    ("G", r"^g\s*[,(]|^g$|shear modulus|modulus of rigidity"),
    ("poisson", r"^[µμν]\s*$|^[µμν]\s*[,(]|^mu\b|poisson"),
    ("density", r"^[ωρ]\s*,|^density|^w\s*,\s*lb"),
]
QUANTITY_RE = [(q, re.compile(p, re.I)) for q, p in QUANTITY_PATTERNS]
CONTEXT_RE = {
    "material": re.compile(r"^(alloy|material|designation|steel|grade)\b", re.I),
    "spec": re.compile(r"^(specification|spec\.?)\b", re.I),
    "form": re.compile(r"^(form|product form|product)\b", re.I),
    "temper": re.compile(r"^(condition|temper|heat treat(?:ment)?)\b", re.I),
    "thickness": re.compile(r"^(thickness|diameter|thickness or diameter)\b", re.I),
    "basis": re.compile(r"^basis\b", re.I),
    "temperature": re.compile(r"^(temp(?:erature)?|test temperature)\b", re.I),
    "direction": re.compile(r"^(direction|grain direction|orientation)\b", re.I),
}
FILLABLE = {"material", "spec", "form", "temper"}      # merged header cells span to the right
GROUP_FIELDS = ("material", "form", "temper")          # a merged value cell spans columns of one heading group
# Constants that do not vary with thickness or basis within one material/form/temper: printed as merged
# cells. Strengths and elongation do vary, and a blank in their rows is usually a '...' the text lost.
SPANNING = {"E", "Ec", "G", "poisson", "density"}
DIMENSIONLESS = {"poisson", "elongation", "Kt", "Kf"}
BASIS = re.compile(r"^(A|B|S|typical|typ\.?)\s*([a-z])?$", re.I)
LABEL_BASIS = re.compile(r"\(\s*([ABS])\s*-\s*basis\s*\)", re.I)        # 'L (S-basis)', 'Fsu, ksi (S-basis):'
DIRECTION = re.compile(r"^(L|LT|ST|L-T|T-L|S-L|longitudinal|long transverse|short transverse|transverse)"
                       r"\s*(?:\(\s*[ABS]\s*-\s*basis\s*\))?\s*:?$", re.I)
SPAN = "\u2063"                                                          # set by pdf.aligned_grid
QUALIFIER = re.compile(r"^\(?\s*e\s*/\s*d\s*=\s*[\d.]+\s*\)?$", re.I)
NUMBER = re.compile(r"^([-+]?\d+(?:\.\d+)?)\s*([a-z]{1,2})?$")
MULTIPLIER = re.compile(r"\b10\s*(?:\^\s*)?(3|6|-3|-6)\s+")
MD_ROW = re.compile(r"^\|(.*)\|\s*$")
CAPTION_MAT = re.compile(r"properties\s+(?:of|for)\s+(.+?)\s*$", re.I)
DESIGNATION = re.compile(r"\b(\d{4}-[TOHF]\d{0,5}\w*|Ti-\d+Al-\d+\w*(?:-\d+\w*)*|\d{2}-\d{1,2}\s?PH|AISI\s?\d{4}\w?|"
                         r"300M|D6AC|Hy-Tuf|A-286|Inconel\s?\d+|\d{4}V?)\b", re.I)


def parse_markdown_tables(text: str) -> list[tuple[str, list[list[str]]]]:
    """All markdown tables in a chunk -> [(caption_or_preceding_text, rows)]."""
    out, cur, pre = [], [], []
    for line in text.splitlines():
        m = MD_ROW.match(line.strip())
        if m:
            cells = [c.strip() for c in m.group(1).split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
                continue
            cur.append(cells)
        else:
            if cur:
                out.append((" ".join(pre[-2:]), cur))
                cur, pre = [], []
            if line.strip():
                pre.append(line.strip())
    if cur:
        out.append((" ".join(pre[-2:]), cur))
    return out


def parse_label(label: str) -> tuple[str | None, str | None, float, str]:
    """'Ftu, ksi:' -> ('Ftu', 'ksi', 1.0, 'Ftu, ksi'); 'E, 103 ksi' -> ('E', 'ksi', 1e3, ...).
    Returns (quantity, unit, multiplier, cleaned label)."""
    lab = label.strip().rstrip(":").strip()
    if not lab:
        return None, None, 1.0, ""
    mult = 1.0
    lab_u = LABEL_BASIS.sub("", lab).strip().rstrip(":").strip()
    m = MULTIPLIER.search(lab_u)
    if m:
        mult = 10.0 ** int(m.group(1))
        lab_u = lab_u[:m.start()] + lab_u[m.end():]
    unit, _rest = units.unit_from_label(lab_u)
    q = next((name for name, rx in QUANTITY_RE if rx.search(lab)), None)
    return q, unit, mult, lab


def _number(cell: str) -> tuple[float | None, str | None]:
    """'220g' -> (220.0, 'g'); '...' / 'See Figure' -> (None, None)."""
    c = cell.strip().replace(",", "")
    m = NUMBER.match(c)
    if not m:
        return None, None
    return float(m.group(1)), (m.group(2) or None)


TEMPER_TOKEN = re.compile(r"(?<![\w-])(?:T\d{1,5}[0-9A-Za-z]*|H\d{2,4}|(?:SCT|RH|TH|CH)\s?\d{3,4}|O|F)(?![\w-])")
OPEN_RANGE = re.compile(r"^[#<≤>]?\s*\d*\.?\d+\s*-$")
JOINT_LIST = re.compile(r",|/|&|\b(?:and|or)\b", re.I)   # "T6, T62a", "T73 or T7351": all of them apply


def _temper_tokens(text: str) -> list[str]:
    """'Solution treated H900 H950' -> ['Solution treated', 'H900', 'H950'] (words between temper
    designations form one condition)."""
    out, pos = [], 0
    for m in TEMPER_TOKEN.finditer(text):
        gap = text[pos:m.start()].strip(" ;")
        if re.search(r"[A-Za-z0-9]", gap):        # "–T81": a dash alone is not a condition
            out.append(gap)
        out.append(m.group(0))
        pos = m.end()
    tail = text[pos:].strip(" ;")
    if tail:
        out.append(tail)
    return out


def _split_spanned(vals: dict[int, str], filled_cols: set[int], ncol: int) -> tuple[set[int], dict[int, str]]:
    """A condition row printed as one cell over several columns ("Solution treated H900 H950 ...
    H1150" over 7 columns) reaches us as the same text in every column. Give each column its own
    condition when the text lists exactly one per column; flag the columns when it lists a different
    number (never guess). A comma/"and" list ("T6, T62a") applies to every column and is kept.
    Copied (filled) values are not spans. Changes ``vals`` in place; returns (split, unresolved)."""
    split: set[int] = set()
    unresolved: dict[int, str] = {}
    j = 1
    while j < ncol:
        v = vals.get(j)
        k = j
        while v and j not in filled_cols and k + 1 < ncol and vals.get(k + 1) == v and k + 1 not in filled_cols:
            k += 1
        n = k - j + 1
        if v and n > 1 and not JOINT_LIST.search(v):
            toks = _temper_tokens(v)
            if len(toks) == n:
                for off, t in enumerate(toks):
                    vals[j + off] = t
                    split.add(j + off)
            elif len(toks) > 1:
                for c in range(j, k + 1):
                    unresolved[c] = (f"TEMPER_UNRESOLVED: one condition cell spans {n} columns and lists "
                                     f"{len(toks)} conditions; read the column heading in the source")
        j = k + 1
    return split, unresolved


def _complete_open_ranges(vals: dict[int, str]) -> list[int]:
    """'0.012-' spanning an A and a B column, with its second line printed under the first column
    only: the second column takes the completed range from its left neighbour. Returns the columns
    completed (they are copies, flagged CONTEXT_FILLED)."""
    done = []
    for j in sorted(vals):
        v = vals[j].strip()
        if not v.endswith("-"):
            continue
        for i in range(j - 1, 0, -1):
            w = vals.get(i, "").strip()
            if w.startswith(v) and not w.endswith("-"):
                vals[j] = w
                done.append(j)
                break
    return done


def _same(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip().lower()


def _range_start(t: str | None) -> float | None:
    """Lower end of a printed thickness: '0.020-0.249' -> 0.020; '≤1.500', '#8.000c', 'Up to' -> 0."""
    t = (t or "").strip()
    if re.match(r"^(?:#|≤|<|up\s+to\b)", t, re.I):
        return 0.0
    m = re.match(r"^(\d*\.?\d+)", t)
    return float(m.group(1)) if m else None


def _split_by_thickness(cols: dict[int, tuple[int, int, str, str]], thick: dict[int, str]) -> dict[int, str]:
    """Decide columns lying between two different headings from printed evidence: a product group's
    thickness ranges ascend, so where they start again (0.020-0.249, then 0.010-0.019) a new group
    begins. Between a left heading (column L) and a right one (R), exactly one such restart decides
    each column; none, several, or a column without a readable thickness leave the columns undecided."""
    out: dict[int, str] = {}
    for left, right, lv, rv in {(a, b, x, y) for a, b, x, y in cols.values()}:
        starts = [_range_start(thick.get(j)) for j in range(left, right + 1)]
        if any(s is None for s in starts):
            continue
        breaks = [left + k for k in range(len(starts) - 1) if starts[k + 1] < starts[k]]   # type: ignore[operator]
        if len(breaks) != 1:
            continue
        for j in range(left + 1, right):
            out[j] = lv if j <= breaks[0] else rv
    return out


def _material_from_caption(caption: str) -> str | None:
    m = CAPTION_MAT.search(caption or "")
    return m.group(1).strip(" .") if m else None


def _temper_from(designation: str | None) -> str | None:
    m = re.search(r"-(T\d{1,5}\w*|O|H\d+|F)\b", designation or "")
    return m.group(1) if m else None


def _record(quantity, value, vtext, unit, mult, ctx, row_label, col_label, caption, footnote, extra_flags,
            chunk_quality, ocr) -> PropertyRecord:
    flags = list(extra_flags)
    if footnote:
        flags.append(f"FOOTNOTE:{footnote}")
    vsi, usi = None, None
    if unit:
        si = units.to_si(value * mult, unit)
        if si:
            vsi, usi = round(si[0], 6), si[1]
    # A6 range check in one canonical unit per quantity, after applying a printed multiplier
    rng_val, rng_unit = value * mult, unit or ""
    target = RANGE_UNIT.get(quantity, "ksi" if quantity.startswith("F") else None)
    if unit and target and units.check_dimension(quantity, unit):
        try:
            rng_val, rng_unit = units.convert(value * mult, unit, target).value, target
        except ValueError:
            pass
    flags += quality.value_flags(quantity, rng_val, vtext, rng_unit, chunk_quality)
    exp = round(math.log10(mult)) if mult != 1.0 else 0
    return {"quantity": quantity, "value": value, "value_text": vtext,
            "units": (f"10^{exp} {unit}" if exp and unit else unit),
            "value_si": vsi, "units_si": usi,
            "material": ctx.get("material"), "temper": ctx.get("temper") or _temper_from(ctx.get("material")),
            "form": ctx.get("form"), "thickness": ctx.get("thickness"), "direction": ctx.get("direction"),
            "temperature": ctx.get("temperature"), "basis": ctx.get("basis"),
            "row_label": row_label, "col_label": col_label, "caption": caption or None,
            "flags": flags, "ocr": int(bool(ocr))}


def _basis(v: str | None) -> tuple[str | None, str | None]:
    m = BASIS.match((v or "").strip())
    if not m:
        return (v or None), None
    b = m.group(1)
    return ("typical" if b.lower().startswith("typ") else b.upper()), m.group(2)


def _usable(q: str, unit: str | None) -> bool:
    """R4.1: a value without units is not stored unless the quantity is dimensionless."""
    return bool(unit) or q in DIMENSIONLESS


def rows_layout(grid: list[list[str]], caption: str, cq: dict | None, ocr: bool,
                columns: dict[int, dict[str, str | None]] | None = None) -> list[PropertyRecord]:
    """Rows = properties (MMPDS style). Column 0 = labels, columns 1.. = product/condition columns.

    Header region (before the first property row): labelled context rows (Specification, Form,
    Temper, Thickness, Basis) set per-column conditions; unlabelled lines are continuations of the
    adjacent context row (a line directly above an empty context row belongs to it, otherwise to the
    row above). A single context value that does not span columns explicitly is applied to all
    columns and flagged CONTEXT_FILLED; a value marked as spanning (aligned_grid) needs no flag.
    ``columns``, when given, receives each data column's heading group (material, form, temper) as
    read once the header is complete (``spanned_records``); it does not change the records."""
    ncol = max(len(r) for r in grid)
    grid = [r + [""] * (ncol - len(r)) for r in grid]
    spans = {(i, j) for i, r in enumerate(grid) for j, c in enumerate(r) if SPAN in c}
    grid = [[c.replace(SPAN, " ").strip() for c in r] for r in grid]
    grid = [[re.sub(r"\s+", " ", c) for c in r] for r in grid]
    ndata = ncol - 1
    ctx: dict[str, dict[int, str]] = {f: {} for f in CONTEXT_RE}
    filled: set[tuple[str, int]] = set()      # (field, column) copied, not printed
    cap_mat = _material_from_caption(caption)
    out: list[dict] = []
    cur = None                        # (quantity, unit, mult, label)
    pending: dict[int, str] = {}
    last_field = None
    seen_prop = False
    split_cols: set[int] = set()
    unresolved: dict[int, str] = {}
    # column between two different headings -> (left column, right column, left heading, right heading)
    ambiguous: dict[str, dict[int, tuple[int, int, str, str]]] = {f: {} for f in FILLABLE}
    resolved: set[tuple[str, int]] = set()

    def field_of(label: str) -> str | None:
        return next((f for f, rx in CONTEXT_RE.items() if rx.search(label)), None) if label else None

    def set_ctx(field: str, vals: dict[int, str], i: int) -> None:
        if field in FILLABLE and len(vals) == 1 and ndata > 1:
            (j0, v0), = vals.items()
            for j in range(1, ncol):
                if j != j0:
                    ctx[field][j] = v0
                    if (i, j) not in spans:
                        filled.add((field, j))
        elif field in FILLABLE:
            # A merged heading is often printed centred over its columns ("T4" over 2, "T6" over the next
            # 6 reaches us in the 5th): an unlabelled column between two different headings may belong to
            # either. Copying the left one put T6 values under T4 (props-2). Now such a column is decided
            # by the thickness row once the header is read (_split_by_thickness), or gets neither heading
            # and a flag (props-3). Columns after the last heading, or between two equal ones, take it.
            printed = sorted(vals)
            for j in range(1, ncol):
                left = max((k for k in printed if k < j), default=None)
                if j in vals or left is None:
                    continue
                right = min((k for k in printed if k > j), default=None)
                if right is not None and _same(vals[right]) != _same(vals[left]):
                    ambiguous[field][j] = (left, right, vals[left], vals[right])
                    continue
                ctx[field][j] = vals[left]
                filled.add((field, j))
        for j, v in vals.items():
            ctx[field][j] = v
            filled.discard((field, j))
            ambiguous.get(field, {}).pop(j, None)

    def footnote_row(vals: dict[int, str]) -> bool:
        return all(re.fullmatch(r"[a-z]{1,2}(,[a-z])*", v) for v in vals.values())

    for i, r in enumerate(grid):
        label = r[0].strip()
        field = field_of(label)
        vals = {j: v for j, v in enumerate(r[1:], 1) if v}
        if field and not seen_prop:
            for j, v in pending.items():
                vals[j] = f"{v} {vals[j]}" if j in vals else v
            pending = {}
            last_field = field
            if vals:
                set_ctx(field, vals, i)
            continue
        if not label:
            if not seen_prop and vals and not footnote_row(vals):
                nxt = grid[i + 1] if i + 1 < len(grid) else None
                nf = field_of(nxt[0].strip()) if nxt else None
                if last_field and all(ctx[last_field].get(j, "").endswith("-") for j in vals):
                    # the second line of a wrapped range ("0.250-" / "0.499") finishes the row above,
                    # even when an empty context row (Basis) follows (props-2)
                    for j, v in vals.items():
                        ctx[last_field][j] = f"{ctx[last_field][j]} {v}"
                elif nf == "thickness" and all(OPEN_RANGE.match(v) for v in vals.values()):
                    # "9.0-" just above the Thickness row starts that row's range, not the condition's
                    for j, v in vals.items():
                        pending[j] = f"{pending[j]} {v}" if j in pending else v
                elif nf and (not any(nxt[1:]) or last_field is None):
                    for j, v in vals.items():
                        pending[j] = f"{pending[j]} {v}" if j in pending else v
                elif last_field:
                    for j, v in vals.items():
                        prev = ctx[last_field].get(j)
                        ctx[last_field][j] = v if (prev is None or (last_field, j) in filled) else f"{prev} {v}"
                        filled.discard((last_field, j))
                        ambiguous.get(last_field, {}).pop(j, None)
                else:
                    for j, v in vals.items():
                        pending[j] = f"{pending[j]} {v}" if j in pending else v
            continue
        q, unit, mult, lab = parse_label(label)
        child = None
        if q:
            if not seen_prop:                 # header complete: resolve spanned condition cells once
                split_cols, unresolved = _split_spanned(ctx["temper"], {c for f, c in filled if f == "temper"}, ncol)
                for c in _complete_open_ranges(ctx["thickness"]):
                    filled.add(("thickness", c))
                for f, cols in ambiguous.items():
                    for j, v in _split_by_thickness(cols, ctx["thickness"]).items():
                        if not ctx[f].get(j):
                            ctx[f][j] = v
                            filled.add((f, j))
                            resolved.add((f, j))
                            del cols[j]
                if columns is not None:          # an unresolved temper is its own group, never a wildcard
                    columns.update({j: {f: (f"?{j}" if f == "temper" and j in unresolved else ctx[f].get(j))
                                        for f in GROUP_FIELDS} for j in range(1, ncol)})
            cur = (q, unit, mult, lab)
            seen_prop = True
        elif cur and (DIRECTION.match(label) or QUALIFIER.match(label)):
            child = label
        else:
            if seen_prop:
                cur = None            # section headings ("Physical Properties:"), footnotes
            continue
        q, unit, mult, plabel = cur
        if not _usable(q, unit):
            continue
        override = LABEL_BASIS.search(child or "") or LABEL_BASIS.search(plabel)
        nums = [(j, *_number(r[j])) for j in range(1, ncol)]
        nums = [(j, v, f) for j, v, f in nums if v is not None]
        for j, v, foot in nums:
            basis, bfoot = _basis(ctx["basis"].get(j))
            extra = []
            if override:
                basis = override.group(1).upper()
                extra.append("BASIS_FROM_LABEL")
            dm = DIRECTION.match(child) if child else None
            thick = ctx["thickness"].get(j)
            if thick and re.match(r"^\s*#\s*\d", thick):
                # MMPDS/MIL-HDBK-5 print '≤' in a Symbol font that extracts as '#'
                thick = "≤" + thick.strip().lstrip("#").strip()
                extra.append("GLYPH_MAPPED: '#' read as '≤' in thickness")
            c = {"material": ctx["material"].get(j) or cap_mat, "form": ctx["form"].get(j),
                 "temper": ctx["temper"].get(j), "thickness": re.sub(r"\s*-\s*", "-", thick) if thick else None,
                 "basis": basis, "temperature": ctx["temperature"].get(j),
                 "direction": dm.group(1).upper() if dm else None}
            if any((f, j) in filled for f in ("material", "spec", "form", "temper", "thickness")):
                extra.append("CONTEXT_FILLED")
            if j in split_cols:
                extra.append("TEMPER_FROM_SPAN: condition read by column position from one heading cell")
            if j in unresolved:
                extra.append(unresolved[j])
            for f in sorted(ambiguous):
                if j in ambiguous[f] and not ctx[f].get(j):
                    _, _, a, b = ambiguous[f][j]
                    extra.append(f"CONDITION_AMBIGUOUS: {f} - column lies between the merged headings "
                                 f"'{a}' and '{b}'; read the column heading in the source")
                elif (f, j) in resolved:
                    extra.append(f"CONDITION_FROM_THICKNESS: {f} '{ctx[f][j]}' - column between two headings, "
                                 "assigned where the thickness ranges start again")
            if bfoot:
                extra.append(f"BASIS_FOOTNOTE:{bfoot}")
            if len(nums) == 1 and ndata >= 3 and j not in (1, ncol - 1):
                extra.append("POSSIBLE_SPAN: single value in a multi-column row; may apply to all columns")
            col = "; ".join(x for x in (ctx["spec"].get(j), c["form"], c["temper"], c["thickness"],
                                        f"basis {ctx['basis'].get(j)}" if ctx["basis"].get(j) else None) if x)
            row = plabel + (f" / {child}" if child else "")
            out.append(_record(q, v, r[j].strip(), unit, mult, c, row, col or f"column {j}", caption,
                               foot, extra, cq, ocr))
    return out


def cols_layout(grid: list[list[str]], caption: str, cq: dict | None, ocr: bool) -> list[PropertyRecord]:
    """Columns = properties: header row names properties with units; context columns give conditions."""
    hdr_i = next((i for i, r in enumerate(grid[:3])
                  if sum(1 for c in r if parse_label(c)[0]) >= 1), None)
    if hdr_i is None:
        return []
    hdr = grid[hdr_i]
    pcols = {j: parse_label(c) for j, c in enumerate(hdr) if parse_label(c)[0]}
    ccols = {j: f for j, c in enumerate(hdr) for f, rx in CONTEXT_RE.items() if c and rx.search(c) and j not in pcols}
    cap_mat = _material_from_caption(caption)
    out: list[dict] = []
    for r in grid[hdr_i + 1:]:
        r = r + [""] * (len(hdr) - len(r))
        ctx = {f: (r[j] or None) for j, f in ccols.items()}
        if not ctx.get("material"):
            first = r[0] if 0 not in pcols else ""
            m = DESIGNATION.search(first or "")
            ctx["material"] = (first if m else None) or cap_mat
        basis, bfoot = _basis(ctx.get("basis"))
        ctx["basis"] = basis
        if ctx.get("direction") and not DIRECTION.match(ctx["direction"]):
            ctx["direction"] = None
        for j, (q, unit, mult, lab) in pcols.items():
            if j >= len(r) or not _usable(q, unit):
                continue
            v, foot = _number(r[j])
            if v is None:
                continue
            extra = [f"BASIS_FOOTNOTE:{bfoot}"] if bfoot else []
            out.append(_record(q, v, r[j].strip(), unit, mult, ctx, lab,
                               "; ".join(f"{f}={ctx[f]}" for f in ("material", "temper", "form") if ctx.get(f)),
                               caption, foot, extra, cq, ocr))
    return out


def extract_from_text(text: str, chunk_quality: dict | None = None, ocr: bool = False,
                      caption: str | None = None) -> list[PropertyRecord]:
    out: list[PropertyRecord] = []
    for pre, grid in parse_markdown_tables(text):
        if len(grid) < 2:
            continue
        cap = caption or pre
        labelled = sum(1 for r in grid if r and parse_label(r[0])[0])
        recs = rows_layout(grid, cap, chunk_quality, ocr) if labelled >= 2 else []
        if not recs:
            recs = cols_layout(grid, cap, chunk_quality, ocr)
        out += recs
    return out


def _fill_spans(grid: list[list[str]], columns: dict[int, dict[str, str | None]]) -> list[list[str]] | None:
    """The grid with each merged value cell repeated over the blank cells it spans, or None if none.

    A value printed once over several columns (E over every sheet column) reaches the text in one
    column, the others blank. Only the constants in SPANNING span: extraction drops '...' cells, so a
    blank in a strength or elongation row is no evidence (a real MMPDS page prints '...' in the B column
    of a D6AC Fcy row that reaches the text blank). A cell spans the blank cells next to it with the same
    heading group (material, form, temper), provided the group holds no other entry in that row. A
    column whose heading was not read (CONDITION_AMBIGUOUS) is never spanned into. A blank cell two
    entries could span keeps its blank unless both print the same number."""
    def clean(c: str) -> str:
        return re.sub(r"\s+", " ", c.replace(SPAN, " ")).strip()

    headed = [f for f in GROUP_FIELDS if any(c.get(f) for c in columns.values())]

    def group(j: int) -> tuple[str, ...] | None:
        """The column's heading group; None when a heading the table prints was not read for it."""
        k = columns.get(j, {})
        return None if any(not k.get(f) for f in headed) else tuple(_same(str(k[f])) for f in headed)

    def compatible(a: int, b: int) -> bool:
        return group(a) is not None and group(a) == group(b)

    ncol = max(len(r) for r in grid)
    out = [[clean(c) for c in r] + [""] * (ncol - len(r)) for r in grid]
    first = next((i for i, r in enumerate(out) if parse_label(r[0])[0]), None)
    if first is None:
        return None
    changed = False
    for r in out[first:]:
        if parse_label(r[0])[0] not in SPANNING:
            continue
        entries = [j for j in range(1, ncol) if r[j]]
        if not any(_number(r[j])[0] is not None for j in entries):
            continue
        claims: dict[int, set[int]] = {}           # blank column -> entries that could span it
        for j in entries:
            lo = hi = j
            while lo - 1 >= 1 and compatible(lo - 1, j):
                lo -= 1
            while hi + 1 < ncol and compatible(hi + 1, j):
                hi += 1
            if any(k != j and r[k] for k in range(lo, hi + 1)):
                continue                               # another entry in the same group: not a span
            for k in range(lo, hi + 1):
                if k != j:
                    claims.setdefault(k, set()).add(j)
        for k, srcs in claims.items():
            vals = {_number(r[j])[0] for j in srcs}
            if len(vals) == 1 and None not in vals:
                r[k] = r[min(srcs, key=lambda j: (abs(j - k), j))]
                changed = True
    return out if changed else None


def spanned_records(text: str, caption: str | None = None) -> list[PropertyRecord]:
    """Records for the columns a merged value cell spans (0.4.3): E printed once over every sheet column
    is stored for the column it reached in the text only. These records are not stored (the store keeps
    the printed cell); verify_packet uses them so a spanning value counts for every column it spans.
    Each carries a SPANNED flag."""
    out: list[PropertyRecord] = []
    for pre, grid in parse_markdown_tables(text):
        if len(grid) < 2 or sum(1 for r in grid if r and parse_label(r[0])[0]) < 2:
            continue
        cols: dict[int, dict[str, str | None]] = {}
        base = rows_layout(grid, caption or pre, None, False, columns=cols)
        filled = _fill_spans(grid, cols) if cols else None
        if filled is None:
            continue

        def key(r: PropertyRecord) -> tuple:
            return tuple(r.get(f) for f in ("quantity", "value", "row_label", "col_label", "basis", "thickness"))

        have: dict[tuple, int] = {}
        for r in base:
            have[key(r)] = have.get(key(r), 0) + 1
        for r in rows_layout(filled, caption or pre, None, False):
            if have.get(key(r)):
                have[key(r)] -= 1
                continue
            r["flags"] = [*r["flags"], "SPANNED: printed once across several columns (a merged cell)"]
            out.append(r)
    return out


# ----------------------------------------------------------------------------- database
COLS = ("quantity", "value", "value_text", "units", "value_si", "units_si", "material", "temper", "form",
        "thickness", "direction", "temperature", "basis", "row_label", "col_label", "caption", "flags", "ocr")


def extract_document(con, doc_id: int) -> int:
    """(Re)build the property records of one document from its table chunks (index-only write)."""
    con.execute("DELETE FROM properties WHERE doc_id=?", (doc_id,))
    ocr_doc = con.execute("SELECT ocr_used FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for ch in con.execute("SELECT chunk_id, locator, text, quality_json FROM chunks WHERE doc_id=? "
                          "AND has_table=1 AND instr(text, '|') > 0", (doc_id,)).fetchall():
        try:
            cq = json.loads(ch["quality_json"]) if ch["quality_json"] else None
        except ValueError:
            cq = None
        ocr = "[OCR]" in (ch["locator"] or "") or bool(ocr_doc and ocr_doc[0] and cq and "ocr_conf" in cq)
        for rec in extract_from_text(ch["text"] or "", cq, ocr):
            rec["flags"] = json.dumps(rec["flags"]) if rec["flags"] else None
            con.execute(f"INSERT INTO properties(doc_id, chunk_id, locator, {', '.join(COLS)}, extracted_at) "
                        f"VALUES (?,?,?,{','.join('?' * len(COLS))},?)",
                        (doc_id, ch["chunk_id"], ch["locator"], *[rec[c] for c in COLS], now))
            n += 1
    return n


def rebuild_if_stale(con, log=print) -> dict | None:
    """Rebuild all property records when the stored ones were made by another parser VERSION.

    Reads only table chunks already in the index (no library file), so it is cheap; records keep
    their documents and locators, only the parsed conditions change. Returns None when current."""
    from .store import has_table
    if not has_table(con, "properties") or not has_table(con, "meta"):
        return None
    row = con.execute("SELECT value FROM meta WHERE key='props_version'").fetchone()
    if row and row[0] == VERSION:
        return None
    docs = [d for (d,) in con.execute(
        "SELECT DISTINCT c.doc_id FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
        "WHERE c.has_table=1 AND instr(c.text, '|') > 0 AND d.extract_status != 'duplicate'")]
    log(f"properties: parser {VERSION} - rebuilding records of {len(docs)} documents from stored tables")
    n = sum(extract_document(con, d) for d in docs)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('props_version', ?)", (VERSION,))
    con.commit()
    return {"version": VERSION, "documents": len(docs), "records": n}


def query(con, quantity: str | None = None, material: str | None = None, basis: str | None = None,
          temper: str | None = None, form: str | None = None, direction: str | None = None,
          tiers: list[str] | None = None, exclude_flagged: bool = False, k: int = 50) -> list[dict]:
    """Property records joined with their source document, T1 first. Substring match on text fields."""
    from .store import has_table
    if not has_table(con, "properties"):
        return []
    where, args = ["d.extract_status != 'duplicate'"], []
    if quantity:
        q = canonical_quantity(quantity)
        where.append("p.quantity = ?")
        args.append(q)
    for col, val in (("p.material", material), ("p.temper", temper), ("p.form", form)):
        if val:
            where.append(f"({col} LIKE ? OR p.caption LIKE ?)" if col == "p.material" else f"{col} LIKE ?")
            args += [f"%{val}%", f"%{val}%"] if col == "p.material" else [f"%{val}%"]
    if basis:
        where.append("p.basis = ?")
        args.append(basis.upper() if len(basis) == 1 else basis)
    if direction:
        where.append("upper(p.direction) = ?")
        args.append(direction.upper())
    if tiers:
        where.append(f"coalesce(d.authority_tier,'T5') IN ({','.join('?' * len(tiers))})")
        args += tiers
    if exclude_flagged:
        where.append("d.classification_flag IS NULL")
    rows = con.execute(
        "SELECT p.*, d.title, d.rel_path, d.authority_tier AS tier, d.classification_flag AS flag, d.year, "
        "d.standard_id, d.revision FROM properties p JOIN documents d ON d.doc_id = p.doc_id "
        f"WHERE {' AND '.join(where)} ORDER BY coalesce(d.authority_tier,'T5'), p.flags IS NOT NULL, "
        "d.year DESC, p.prop_id LIMIT ?", (*args, k)).fetchall()
    from .classify import copy_group_flags, effective_flag
    groups = copy_group_flags(con)       # G2.5: a copy of a flagged document is labelled and filtered as one
    out = []
    for r in rows:
        d = dict(r)
        d["flag"] = effective_flag(d["flag"], d["doc_id"], groups)
        if exclude_flagged and d["flag"]:
            continue
        d["flags"] = json.loads(d["flags"]) if d["flags"] else []
        out.append(d)
    return out


def canonical_quantity(name: str) -> str:
    """User spelling ('ftu', 'UTS', 'yield strength', "Young's modulus") -> stored quantity code."""
    n = name.strip()
    for q, _ in QUANTITY_PATTERNS:
        if n.lower() == q.lower():
            return q
    q, _u, _m, _l = parse_label(n)
    return q or n


def stats(con) -> dict:
    from .store import has_table
    if not has_table(con, "properties"):
        return {"records": 0, "documents": 0, "flagged": 0, "by_quantity": {}}
    r = con.execute("SELECT count(*), count(DISTINCT doc_id), sum(flags IS NOT NULL) FROM properties").fetchone()
    by_q = dict(con.execute("SELECT quantity, count(*) FROM properties GROUP BY quantity ORDER BY 2 DESC").fetchall())
    return {"records": r[0], "documents": r[1], "flagged": r[2] or 0, "by_quantity": by_q}
