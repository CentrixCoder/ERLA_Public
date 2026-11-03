"""PDF extractor: PyMuPDF text per page, table extraction (A2), Tesseract OCR fallback for scanned
pages with page triage (S2) and per-word confidence flags (A5/A6)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Chunk, Extractor, ExtractResult, clean, rows_to_markdown, split_long

NUM_ROW = re.compile(r"(?:[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s+){4,}")
NUM_CELL = re.compile(r"^[\s(]*[-+±]?\d[\d.,]*\s*[a-z%]{0,4}[)\s]*$", re.I)
CAPTION = re.compile(r"^\s*(table|tab\.)\s*[\w.\-]+", re.I)


def _year(meta: dict) -> int | None:
    for key in ("creationDate", "modDate"):
        m = re.search(r"(19|20)\d{2}", meta.get(key) or "")
        if m:
            return int(m.group(0))
    return None


def _caption(page, bbox) -> str:
    """Up to two text lines directly above a table; a 'Table x-y ...' line is preferred."""
    import fitz
    clip = fitz.Rect(page.rect.x0, max(page.rect.y0, bbox.y0 - 90), page.rect.x1, bbox.y0)
    lines = [ln.strip() for ln in (page.get_text("text", clip=clip) or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    for i in range(len(lines) - 1, -1, -1):
        if CAPTION.match(lines[i]):
            return " ".join(lines[i:i + 2])[:300]
    return " ".join(lines[-2:])[:300]


def _plausible_table(rows: list[list[str]]) -> bool:
    """Reject chart grids and layout artefacts that find_tables() reports as tables:
    too many columns, mostly empty, single-character cells, or no row with two numeric cells."""
    if len(rows) < 2:
        return False
    ncol = max(len(r) for r in rows)
    if ncol < 2 or ncol > 25:
        return False
    cells = [c for r in rows for c in r]
    filled = [c for c in cells if c]
    if not filled or len(filled) / len(cells) < 0.3:
        return False
    if sum(len(c) for c in filled) / len(filled) < 2.0 or sum(len(c) == 1 for c in filled) / len(filled) > 0.4:
        return False
    return any(sum(1 for c in r if c and NUM_CELL.match(c)) >= 2 for r in rows)


LEADER = re.compile(r"(?:\s*\.){3,}")          # dot leaders ". . . ."


def _col_edges(t) -> list[float]:
    """Left edges of the table's columns from its cell boxes (merged cells collapse to one edge)."""
    xs = sorted({round(c[0], 1) for c in (getattr(t, "cells", None) or []) if c})
    edges: list[float] = []
    for x in xs:
        if not edges or x - edges[-1] > 3.0:
            edges.append(x)
    return edges


def word_grid(page, t, y_tol: float = 3.2) -> list[list[str]]:
    """Rebuild a table as one row per printed line using word positions.

    find_tables() merges a block of printed rows into one cell (MMPDS/MIL-HDBK-5 layout:
    'Ftu, ksi / Fty, ksi / ...' in one cell, '220 / 185 / ...' in the next). Assigning each word to a
    column by x and to a line by y restores row alignment. Subscripts printed slightly lower
    (F tu) are merged into their line by `y_tol`. Dot leaders are removed."""
    edges = _col_edges(t)
    if len(edges) < 2:
        return []
    words = page.get_text("words", clip=t.bbox) or []
    if not words:
        return []
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in words:
        yc = (w[1] + w[3]) / 2
        if lines and abs(yc - lines[-1][0]) <= y_tol:
            lines[-1][1].append(w)
        else:
            lines.append([yc, [w]])
    grid = []
    for _yc, ws in lines:
        row = [""] * len(edges)
        for w in sorted(ws, key=lambda w: w[0]):
            col = max(i for i, e in enumerate(edges) if w[0] >= e - 2.0) if w[0] >= edges[0] - 2.0 else 0
            row[col] = f"{row[col]} {w[4]}".strip()
        row = [LEADER.sub("", c).strip(" .") for c in row]
        if any(row):
            grid.append(row)
    return grid


NUM_TOKEN = re.compile(r"^[-+]?\d+(?:\.\d+)?[a-z]{0,2}$")
PROPERTY_PAGE = re.compile(r"\bF\s*t\s*[uy]\b[^\n]*\b(?:ksi|MPa)\b", re.I)
SPAN_SEP = "\u2063"     # invisible separator: marks a phrase that spans several data columns


REL_PREFIX = {"#", "≤", "<", "≥", ">", "<=", ">=", "\uf0a3", "\uf0b3"}   # '#' = '≤' in MMPDS Symbol font
VALUE_LIKE = re.compile(r"^[-+]?\d*\.?\d+[a-z]{0,2}$|^\d*\.?\d+\s*-\s*\d*\.?\d*$|^[ABS]$")  # 55, 0.012-0.039, A
CODE_TOKEN = re.compile(r"^[A-Z]{1,2}\d{1,5}[A-Z\d]*$")      # a temper or condition code: T3, T351, H1150


