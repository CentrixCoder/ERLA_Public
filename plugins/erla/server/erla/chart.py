"""Gated chart reading (Plan B, 0.4.0): read a value off a library figure, or refuse and say why.

A chart reading is weaker evidence than a printed number, so it is used only when nothing better exists and
only when every gate passes. Each gate returns REJECTED with its reason; nothing is ever guessed:

  G0  printed data first: a page that prints the curve's equation or a table is refused unless the reading is
      requested as a cross-check of that printed data (the reading never replaces it)
  G1  figure found and rendered at a resolution where one pixel is small against the axis span
  G2  each axis calibrates from >= 3 OCR'd tick labels that fit one scale (linear or log10) within tolerance;
      a misread or non-uniform label set fails
  G3  the scale type is unambiguous (the better of linear/log fits clearly better than the other)
  G4  the requested x lies inside the traced curve's own extent: no extrapolation
  G5  the curve is identified: by its colour; one stroke in the column (or a clean column within a few pixels on
      each side) - crossing or touching curves, markers or text at the point fail
  G6  the reading's uncertainty (stroke half-width + calibration residual) is small against the value
  G7  no reading between curves of a family: the tool reads one identified curve; several separate strokes of
      the colour through the whole neighbourhood (a family drawn in one colour) are refused, and a value
      between two family members is two readings, never an interpolation
  G8  the figure is square to the page: tick labels that drift off their row or column (a rotated scan) fail,
      since a skew of half a degree already moves a reading by more than its uncertainty
  G9  every axis tick label is read with OCR confidence >= MIN_OCR_CONF; a doubtful label is refused, not
      dropped, because the labels left behind may then fit a wrong scale
  G10 re-projection: the reported (x, y), plotted back through the calibrated axes, lands on the curve's
      stroke in the requested column (a reading taken from a neighbouring clean column must still lie on
      the curve at x)

On rejection the result lists other library documents carrying the same figure caption, so an equivalent but
cleaner chart (another edition, a reproduction) can be read instead. verify_packet re-reads a chart item and
passes it only within the stated uncertainty.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np

RENDER_DPI = 300
MIN_LABELS = 3
FIT_TOL = 0.004            # axis fit residual, as a share of the axis pixel span
SCALE_RATIO = 4.0          # the rejected scale's residual must be this many times the chosen one's
MAX_REL_UNCERTAINTY = 0.03
COLOR_TOL = 60             # RGB distance for "this pixel is the curve's colour"
CLEAN_SEARCH_PX = 6        # columns either side searched for a single clean stroke
NEIGHBOURHOOD_PX = 20      # columns either side checked for a second stroke of the same colour (identity)
MIN_OCR_CONF = 70          # Tesseract word confidence (0-100) an axis tick label must reach (G9)
MAX_SKEW_DEG = 0.3         # tick-label row/column drift allowed (G8); 0.5 deg measured moving a reading 3x its uncertainty
ON_CURVE_PX = 1.0          # G10: the re-plotted point may sit this far outside the curve's stroke
NUM = re.compile(r"^[-−]?\d{1,3}(?:,\d{3})+$|^[-−]?\d+(?:\.\d+)?$")
PRINTED = re.compile(r"\b(?:equation|log\s*n\s*f?\s*=|\w+\s*=\s*[-\d.]+\s*[-+]\s*[-\d.]+\s*log)", re.I)
CAPTION = re.compile(r"(Fig(?:ure)?\.?\s*[A-Z]?[\d][\w.()\-]*)\.?\s+([^\n]{8,140})")


class ChartRejected(ValueError):
    """A gate failed: the message names the gate and the reason."""


@dataclass
class Axis:
    scale: str              # 'linear' or 'log'
    a: float                # value = a + b * px   (log: log10(value) = a + b * px)
    b: float
    residual_px: float
    labels: int

    def value(self, px: float) -> float:
        v = self.a + self.b * px
        return 10 ** v if self.scale == "log" else v

    def px(self, value: float) -> float:
        v = math.log10(value) if self.scale == "log" else value
        return (v - self.a) / self.b


def _num(text: str) -> float | None:
    t = text.strip().replace("−", "-")
    if not NUM.match(t):
        return None
    return float(t.replace(",", ""))


def fit_axis(points: list[tuple[float, float]], span_px: float, name: str) -> Axis:
    """Calibrate one axis from (pixel, label value) pairs: gates G2 and G3."""
    pts = sorted({(round(p, 1), v) for p, v in points})
    if len(pts) < MIN_LABELS:
        raise ChartRejected(f"G2 {name} axis: {len(pts)} readable tick labels, need {MIN_LABELS}")
    px = np.array([p for p, _ in pts])
    val = np.array([v for _, v in pts])
    fits: dict[str, tuple[float, float, float]] = {}
    for scale in ("linear", "log"):
        if scale == "log" and np.any(val <= 0):
            continue
        y = np.log10(val) if scale == "log" else val
        b, a = np.polyfit(px, y, 1)
        if b == 0:
            continue
        resid_px = float(np.max(np.abs((y - (a + b * px)) / b)))
        fits[scale] = (float(a), float(b), resid_px)
    if not fits:
        raise ChartRejected(f"G2 {name} axis: tick labels do not form a scale")
    best = min(fits, key=lambda s: fits[s][2])
    a, b, r = fits[best]
    if r > FIT_TOL * span_px:
        raise ChartRejected(f"G2 {name} axis: tick labels do not fit one {best} scale (worst label off by "
                            f"{r:.1f} px; a misread label or a non-uniform axis)")
    other = [s for s in fits if s != best]
    if other and fits[other[0]][2] < SCALE_RATIO * max(r, 0.5):
        raise ChartRejected(f"G3 {name} axis: linear and log scales fit the labels about equally; scale ambiguous")
    return Axis(best, a, b, r, len(pts))


def _labels(img, ocr) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Numeric OCR words as (pixel, value) along the left edge (y axis) and the bottom edge (x axis): G8, G9."""
    d = ocr(img)
    h, w = img.shape[0], img.shape[1]
    words = []                      # (centre x, centre y, left, right, value, confidence)
    for i, t in enumerate(d["text"]):
        v = _num(t or "")
        conf = float(d["conf"][i])
        if v is None or conf < 0:   # Tesseract gives -1 for boxes that hold no word
            continue
        x, y, bw, bh = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        words.append((x + bw / 2, y + bh / 2, x, x + bw, v, conf))
    left = [wd for wd in words if wd[3] < 0.25 * w]
    bottom = [wd for wd in words if wd[1] > 0.6 * h]
    ylab = _aligned(left, key=lambda wd: wd[3], tol=0.02 * w)       # right edges of y labels line up
    xlab = _aligned(bottom, key=lambda wd: wd[1], tol=0.02 * h)      # centres of x labels line up
    for name, lab in (("y", ylab), ("x", xlab)):
        low = [wd for wd in lab if wd[5] < MIN_OCR_CONF]
        if low:
            raise ChartRejected(f"G9 {name} axis: tick label {low[0][4]:g} read with OCR confidence {low[0][5]:.0f} "
                                f"(< {MIN_OCR_CONF}): labels not trusted")
    skew = _skew_deg(ylab, xlab)
    if skew > MAX_SKEW_DEG:
        raise ChartRejected(f"G8 tick labels drift {skew:.2f} deg off their row or column (> {MAX_SKEW_DEG}): "
                            "the figure is rotated; a skewed scan is not read")
    return ([(wd[1], wd[4]) for wd in ylab], [(wd[0], wd[4]) for wd in xlab])


