"""PDF extractor: PyMuPDF text per page, table extraction (A2), Tesseract OCR fallback for scanned
pages with page triage (S2) and per-word confidence flags (A5/A6)."""

from __future__ import annotations

import re
from pathlib import Path

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


def aligned_grid(page, clip=None, y_tol: float = 3.2, gap: float = 12.0) -> tuple[list[list[str]], str]:
    """A2 for borderless tables (MMPDS-10+ allowables pages): rebuild rows and columns from geometry.

    Returns (grid, caption). The caption line ('Table 3.7.9.0(b1). Design ... of 7075 Aluminum Alloy')
    and centred continuation lines directly below it are returned as the caption, not as rows; the
    page header above the caption is ignored. Data columns = x-centres of the numbers on the line with
    the most numbers (the widest data row). Text left of the first data column is the row label. Other
    phrases go to the nearest data column; a phrase covering several column centres (a spanning
    header such as 'Sheet', or a thickness range centred over an A/B pair) is placed in each column it
    covers, marked with SPAN_SEP."""
    words = page.get_text("words", clip=clip) if clip is not None else page.get_text("words")
    if not words:
        return [], ""
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
    centers = [(w[0] + w[2]) / 2 for w in tmpl[1] if NUM_TOKEN.match(w[4])]
    if len(centers) < 2:
        return [], caption
    half = min(b - a for a, b in zip(centers, centers[1:])) / 2
    label_edge = centers[0] - max(half, 8.0)
    tol = max(half * 0.5, 3.0)
    grid = []
    for _yc, ws in body:
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
        for ph in phrases:
            x0, x1 = ph[0][0], ph[-1][2]
            text = " ".join(w[4] for w in ph)
            covered = [i for i, c in enumerate(centers) if x0 - tol <= c <= x1 + tol]
            if len(covered) >= 2 and not NUM_TOKEN.match(text):
                for i in covered:
                    row[i + 1] = f"{row[i + 1]} {SPAN_SEP}{text}".strip()
            else:
                xc = (x0 + x1) / 2
                i = min(range(len(centers)), key=lambda k: abs(centers[k] - xc))
                row[i + 1] = f"{row[i + 1]} {text}".strip()
        row = [LEADER.sub("", c).strip(" .") for c in row]
        if any(row):
            grid.append(row)
    return grid, caption


def _merged(rows: list[list[str]]) -> bool:
    return any((c or "").count("\n") >= 2 for r in rows for c in r)


def page_tables(page, loc: str, max_tables: int = 8) -> list[Chunk]:
    """A2: tables on a text page -> markdown chunks with caption, locator `<p> table n`.

    Material-property pages (Ftu/Fty with ksi/MPa) use the geometry grid (aligned_grid), which keeps
    spanning headers and multi-line header cells; other pages use PyMuPDF's ruled-table detection."""
    txt = page.get_text("text") or ""
    if PROPERTY_PAGE.search(txt):
        grid, cap = aligned_grid(page)
        if grid and _plausible_table([[c.replace(SPAN_SEP, "") for c in r] for r in grid]):
            return [Chunk(f"{loc} table 1", (f"{cap}\n\n" if cap else "") + rows_to_markdown(grid), has_table=True,
                          quality={"table": {"rows": len(grid), "cols": len(grid[0]), "caption": cap or None,
                                             "method": "aligned"}})]
    try:
        found = page.find_tables()
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
    return out


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