def _turn(main: tuple[int, int], w: float, h: float):
    """Box in the unturned page's coordinates -> box in reading orientation, for text running ``main``."""
    if main == (0, -1):                                          # bottom-to-top (a page turned with /Rotate 90)
        return lambda b: (h - b[3], b[0], h - b[1], b[2])
    if main == (0, 1):                                           # top-to-bottom
        return lambda b: (b[1], w - b[2], b[3], w - b[0])
    return lambda b: (w - b[2], h - b[3], w - b[0], h - b[1])    # upside down


def _once(words: list[tuple]) -> list[tuple]:
    """Each word once: a word printed again over itself (a tagged heading drawn twice, ASM Handbook tables:
    'Kc' over 'Kc (ksi√in.)', 'Gauge' over 'Gauge (in.)') would read 'Kc Kc'. Same text and boxes overlapping
    by half the smaller one is a copy."""
    kept: dict[str, list[tuple]] = {}
    out = []
    for wd in words:
        area = max((wd[2] - wd[0]) * (wd[3] - wd[1]), 1e-6)
        dup = False
        for k in kept.get(wd[4], []):
            ix = min(wd[2], k[2]) - max(wd[0], k[0])
            iy = min(wd[3], k[3]) - max(wd[1], k[1])
            if ix > 0 and iy > 0 and ix * iy >= 0.5 * min(area, (k[2] - k[0]) * (k[3] - k[1])):
                dup = True
                break
        if not dup:
            kept.setdefault(wd[4], []).append(wd)
            out.append(wd)
    return out


SIZE_BREAK = re.compile(r"[\d\-–]")     # a digit or dash: where a stacked fraction meets full-size text


def reading_words(page, clip=None, split_sizes: bool = False, once: bool = True) -> list[tuple]:
    """The page's words as (x0, y0, x1, y1, text) in reading orientation, each printed word once.

    A landscape table is often a portrait page turned with /Rotate 90 and drawn bottom-to-top (MMPDS-11
    p.1007). Its words come in the unturned page's coordinates, where each printed column is one "line", so
    the table read as a single caption line and was lost. When most text lines run in another direction than
    left to right, the words of those lines are built from their characters and turned to reading
    orientation; lines in other directions (the page header, printed upright on the unturned page) are left
    out. Upright pages keep get_text("words") unless ``split_sizes``: then words are built from characters
    too and split where a digit or dash meets one of a clearly different size (Shigley's stacked '1/4'
    printed against '–1' reads '4–1' as one word)."""
    from collections import Counter

    def text(kind: str) -> Any:
        return page.get_text(kind, clip=clip) if clip is not None else page.get_text(kind)

    def lines_of(d: dict) -> list[dict]:
        return [ln for b in d.get("blocks", []) if b.get("type") == 0 for ln in b.get("lines", [])]

    dirs = Counter((round(ln["dir"][0]), round(ln["dir"][1])) for ln in lines_of(text("dict")))
    main = dirs.most_common(1)[0][0] if dirs else (1, 0)
    upright = main in ((1, 0), (0, 0))
    if upright and not split_sizes:
        words = [tuple(w[:5]) for w in text("words")]
        return _once(words) if once else words
    lines = lines_of(text("rawdict"))
    box = page.cropbox
    turn = (lambda b: b) if upright else _turn(main, box.width, box.height)
    out: list[tuple] = []
    for ln in lines:
        if not upright and (round(ln["dir"][0]), round(ln["dir"][1])) != main:
            continue
        cur: list[dict] = []                                     # a word may run over spans ('(b' + '4)')
        chars = [{**c, "size": span.get("size", 0.0)} for span in ln.get("spans", []) for c in span.get("chars", [])]
        for ch in [*chars, {"c": " ", "size": 0.0}]:
            prev = cur[-1] if cur else None
            if ch["c"].strip() and not (split_sizes and prev and SIZE_BREAK.match(prev["c"]) and SIZE_BREAK.match(ch["c"])
                                        and abs(ch["size"] - prev["size"]) > 0.2 * max(ch["size"], prev["size"])):
                cur.append(ch)
                continue
            if cur:
                b = (min(c["bbox"][0] for c in cur), min(c["bbox"][1] for c in cur),
                     max(c["bbox"][2] for c in cur), max(c["bbox"][3] for c in cur))
                out.append((*turn(b), "".join(c["c"] for c in cur)))
            cur = [ch] if ch["c"].strip() else []
    return _once(out) if once else out