def _skew_deg(ylab, xlab) -> float:
    """Rotation of the figure from its tick labels: the x labels' centres must lie on one row, and the y labels
    on one column (by their left edges, right edges or centres, whichever the labels are aligned by)."""
    angles = []
    if len(xlab) >= MIN_LABELS:
        b = np.polyfit([wd[0] for wd in xlab], [wd[1] for wd in xlab], 1)[0]
        angles.append(abs(math.degrees(math.atan(b))))
    if len(ylab) >= MIN_LABELS:
        ys = [wd[1] for wd in ylab]
        angles.append(min(abs(math.degrees(math.atan(np.polyfit(ys, [f(wd) for wd in ylab], 1)[0])))
                          for f in (lambda wd: wd[2], lambda wd: wd[3], lambda wd: wd[0])))
    return max(angles, default=0.0)


def _aligned(words, key, tol):
    best: list = []
    for wd in words:
        grp = [o for o in words if abs(key(o) - key(wd)) <= tol]
        if len(grp) > len(best):
            best = grp
    return best


def trace(img, color: tuple[int, int, int], x_px: int) -> tuple[float, float, int, int]:
    """(y pixel, stroke half-height, curve's min x, max x) of the coloured curve at column x_px: G4, G5."""
    rgb = img[:, :, :3].astype(int)
    mask = np.sqrt(((rgb - np.array(color)) ** 2).sum(axis=2)) <= COLOR_TOL
    cols = np.where(mask.any(axis=0))[0]
    if cols.size == 0:
        raise ChartRejected("G5 no pixels of the curve colour in the figure: curve not identified")
    # the curve is the longest run of columns holding its colour; legend swatches and stray marks of the
    # same colour sit apart from it and must not stretch its extent (G4)
    runs = np.split(cols, np.where(np.diff(cols) > 25)[0] + 1)
    main = max(runs, key=len)
    lo, hi = int(main.min()), int(main.max())
    if not lo <= x_px <= hi:
        raise ChartRejected(f"G4 the requested x lies outside the plotted curve (pixels {lo}-{hi}): "
                            "no extrapolation")
    # identity: two strokes of this colour that persist across the neighbourhood (another curve, or the same
    # curve folding back) make it unknowable which one is meant, even where they merge at a crossing; short
    # extras such as data markers cover only a few columns and do not trip this
    window = [c for c in range(x_px - NEIGHBOURHOOD_PX, x_px + NEIGHBOURHOOD_PX + 1) if lo <= c <= hi]
    multi, fewest = 0, None
    for c in window:
        rows = np.where(mask[:, c])[0]
        strokes = [r for r in np.split(rows, np.where(np.diff(rows) > 1)[0] + 1) if len(r) >= 3] if rows.size else []
        if len(strokes) > 1:                 # 1-2 px specks (anti-aliased edges of other marks) are not strokes
            multi += 1
        fewest = len(strokes) if fewest is None else min(fewest, len(strokes))
    if window and multi == len(window):      # separate strokes all the way across: a family, not a crossing
        raise ChartRejected(f"G7 {fewest} separate strokes of this colour run through x (a family of curves drawn "
                            "in one colour, or curves and grid lines): which one is meant is unknowable here, and a "
                            "value between two curves is never interpolated; isolate one curve with region and read "
                            "each bracketing curve")
    if window and multi > 0.5 * len(window):
        raise ChartRejected("G5 two strokes of the curve colour run through this region (another curve of the "
                            "same colour, or a crossing): the curve is not uniquely identified")
    for off in [0] + [s * k for k in range(1, CLEAN_SEARCH_PX + 1) for s in (1, -1)]:
        c = x_px + off
        if not 0 <= c < mask.shape[1]:
            continue
        rows = np.where(mask[:, c])[0]
        if rows.size == 0:
            continue
        runs = np.split(rows, np.where(np.diff(rows) > 1)[0] + 1)
        if len(runs) == 1 and len(runs[0]) <= 12:
            run = runs[0]
            if off == 0:
                return float(run.mean()), max(1.0, len(run) / 2), lo, hi
            # a clean column next to a cluttered one: take the stroke centre there, if the slope is small
            return float(run.mean()), max(1.0, len(run) / 2) + abs(off) * 0.5, lo, hi
    raise ChartRejected("G5 the curve is not a single clean stroke near the requested x (crossing or touching "
                        "curves, markers or text): not read")


