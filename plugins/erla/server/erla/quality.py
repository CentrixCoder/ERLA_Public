"""A5 OCR word-confidence flags and A6 numeric sanity checks.

Nothing here changes text or values: it only produces flags that travel with chunks and property
records (G3.5 / G3.6). Flag codes:
  OCR_LOW_CONF  numeric token Tesseract read with confidence below ocr.low_conf_numeric
  OCR_CONFUSION numeric token containing a letter commonly confused with a digit (O/0, l/I/1, S/5, B/8, Z/2)
  RANGE         value outside the plausible range for its quantity and unit (possible lost decimal point)
  UNIT_DIM      unit dimension does not match the quantity (e.g. Ftu in inches)
"""

from __future__ import annotations

import re

NUMERIC_TOKEN = re.compile(r"^[+\-±]?\d[\d.,]*(?:[eE][+\-]?\d+)?%?$")
# a token that is mostly digits but contains one confusable letter: 7O75, 1l.5, 6S.2, 0.B5, 4Z0
CONFUSION = re.compile(r"^(?=(?:.*\d){2})[+\-]?[\dOoIlSBZ]+(?:[.,][\dOoIlSBZ]+)?$")
CONFUSABLE = set("OoIlSBZ")
# alloy / spec designations legitimately mix letters and digits; never flag these
DESIGNATION = re.compile(r"^(?:[A-Z]{1,4}-?\d|\d{4}-[TOHF]\d|\d+[A-Z]{1,3}$)")

# Plausible ranges for isotropic metals and common engineering materials, in the unit given.
# Wide on purpose: they catch decimal-point loss / digit confusion, not marginal data.
RANGES = {
    ("Ftu", "ksi"): (1, 400), ("Fty", "ksi"): (1, 350), ("Fcy", "ksi"): (1, 350), ("Fsu", "ksi"): (1, 250),
    ("Fbru", "ksi"): (1, 700), ("Fbry", "ksi"): (1, 600),
    ("Ftu", "MPa"): (7, 2800), ("Fty", "MPa"): (7, 2400), ("Fcy", "MPa"): (7, 2400), ("Fsu", "MPa"): (7, 1700),
    ("Fbru", "MPa"): (7, 4800), ("Fbry", "MPa"): (7, 4100),
    ("E", "msi"): (0.1, 70), ("E", "ksi"): (100, 70000), ("E", "GPa"): (0.7, 480), ("E", "MPa"): (700, 480000),
    ("Ec", "msi"): (0.1, 70), ("G", "msi"): (0.05, 30), ("G", "GPa"): (0.3, 200),
    ("density", "lb/in3"): (0.01, 0.9), ("density", "g/cm3"): (0.3, 25), ("density", "kg/m3"): (300, 25000),
    ("elongation", "%"): (0, 100), ("poisson", ""): (0.0, 0.5),
    ("KIc", "ksi√in"): (1, 250), ("KIc", "MPa√m"): (1, 275), ("Kt", ""): (1.0, 20.0), ("Kf", ""): (1.0, 20.0),
}
UNIT_KEY = {"ksi": "ksi", "MPa": "MPa", "N/mm2": "MPa", "msi": "msi", "Msi": "msi", "GPa": "GPa",
            "lb/in3": "lb/in3", "lb/in^3": "lb/in3", "lb/in³": "lb/in3", "g/cm3": "g/cm3", "g/cc": "g/cm3",
            "kg/m3": "kg/m3", "%": "%", "ksi√in": "ksi√in", "ksi-in1/2": "ksi√in", "MPa√m": "MPa√m",
            "": "", None: ""}


def ocr_confusions(text: str, limit: int = 50) -> list[str]:
    """Numeric-looking tokens containing a digit/letter confusion (A6)."""
    out = []
    for tok in re.split(r"[\s|;:()\[\]]+", text):
        t = tok.strip(",")
        if len(t) < 3 or not CONFUSION.match(t) or DESIGNATION.match(t):
            continue
        if any(c in CONFUSABLE for c in t) and any(c.isdigit() for c in t):
            out.append(t)
            if len(out) >= limit:
                break
    return out


def low_conf_numbers(words: list[tuple[str, float]], threshold: float, limit: int = 100) -> list[str]:
    """Numeric tokens whose Tesseract confidence is below `threshold` (A5). words = [(text, conf)]."""
    out = []
    for w, c in words:
        t = w.strip(",;:()")
        if 0 <= c < threshold and NUMERIC_TOKEN.match(t):
            out.append(t)
            if len(out) >= limit:
                break
    return out


def range_flag(quantity: str, value: float, unit: str | None) -> str | None:
    rng = RANGES.get((quantity, UNIT_KEY.get(unit, unit or "")))
    if rng is None:
        return None
    lo, hi = rng
    return None if lo <= value <= hi else f"RANGE: {value:g} {unit or ''} outside {lo:g}-{hi:g} for {quantity}"


def value_flags(quantity: str, value: float, value_text: str, unit: str | None, chunk_quality: dict | None) -> list[str]:
    """All flags for one extracted value."""
    from .units import check_dimension
    flags = []
    q = chunk_quality or {}
    raw = value_text.strip()
    if raw in set(q.get("low_conf_numbers") or []):
        flags.append("OCR_LOW_CONF")
    if raw in set(q.get("ocr_confusions") or []) or any(c in CONFUSABLE for c in raw):
        flags.append("OCR_CONFUSION")
    rf = range_flag(quantity, value, unit)
    if rf:
        flags.append(rf)
    if check_dimension(quantity, unit) is False:
        flags.append(f"UNIT_DIM: {unit} is not a valid unit for {quantity}")
    return flags
