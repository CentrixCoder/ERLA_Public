"""A4: engineering unit parsing, conversion and dimensional checks (pint).

Rules (rules_and_guidelines R4.6, G3.6): a converted value is always reported next to the original
value and unit, with the conversion factor used; units are never silently changed or dropped.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# Source spellings -> pint expressions. Keys are matched case-sensitively first, then lower-case.
ALIASES = {
    "ksi": "ksi", "psi": "psi", "msi": "msi", "Msi": "msi", "MSI": "msi", "KSI": "ksi", "PSI": "psi",
    "MPa": "MPa", "GPa": "GPa", "kPa": "kPa", "Pa": "Pa", "N/mm2": "N/mm**2", "N/mm^2": "N/mm**2",
    "N/mm²": "N/mm**2", "kgf/mm2": "kgf/mm**2", "kgf/mm^2": "kgf/mm**2",
    "lb": "lbf", "lbs": "lbf", "lbf": "lbf", "kip": "kip", "kips": "kip", "N": "N", "kN": "kN",
    "in": "inch", "in.": "inch", "inch": "inch", "inches": "inch", "mm": "mm", "cm": "cm", "m": "m", "ft": "ft",
    "in-lb": "inch*lbf", "in-lbs": "inch*lbf", "in.-lb": "inch*lbf", "lb-in": "lbf*inch", "ft-lb": "ft*lbf",
    "N-m": "N*m", "N·m": "N*m", "Nm": "N*m", "N-mm": "N*mm",
    "°F": "degF", "ºF": "degF", "degF": "degF", "F": "degF", "°C": "degC", "ºC": "degC", "degC": "degC",
    "lb/in3": "lb/inch**3", "lb/in^3": "lb/inch**3", "lb/in.3": "lb/inch**3", "lb/in³": "lb/inch**3",
    "g/cm3": "g/cm**3", "g/cm^3": "g/cm**3", "g/cc": "g/cm**3", "kg/m3": "kg/m**3", "kg/m^3": "kg/m**3",
    "ksi√in": "ksi*inch**0.5", "ksi-in1/2": "ksi*inch**0.5", "ksi√in.": "ksi*inch**0.5",
    "ksi-in^0.5": "ksi*inch**0.5", "ksi in^1/2": "ksi*inch**0.5", "ksi*in^0.5": "ksi*inch**0.5",
    "MPa√m": "MPa*m**0.5", "MPa-m1/2": "MPa*m**0.5", "MPa m^1/2": "MPa*m**0.5", "MPa*m^0.5": "MPa*m**0.5",
    "%": "percent", "percent": "percent", "Hz": "Hz",
    "in/in/°F": "1/degF", "in/in/F": "1/delta_degF", "10^-6 in/in/°F": "cte_e6_F",
}
# Unit tokens recognised inside table headers / labels: "Ftu, ksi", "(MPa)", "E [msi]".
# Single letters (N, F, m) are excluded here: in a label "cycles, N" N is not newtons.
_LABEL_UNITS = [k for k in ALIASES if len(k) > 1 and k not in ("in", "lb", "ft")] + ["in", "lb"]
UNIT_IN_LABEL = re.compile(
    r"(?:[,(\[]\s*|\s)(" + "|".join(sorted((re.escape(k) for k in _LABEL_UNITS), key=len, reverse=True))
    + r")\s*[)\]]?\s*$")

# Expected dimensionality per property quantity (used for dimensional checks, A4)
QUANTITY_DIM = {
    "Ftu": "[pressure]", "Fty": "[pressure]", "Fcy": "[pressure]", "Fsu": "[pressure]",
    "Fbru": "[pressure]", "Fbry": "[pressure]", "E": "[pressure]", "Ec": "[pressure]", "G": "[pressure]",
    "KIc": "[pressure]*[length]**0.5", "density": "[mass]/[length]**3", "elongation": "",
    "poisson": "", "Kt": "", "Kf": "",
}
SI_TARGET = {"[pressure]": "MPa", "[pressure]*[length]**0.5": "MPa*m**0.5", "[mass]/[length]**3": "kg/m**3",
             "[length]": "mm", "[force]": "N", "[temperature]": "degC"}
US_TARGET = {"[pressure]": "ksi", "[pressure]*[length]**0.5": "ksi*inch**0.5", "[mass]/[length]**3": "lb/inch**3",
             "[length]": "inch", "[force]": "lbf", "[temperature]": "degF"}


@lru_cache(maxsize=1)
def ureg() -> Any:                      # pint's generics differ by version (0.25 vs 0.26)
    import pint
    registry: Any = getattr(pint, "UnitRegistry")
    u: Any = registry(autoconvert_offset_to_baseunit=True)
    if "msi" not in u:
        u.define("msi = 1e6 * psi")
    if "cte_e6_F" not in u:
        # pint refuses a scale factor inside a unit expression ("1e-6/delta_degF"), so a thermal-expansion
        # coefficient printed in 10^-6 in/in/°F needs a named unit (correctness audit 2026-10-31)
        u.define("cte_e6_F = 1e-6 / delta_degF")
    return u


def normalize(unit: str | None) -> str | None:
    """Source unit spelling -> pint expression, or None if not recognised."""
    if not unit:
        return None
    u = unit.strip().strip("()[]").strip()
    if u in ALIASES:
        return ALIASES[u]
    if u.lower() in {k.lower(): v for k, v in ALIASES.items()}:
        return {k.lower(): v for k, v in ALIASES.items()}[u.lower()]
    try:
        ureg().parse_units(u)
        return u
    except Exception:
        return None


def unit_from_label(label: str | None) -> tuple[str | None, str]:
    """'Ftu, ksi' -> ('ksi', 'Ftu'); '(MPa)' -> ('MPa', ''). Returns (source unit, label without unit)."""
    if not label:
        return None, ""
    m = UNIT_IN_LABEL.search(label.strip())
    if not m:
        return None, label.strip()
    return m.group(1), label[:m.start()].strip(" ,([")


def dimensionality(unit: str) -> str:
    n = normalize(unit)
    if n is None:
        return "?"
    d = ureg().Quantity(1, n).dimensionality
    return str(d) if d else ""


def check_dimension(quantity: str, unit: str | None) -> bool | None:
    """True/False if `unit` matches the expected dimension of `quantity`; None if unknown."""
    want = QUANTITY_DIM.get(quantity)
    if want is None or unit is None:
        return None
    n = normalize(unit)
    if n is None:
        return None
    if want == "":
        return ureg().Quantity(1, n).dimensionless
    return ureg().Quantity(1, n).check(want)


@dataclass
class Conversion:
    value: float
    unit: str
    original_value: float
    original_unit: str
    factor: float | None      # None for offset units (temperature)

    def text(self) -> str:
        f = f"factor {self.factor:.6g}" if self.factor is not None else "offset conversion"
        return f"{self.original_value:g} {self.original_unit} = {self.value:.6g} {self.unit} ({f})"


def convert(value: float, unit: str, to: str) -> Conversion:
    """Convert with the factor recorded (R4.6). Raises ValueError for unknown/incompatible units."""
    src, dst = normalize(unit), normalize(to)
    if src is None or dst is None:
        raise ValueError(f"unknown unit: {unit if src is None else to}")
    U: Any = ureg()                     # pint is only partially typed
    factor: float | None
    try:
        out = float(U.Quantity(value, src).to(dst).magnitude)
        one: Any = U.Quantity(1.0, src)
        factor = None if not one.unit_items() or any("deg" in k for k, _ in one.unit_items()) \
            else float(one.to(dst).magnitude)
    except Exception as exc:
        raise ValueError(f"cannot convert {unit} to {to}: {exc}") from exc
    if factor is not None and not math.isclose(out, value * factor, rel_tol=1e-9):
        factor = None
    return Conversion(out, to, value, unit, factor)


def _target(unit: str, table: dict[str, str]) -> str | None:
    n = normalize(unit)
    if n is None:
        return None
    q = ureg().Quantity(1, n)
    for dim, tgt in table.items():
        try:
            if q.check(dim):
                return tgt
        except Exception:
            continue
    return None


def to_project(value: float, unit: str, system: str = "US") -> Conversion | None:
    """Convert to the project unit system's standard unit for that dimension; None if same/unknown."""
    target = _target(unit, US_TARGET if system.upper() == "US" else SI_TARGET)
    if not target or normalize(target) == normalize(unit):
        return None
    try:
        return convert(value, unit, target)
    except ValueError:
        return None


def to_si(value: float, unit: str) -> tuple[float, str] | None:
    """(value, SI unit) for storage/comparison; None if the unit is unknown or has no SI target."""
    target = _target(unit, SI_TARGET)
    if not target:
        return None
    try:
        return convert(value, unit, target).value, target
    except ValueError:
        return None