def reproject(img, color: tuple[int, int, int], x_axis: Axis, y_axis: Axis, x_value: float, y_value: float) -> None:
    """G10: plot the reported (x, y) back through the calibrated axes; it must land on the curve's stroke there."""
    col = int(round(x_axis.px(x_value)))
    row = y_axis.px(y_value)
    rgb = img[:, col, :3].astype(int)
    rows = np.where(np.sqrt(((rgb - np.array(color)) ** 2).sum(axis=1)) <= COLOR_TOL)[0]
    gap = float(np.min(np.abs(rows - row))) if rows.size else math.inf
    if gap > ON_CURVE_PX:
        where = "no stroke of the curve there" if rows.size == 0 else f"the curve is {gap:.1f} px away"
        raise ChartRejected(f"G10 the reading ({x_value:g}, {y_value:.4g}) plotted back onto the figure is off the "
                            f"curve ({where}): it came from a neighbouring column and does not hold at x")


def read_point(img, x_value: float, color: tuple[int, int, int], ocr, x_axis: Axis | None = None,
               y_axis: Axis | None = None) -> dict[str, Any]:
    """Read y at x on one coloured curve of a rendered figure (H x W x 3 array), every gate applied."""
    h, w = img.shape[0], img.shape[1]
    if min(h, w) < 400:
        raise ChartRejected(f"G1 figure is {w}x{h} px: too coarse to read reliably")
    if x_axis is None or y_axis is None:
        ylab, xlab = _labels(img, ocr)
        y_axis = y_axis or fit_axis(ylab, h, "y")
        x_axis = x_axis or fit_axis(xlab, w, "x")
    if x_axis.scale == "log" and x_value <= 0:
        raise ChartRejected("G4 x must be > 0 on a log axis")
    x_px = int(round(x_axis.px(x_value)))
    if not 0 <= x_px < w:
        raise ChartRejected("G4 the requested x lies outside the figure's axis: no extrapolation")
    y_px, half, lo, hi = trace(img, color, x_px)
    y = y_axis.value(y_px)
    reproject(img, color, x_axis, y_axis, x_value, y)
    dy = abs(y_axis.value(y_px + half + y_axis.residual_px) - y)
    if y == 0 or dy / abs(y) > MAX_REL_UNCERTAINTY:
        raise ChartRejected(f"G6 reading uncertainty {dy:.3g} is {dy / max(abs(y), 1e-12):.1%} of the value: "
                            "too large to use")
    return {"x": x_value, "y": y, "uncertainty": dy, "x_px": x_px, "y_px": y_px,
            "x_axis": x_axis.__dict__, "y_axis": y_axis.__dict__, "curve_extent_px": [lo, hi]}