def _lines(words: list[tuple], y_tol: float) -> list[list]:
    """Words grouped into printed lines by their vertical centre: [[y centre, words left to right], ...]."""
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in words:
        yc = (w[1] + w[3]) / 2
        if lines and abs(yc - lines[-1][0]) <= y_tol:
            lines[-1][1].append(w)
        else:
            lines.append([yc, [w]])
    for ln in lines:
        ln[1].sort(key=lambda w: w[0])
    return lines


def _centred_groups(mids: list[float], centers: list[float], half: float) -> list[tuple[int, int]] | None:
    """Column groups of headings each printed centred over its group, left to right from the first data
    column: a group starting at column a with its heading centred at m ends at the column nearest the mirror
    image of a about m. Only a chain that ends exactly on the last column is taken (otherwise None): headings
    printed left-aligned, or over only some columns, do not close it, and those lines keep the old placement."""
    out: list[tuple[int, int]] = []
    start = 0
    for m in mids:
        if start >= len(centers):
            return None
        mirror = 2 * m - centers[start]
        end = min(range(len(centers)), key=lambda k: abs(centers[k] - mirror))
        if end < start or abs(centers[end] - mirror) > half or not centers[start] - half <= m <= centers[end] + half:
            return None
        out.append((start, end))
        start = end + 1
    return out if start == len(centers) else None


def aligned_grid(page, clip=None, y_tol: float = 3.2, gap: float = 12.0,
                 code_columns: bool = True, once: bool = True) -> tuple[list[list[str]], str]:
    """A2 for borderless tables (MMPDS-10+ allowables pages): rebuild rows and columns from geometry.

    Returns (grid, caption). The caption line ('Table 3.7.9.0(b1). Design ... of 7075 Aluminum Alloy')
    and centred continuation lines directly below it are returned as the caption, not as rows; the
    page header above the caption is ignored. Data columns = x-centres of the numbers on the line with
    the most numbers (the widest data row). Text left of the first data column is the row label. Other
    phrases go to the nearest data column; a phrase covering several column centres (a spanning
    header such as 'Sheet', or a thickness range centred over an A/B pair) is placed in each column it
    covers, marked with SPAN_SEP; so is a heading above the data centred between two columns. A temper code
    standing between the numbers of that line ('2024  T3  0.063  145') is a column of its own. Words are read in
    reading orientation (reading_words)."""
    words = reading_words(page, clip, once=once)
    if not words:
        return [], ""
    lines = _lines(words, y_tol)
    text_of = lambda ws: " ".join(w[4] for w in ws)                       # noqa: E731
    caption, start = "", 0
    ci = next((i for i, (_y, ws) in enumerate(lines) if CAPTION.match(text_of(ws))), None)
    if ci is not None:
        caption, start = text_of(lines[ci][1]), ci + 1
        cx = (lines[ci][1][0][0] + lines[ci][1][-1][2]) / 2
        h = max(w[3] - w[1] for w in lines[ci][1])
        while start < len(lines):
            ws = lines[start][1]
            xc = (ws[0][0] + ws[-1][2]) / 2
            if lines[start][0] - lines[start - 1][0] <= 1.8 * h and abs(xc - cx) < 25 and \
                    not any(NUM_TOKEN.match(w[4]) for w in ws):
                caption += " " + text_of(ws)
                start += 1
            else:
                break
    body = lines[start:]
    tmpl = max(body, key=lambda ln: sum(1 for w in ln[1] if NUM_TOKEN.match(w[4])), default=None)
    if tmpl is None:
        return [], caption
    nums = [w for w in tmpl[1] if NUM_TOKEN.match(w[4])]
    centers = [(w[0] + w[2]) / 2 for w in nums]
    if len(centers) < 2:
        return [], caption
    # ASM Handbook: '2024  T3  0.063  145 ...' - without its own column the temper joined the gauge cell
    codes = [(w[0] + w[2]) / 2 for w in tmpl[1] if code_columns and CODE_TOKEN.match(w[4])
             and nums[0][2] < w[0] and w[2] < nums[-1][0]]
    centers = sorted(centers + [c for c in codes if min(abs(c - x) for x in centers) >= 15.0])
    half = min(b - a for a, b in zip(centers, centers[1:])) / 2
    label_edge = centers[0] - max(half, 8.0)
    tol = max(half * 0.5, 3.0)
    first_data = next((y for y, ws in body if sum(1 for w in ws if NUM_TOKEN.match(w[4])) >= 2), tmpl[0])
    # a table whose data rows carry row labels (MMPDS 'Ftu, ksi:', 'L') reads its headings through those labels
    labelled = any(w[2] <= label_edge + 2.0 for y, ws in body if y >= first_data for w in ws)
    grid = []
    for yc, ws in body:
        row = [""] * (len(centers) + 1)
        row[0] = " ".join(w[4] for w in ws if w[2] <= label_edge + 2.0)
        rest = [w for w in ws if w[2] > label_edge + 2.0]
        phrases: list[list] = []
        for w in rest:
            prev = phrases[-1][-1][4] if phrases else ""
            if phrases and w[0] - phrases[-1][-1][2] <= gap and (
                    prev in REL_PREFIX or (not VALUE_LIKE.match(w[4]) and not VALUE_LIKE.match(prev))):
                phrases[-1].append(w)          # '# 0.1874' / '≤ 0.249' stay one phrase
            else:
                phrases.append([w])
        # a lone text phrase on a labelled line (MMPDS 'Plate', 'Annealed', 'AMS 4911' centred over the
        # table) applies to every data column: mark it as a span instead of a value in one column
        if len(phrases) == 1 and row[0]:
            text = " ".join(w[4] for w in phrases[0])
            if not VALUE_LIKE.match(text) and not NUM_TOKEN.match(text) and not text.startswith(tuple(REL_PREFIX)):
                grid.append([LEADER.sub("", row[0]).strip(" .")] + [f"{SPAN_SEP}{text}"] * len(centers))
                continue
        # several text phrases on a labelled line, each centred over its own group of columns (MMPDS 'Sheet'
        # over two columns, 'Plate' over sixteen): each reaches only its middle column by position
        texts = [" ".join(w[4] for w in ph) for ph in phrases]
        groups = _centred_groups([(ph[0][0] + ph[-1][2]) / 2 for ph in phrases], centers, half) if (
            row[0] and len(phrases) >= 2 and not any(VALUE_LIKE.match(t) or NUM_TOKEN.match(t)
                                                      or t.startswith(tuple(REL_PREFIX)) for t in texts)) else None
        if groups:
            for t, (a, b) in zip(texts, groups):
                for i in range(a, b + 1):
                    row[i + 1] = f"{SPAN_SEP}{t}" if b > a else t
            grid.append([LEADER.sub("", c).strip(" .") for c in row])
            continue
        near = [min(range(len(centers)), key=lambda k: abs(centers[k] - (ph[0][0] + ph[-1][2]) / 2)) for ph in phrases]
        for n_ph, ph in enumerate(phrases):
            x0, x1 = ph[0][0], ph[-1][2]
            text = " ".join(w[4] for w in ph)
            covered = [i for i, c in enumerate(centers) if x0 - tol <= c <= x1 + tol]
            taken = {k for m, k in enumerate(near) if m != n_ph}      # columns other phrases of this line head
            xc = (x0 + x1) / 2
            # a heading centred between two columns, above the data of a table without row labels (ASM 'Fty
            # (ksi)' over L and LT, A07 2026-11-02): it heads both, not the nearer one. A heading of one column
            # sits on that column's centre, so only a centre near the midpoint of a pair counts, and only over
            # two columns no other phrase of the line heads (a collapsed multi-column heading, doc 786 p.89).
            pair = next((i for i in range(len(centers) - 1) if abs(xc - (centers[i] + centers[i + 1]) / 2)
                         <= 0.15 * (centers[i + 1] - centers[i]) and not {i, i + 1} & taken), None) if (
                not labelled and not row[0] and yc < first_data and len(covered) < 2 and re.search(r"[A-Za-z]", text)
                and not VALUE_LIKE.match(text) and not text.startswith(tuple(REL_PREFIX))) else None
            if pair is not None:
                covered = [pair, pair + 1]
            if len(covered) >= 2 and not NUM_TOKEN.match(text):
                for i in covered:
                    row[i + 1] = f"{row[i + 1]} {SPAN_SEP}{text}".strip()
            else:
                i = min(range(len(centers)), key=lambda k: abs(centers[k] - xc))
                row[i + 1] = f"{row[i + 1]} {text}".strip()
        row = [LEADER.sub("", c).strip(" .") for c in row]
        if any(row):
            grid.append(row)
    return grid, caption


def _merged(rows: list[list[str]]) -> bool:
    return any((c or "").count("\n") >= 2 for r in rows for c in r)


# ------------------------------------------------------------------ borderless tables on other pages
NUMERIC_PHRASE = re.compile(r"^[(±+\-−]?\d[\d.,/ ]*[a-z]{0,2}[*)%]?$")     # 85, 0.031 8, 3/4, 1 1/4, 67b
LEADER_WORD = re.compile(r"\.{4,}")                                        # a contents line, not a table row
FRACTION_PART = re.compile(r"^\d{1,2}$")
GROUP_HEAD = re.compile(r"^\d*\.\d{3}(?: \d{3})*$")                       # '0.001' before its digit group '80'
GROUP_TAIL = re.compile(r"^\d{1,3}$")