def printed_data(page_text: str) -> str | None:
    """G0: the printed equation or table text a page carries next to its figure, if any."""
    for line in page_text.splitlines():
        if PRINTED.search(line):
            return line.strip()[:200]
    return None


def caption(page_text: str) -> str | None:
    m = CAPTION.search(page_text)
    return f"{m.group(1)} {m.group(2)}".strip() if m else None


def _page_number(locator: str) -> int:
    m = re.search(r"\(pdf (\d+)\)", locator) or re.match(r"\s*p\.\s*(\d+)\b", locator)
    if not m:
        raise ChartRejected(f"G1 locator {locator!r} names no PDF page")
    return int(m.group(1))


def _render(path: str, page_no: int, region: list[float] | None):
    """The figure on a PDF page as an RGB array: the given region (points), else the page's largest image."""
    import pymupdf
    doc = pymupdf.open(path)
    if not 1 <= page_no <= doc.page_count:
        raise ChartRejected(f"G1 page {page_no} is not in the document ({doc.page_count} pages)")
    page = doc[page_no - 1]
    text = str(page.get_text())
    if region:
        clip = pymupdf.Rect(*region)
    else:
        boxes = [r for img in page.get_images(full=True) for r in page.get_image_rects(img[0])]
        clip = max(boxes, key=lambda r: r.width * r.height) if boxes else page.rect
        if clip.width * clip.height < 0.05 * page.rect.width * page.rect.height:
            clip = page.rect
    pix = page.get_pixmap(dpi=RENDER_DPI, clip=clip)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    return arr.copy(), text


def tesseract_ocr(cfg):
    import pytesseract
    if cfg is not None and cfg.ocr.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = str(cfg.ocr.tesseract_cmd)

    def run(img):
        return pytesseract.image_to_data(img, config="--psm 11", output_type=pytesseract.Output.DICT)
    return run


def parse_color(color: str | list[int] | tuple[int, ...]) -> tuple[int, int, int]:
    if isinstance(color, str):
        c = color.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
            raise ChartRejected("G5 curve colour must be '#rrggbb'")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    if len(color) != 3:
        raise ChartRejected("G5 curve colour must be three 0-255 values")
    r, g, b = (int(v) for v in color)
    return r, g, b


def read_chart(con: Any, cfg: Any, doc_id: int, locator: str, x: float, color: Any, region: list[float] | None = None,
               cross_check: bool = False) -> dict[str, Any]:
    """Read y at x on a figure in the library, or return REJECTED with the gate, the reason and alternatives."""
    row = con.execute("SELECT path, ext, classification_flag FROM documents WHERE doc_id=?", (int(doc_id),)).fetchone()
    if row is None:
        return {"status": "REJECTED", "gate": "G1", "reason": f"no document {doc_id}"}
    from .classify import copy_group_flags, effective_flag
    # G2.5: a copy of a flagged document carries its copy group's flag (correctness audit 2026-10-31)
    flag = effective_flag(row[2], int(doc_id), copy_group_flags(con))
    out: dict[str, Any] = {"doc_id": int(doc_id), "locator": locator, "x": x, "flag": flag}
    text = ""
    try:
        if (row[1] or "").lower() != "pdf":
            raise ChartRejected("G1 chart reading needs a PDF page")
        arr, text = _render(row[0], _page_number(locator), region)
        printed = printed_data(text)
        if printed and not cross_check:
            raise ChartRejected(f"G0 the page prints its data ({printed!r}): use the printed equation or table; "
                                "read the chart only with cross_check=true to check that printed data")
        res = read_point(arr, float(x), parse_color(color), tesseract_ocr(cfg))
        out.update(status="READ", **res)
        if printed:
            out["printed"] = printed
            out["note"] = "cross-check only: the printed data governs; a disagreement flags a misprint or a misread"
        return out
    except ChartRejected as exc:
        msg = str(exc)
        out.update(status="REJECTED", gate=msg.split(" ", 1)[0], reason=msg)
        cap = caption(text)
        if cap:
            from .search import search
            hits, _, _ = search(con, cap, k=8, cfg=cfg, rerank=False, memory=False)
            out["caption"] = cap
            out["alternatives"] = [{"doc_id": h.doc_id, "title": h.title, "tier": h.tier, "locators": h.locators[:2],
                                    "flag": h.flag} for h in hits if h.doc_id != int(doc_id)][:5]
        return out