def _stack_fractions(words: list[tuple]) -> list[tuple]:
    """A stacked fraction - a small numerator printed over its small denominator (Shigley's thread sizes) -
    as one word 'n/d' between them, on the line of its row. Otherwise '3' and '4' are two rows and the size
    '3/4' is lost."""
    if len(words) < 2:
        return words
    heights = sorted(w[3] - w[1] for w in words)
    small_h = 0.8 * heights[len(heights) // 2]
    small = sorted((i for i, w in enumerate(words) if FRACTION_PART.match(w[4]) and w[3] - w[1] < small_h),
                   key=lambda i: (words[i][1], words[i][0]))
    taken: set[int] = set()
    joined: list[tuple] = []
    for i in small:
        if i in taken:
            continue
        a = words[i]
        h = a[3] - a[1]
        for j in small:
            b = words[j]
            if j == i or j in taken:
                continue
            overlap = min(a[2], b[2]) - max(a[0], b[0])
            if b[1] > a[1] + 0.4 * h and b[1] - a[3] < 0.5 * h and overlap >= 0.5 * min(a[2] - a[0], b[2] - b[0]):
                taken |= {i, j}
                joined.append((min(a[0], b[0]), a[1], max(a[2], b[2]), b[3], f"{a[4]}/{b[4]}"))
                break
    return [w for k, w in enumerate(words) if k not in taken] + joined


def _digit_groups(ws: list[tuple]) -> list[tuple]:
    """'0.001 80' printed with a thin space is one number (0.00180), not two cells."""
    out: list[tuple] = []
    for w in ws:
        p = out[-1] if out else None
        if p and GROUP_HEAD.match(p[4]) and GROUP_TAIL.match(w[4]) and 0 <= w[0] - p[2] < 0.45 * (p[3] - p[1]):
            out[-1] = (p[0], min(p[1], w[1]), w[2], max(p[3], w[3]), f"{p[4]} {w[4]}")
        else:
            out.append(w)
    return out


def _gap_phrases(ws: list[tuple], gap: float) -> list[list[tuple]]:
    phrases: list[list[tuple]] = []
    for w in ws:
        if phrases and w[0] - phrases[-1][-1][2] <= gap:
            phrases[-1].append(w)
        else:
            phrases.append([w])
    return phrases


def _join_heading(parts: list[str]) -> str:
    """Heading lines of one column read top to bottom: 'Tensile-' + 'Stress' -> 'Tensile-Stress'."""
    out = ""
    for p in parts:
        out = f"{out}{p}" if out.endswith("-") and p[:1].isalpha() else f"{out} {p}".strip()
    return out


def text_tables(page, loc: str, max_tables: int = 8, y_tol: float = 3.2) -> list[Chunk]:
    """A2 for borderless tables on pages that are not MMPDS allowables pages (Shigley Tables 8-2 and 8-9):
    find_tables() needs ruling lines and aligned_grid() takes the whole page as one table, so these were lost.

    A data line has at least three phrases (words closer than about one character height) of which at least
    two are numbers. A table body is three or more data lines in a run, broken by prose, a caption, a
    contents line or a wide vertical gap; up to two other lines inside it are kept as rows. The heading lines
    are the multi-phrase lines directly above it, read per column and joined into one heading row, which is
    how verify_packet reads a column's quantity. Columns are the x-extents the body's phrases occupy. The
    caption is the 'Table x' line above the headings (with the title lines below it) or below the body."""
    words = reading_words(page, split_sizes=True)
    if len(words) < 9:
        return []
    words = _stack_fractions(words)
    heights = sorted(w[3] - w[1] for w in words)
    h = heights[len(heights) // 2]
    gap = max(6.0, 1.1 * h)
    width = max(w[2] for w in words) - min(w[0] for w in words)
    lines = [(yc, _gap_phrases(_digit_groups(ws), gap)) for yc, ws in _lines(words, y_tol)]
    text_of = lambda ph: " ".join(w[4] for w in ph)                       # noqa: E731

    def is_data(ph: list[list[tuple]]) -> bool:
        return (len(ph) >= 3 and sum(1 for p in ph if NUMERIC_PHRASE.match(text_of(p))) >= 2
                and not any(LEADER_WORD.search(w[4]) for p in ph for w in p))

    def is_prose(ph: list[list[tuple]]) -> bool:
        return len(ph) == 1 and (len(ph[0]) >= 5 or ph[0][-1][2] - ph[0][0][0] > 0.5 * width)

    data = [is_data(ph) for _, ph in lines]
    out: list[Chunk] = []
    i = 0
    while i < len(lines) and len(out) < max_tables:
        if not data[i]:
            i += 1
            continue
        last, n_data, j = i, 1, i
        while j + 1 < len(lines):
            nxt = j + 1
            ph = lines[nxt][1]
            if is_prose(ph) or CAPTION.match(text_of(ph[0])) or lines[nxt][0] - lines[j][0] > 5 * h:
                break
            if data[nxt]:
                last, n_data = nxt, n_data + 1
            elif nxt - last > 2:
                break
            j = nxt
        start, end = i, last
        i = last + 1
        if n_data < 3:
            continue
        spans = sorted((p[0][0], p[-1][2]) for k in range(start, end + 1) if data[k] for p in lines[k][1])
        cols: list[list[float]] = []
        for x0, x1 in spans:
            if cols and x0 <= cols[-1][1] + 1.0:
                cols[-1][1] = max(cols[-1][1], x1)
            else:
                cols.append([x0, x1])
        if len(cols) < 2:
            continue
        lo, hi = cols[0][0], cols[-1][1]
        head = start
        while head - 1 >= 0 and start - head < 8:
            ph = lines[head - 1][1]
            if (is_prose(ph) or CAPTION.match(text_of(ph[0])) or lines[head][0] - lines[head - 1][0] > 3 * h
                    or (len(ph) == 1 and (len(ph[0]) > 3 or ph[0][0][0] < lo - 30 or ph[0][-1][2] > hi + 30))):
                break
            head -= 1

        def place(p: list[tuple], heading: bool) -> list[int]:
            x0, x1 = p[0][0], p[-1][2]
            over = [(min(x1, c[1]) - max(x0, c[0]), k) for k, c in enumerate(cols)]
            hits = [k for ov, k in over if ov > 0]
            if heading and len(hits) >= 2:
                return hits                                       # a group heading over several columns
            if hits:
                return [max(over)[1]]
            if heading and (x1 < lo - 10 or x0 > hi + 10):
                return []                                         # a heading over no data ('Head Marking')
            xc = (x0 + x1) / 2
            return [min(range(len(cols)), key=lambda k: abs((cols[k][0] + cols[k][1]) / 2 - xc))]

        heading: list[list[str]] = [[] for _ in cols]
        for k in range(head, start):
            for p in lines[k][1]:
                for c in place(p, True):
                    heading[c].append(text_of(p))
        grid = [[_join_heading(parts) for parts in heading]] if head < start else []
        for k in range(start, end + 1):
            row = [""] * len(cols)
            for p in lines[k][1]:
                for c in place(p, False):
                    row[c] = f"{row[c]} {text_of(p)}".strip()
            grid.append(row)
        if not _plausible_table(grid):
            continue
        cap = ""
        for k in range(head - 1, max(head - 4, -1), -1):
            if CAPTION.match(text_of(lines[k][1][0])):
                cap = " ".join(text_of(p) for m in range(k, head) for p in lines[m][1])
                break
        else:
            for k in range(end + 1, min(end + 4, len(lines))):
                if CAPTION.match(text_of(lines[k][1][0])):
                    cap = " ".join(text_of(p) for m in range(k, min(k + 2, len(lines))) for p in lines[m][1])
                    break
        cap = cap[:300]
        out.append(Chunk(f"{loc} table {len(out) + 1}", (f"{cap}\n\n" if cap else "") + rows_to_markdown(grid),
                         has_table=True, quality={"table": {"rows": len(grid), "cols": len(cols),
                                                            "caption": cap or None, "method": "text"}}))
    return out


def page_tables(page, loc: str, max_tables: int = 8) -> list[Chunk]:
    """A2: tables on a text page -> markdown chunks with caption, locator `<p> table n`.

    Material-property pages (Ftu/Fty with ksi/MPa) use the geometry grid (aligned_grid), which keeps
    spanning headers and multi-line header cells; other pages use PyMuPDF's ruled-table detection."""
    txt = page.get_text("text") or ""

    def plausible(g: list[list[str]]) -> bool:
        return bool(g) and _plausible_table([[c.replace(SPAN_SEP, "") for c in r] for r in g])

    if PROPERTY_PAGE.search(txt):
        # whether the page is one geometry grid is decided as before (no temper-code columns, words printed
        # twice kept): with them a page of prose around a ruled table (a design manual printing its text twice)
        # passed as a grid and lost the table; only turned pages read differently, which is the point
        grid, cap = aligned_grid(page, code_columns=False, once=False)
        if plausible(grid):
            coded, cap2 = aligned_grid(page)
            if plausible(coded):
                grid, cap = coded, cap2
            return [Chunk(f"{loc} table 1", (f"{cap}\n\n" if cap else "") + rows_to_markdown(grid), has_table=True,
                          quality={"table": {"rows": len(grid), "cols": len(grid[0]), "caption": cap or None,
                                             "method": "aligned"}})]
    try:
        # ruled tables only, as PyMuPDF 1.x does: 2.x adds the layout analyser's borderless tables (use_layout),
        # one row per printed line with stacked fractions apart, which pre-empted text_tables (CI, 2026-11-02)
        try:
            found = page.find_tables(strategy="lines", use_layout=False)
        except TypeError:                                  # PyMuPDF 1.x: no layout analyser
            found = page.find_tables(strategy="lines")
    except Exception:
        return []
    out: list[Chunk] = []
    for t_i, t in enumerate(list(found.tables)[:max_tables], 1):
        try:
            rows = t.extract() or []
            hdr = getattr(t, "header", None)
            if hdr is not None and getattr(hdr, "external", False) and getattr(hdr, "names", None):
                rows = [list(hdr.names)] + rows
        except Exception:
            continue
        if _merged(rows):                      # multi-line merged cells -> rebuild from word positions
            grid = word_grid(page, t)
            if grid:
                rows = grid
        rows = [[LEADER.sub("", re.sub(r"\s+", " ", c or "")).strip(" .") for c in r] for r in rows]
        rows = [r for r in rows if any(r)]
        if not _plausible_table(rows):
            continue
        cap = _caption(page, t.bbox if hasattr(t.bbox, "y0") else __import__("fitz").Rect(t.bbox))
        md = rows_to_markdown(rows)
        out.append(Chunk(f"{loc} table {t_i}", (f"{cap}\n\n" if cap else "") + md, has_table=True,
                         quality={"table": {"rows": len(rows), "cols": max(len(r) for r in rows),
                                            "caption": cap or None}}))
    return out or text_tables(page, loc, max_tables)


class PdfExtractor(Extractor):
    exts = ("pdf",)

    def __init__(self, cfg=None, ocr_now: bool = False, progress=None):
        super().__init__(cfg)
        self.ocr_now = ocr_now
        self.progress = progress        # S5: called after every OCR'd page (refresh-lock heartbeat)

    # -------------------------------------------------------------- OCR helpers
    def _render(self, page, dpi: int):
        import fitz
        from PIL import Image
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
        return Image.frombytes("L", (pix.width, pix.height), pix.samples)

    def _tess(self, page, dpi: int, psm: int) -> tuple[str, float, list[tuple[str, float]]]:
        """Tesseract at `dpi` / psm -> (text with line breaks, mean word conf, [(word, conf)])."""
        import pytesseract
        d = pytesseract.image_to_data(self._render(page, dpi), lang=self.cfg.ocr.lang,
                                      config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
        lines, words, key, cur = [], [], None, []
        for i, word in enumerate(d["text"]):
            if not word or not word.strip():
                continue
            k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            if k != key:
                if cur:
                    lines.append(" ".join(cur))
                if key is not None and k[:2] != key[:2]:
                    lines.append("")                      # paragraph break
                key, cur = k, []
            cur.append(word)
            try:
                c = float(d["conf"][i])
            except (TypeError, ValueError):
                c = -1.0
            words.append((word, c))
        if cur:
            lines.append(" ".join(cur))
        confs = [c for _, c in words if c >= 0]
        return "\n".join(lines), (sum(confs) / len(confs) if confs else 0.0), words

    def _ocr_page(self, page) -> tuple[str, str, float, list[tuple[str, float]]]:
        """S2 page triage. Returns (text, mode, mean_conf, words); mode = blank | text | escalated | full.

        1. 36-dpi thumbnail ink fraction < blank_ink -> blank, no OCR.
        2. text pass at text_dpi, psm 3.
        3. table-like (digit ratio) or low confidence -> rerun at dpi, psm 6; keep the higher-confidence pass.
        """
        import numpy as np
        import pytesseract

        ocr = self.cfg.ocr
        if ocr.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = str(ocr.tesseract_cmd)
        if not ocr.triage:
            txt, conf, words = self._tess(page, ocr.dpi, 3)
            return txt, "full", conf, words
        thumb = np.asarray(self._render(page, 36), dtype=np.uint8)
        if thumb.size and float((thumb < 128).mean()) < ocr.blank_ink:
            return "", "blank", 0.0, []
        txt, conf, words = self._tess(page, ocr.text_dpi, 3)
        body = [ch for ch in txt if not ch.isspace()]
        digits = sum(ch.isdigit() for ch in body) / max(1, len(body))
        if conf < ocr.escalate_conf or digits > ocr.table_digit_ratio or not body:
            txt2, conf2, words2 = self._tess(page, ocr.dpi, 6)
            if conf2 >= conf or not body:
                return txt2, "escalated", conf2, words2
        return txt, "text", conf, words

    def _page_quality(self, text: str, conf: float, words: list[tuple[str, float]]) -> dict:
        from ..quality import low_conf_numbers, ocr_confusions
        return {"ocr_conf": round(conf, 1),
                "low_conf_numbers": low_conf_numbers(words, self.cfg.ocr.low_conf_numeric),
                "ocr_confusions": ocr_confusions(text)}

    # ---------------------------------------------------------------- extract
    def _tables_on(self) -> tuple[bool, int]:
        t = getattr(self.cfg, "tables", None) or {}
        return bool(t.get("enabled", True)), int(t.get("max_per_page", 8))

    def extract(self, path: Path) -> ExtractResult:
        import fitz

        res = ExtractResult()
        try:
            doc = fitz.open(str(path))
        except Exception as exc:  # corrupt / unreadable
            return ExtractResult(status="failed", error=f"open: {exc}")
        try:
            if doc.needs_pass and not doc.authenticate(""):
                return ExtractResult(status="failed", error="encrypted")
            meta = doc.metadata or {}
            res.meta = {
                "title": (meta.get("title") or "").strip() or None,
                "authors": (meta.get("author") or "").strip() or None,
                "year": _year(meta),
                "page_count": doc.page_count,
            }
            min_chars = self.cfg.ocr.min_chars_per_page if self.cfg else 50
            tables_on, max_tables = self._tables_on() if self.cfg else (True, 8)
            pages: list[tuple[str, str]] = []
            quality: dict[int, dict] = {}
            tables: dict[int, list[Chunk]] = {}
            sparse = 0
            for i, page in enumerate(doc):
                try:
                    label = page.get_label() or ""
                except Exception:
                    label = ""
                loc = f"p.{label}" if label else f"p.{i + 1}"
                if label and label != str(i + 1):
                    loc += f" (pdf {i + 1})"
                txt = clean(page.get_text("text") or "")
                if len(txt) < min_chars:
                    sparse += 1
                elif tables_on and NUM_ROW.search(txt):
                    tables[i] = page_tables(page, loc, max_tables)
                pages.append((loc, txt))

            scanned = doc.page_count > 0 and sparse / doc.page_count > 0.5
            res.meta["scanned"] = scanned
            ocr_ok = bool(self.cfg and self.cfg.ocr.enabled and self.cfg.ocr.tesseract_cmd)
            if scanned and ocr_ok and self.ocr_now:
                limit = self.cfg.ocr.max_pages or doc.page_count
                tally = {"blank": 0, "text": 0, "escalated": 0, "full": 0}
                confs: list[float] = []
                failed: list[int] = []
                for i, page in enumerate(doc):
                    if i >= limit:
                        res.meta["ocr_skipped_pages"] = doc.page_count - limit      # ocr.max_pages, said aloud
                        break
                    loc, txt = pages[i]
                    if len(txt) < min_chars:
                        try:
                            o, mode, conf, words = self._ocr_page(page)
                            o = clean(o)
                        except Exception as exc:
                            # one page's failure no longer ends OCR for the document: the rest is read, and the
                            # document stays pending so run_ocr tries the failed pages again (correctness audit
                            # 2026-10-31: pages after the first error were never read and pending was cleared)
                            res.error = res.error or f"ocr: {exc}"
                            failed.append(i + 1)
                            continue
                        tally[mode] += 1
                        if self.progress:
                            self.progress()
                        if o:
                            confs.append(conf)
                            pages[i] = (f"{loc} [OCR]", o)
                            quality[i] = self._page_quality(o, conf, words)
                            res.ocr_used = True
                res.meta["ocr_pages"] = tally
                if failed:
                    res.meta["ocr_failed_pages"] = failed[:200]
                    res.ocr_pending = True
                if confs:
                    res.meta["ocr_mean_conf"] = round(sum(confs) / len(confs), 1)
            elif scanned and ocr_ok:
                res.ocr_pending = True

            n_tables = 0
            for i, (loc, txt) in enumerate(pages):
                if txt:
                    pieces = split_long(txt)
                    q = quality.get(i)
                    for j, piece in enumerate(pieces):
                        l = loc if len(pieces) == 1 else f"{loc} part {j + 1}"
                        pq = None
                        if q:
                            pq = {"ocr_conf": q["ocr_conf"],
                                  "low_conf_numbers": [t for t in q["low_conf_numbers"] if t in piece],
                                  "ocr_confusions": [t for t in q["ocr_confusions"] if t in piece]}
                        res.chunks.append(Chunk(l, piece, has_table=bool(NUM_ROW.search(piece)), quality=pq))
                for tc in tables.get(i, []):
                    res.chunks.append(tc)
                    n_tables += 1
            res.meta["tables"] = n_tables
            if not res.meta.get("title"):
                first = next((t for _, t in pages if t), "")
                res.meta["title_guess"] = first.split("\n", 1)[0][:200] if first else None
            if not res.chunks:
                res.status = "empty"
        except Exception as exc:
            res.status, res.error = "failed", f"extract: {exc}"
        finally:
            doc.close()
        return res
