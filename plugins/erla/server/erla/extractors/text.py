"""Plain-text family (txt, md, csv, json) and solver files (f06, bdf/dat, op2, inp)."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from pathlib import Path

from .base import Chunk, Extractor, ExtractResult, clean, rows_to_markdown

WINDOW_LINES = 120
OVERLAP_LINES = 18     # ~15 %


def read_text(path: Path, limit_bytes: int | None = None) -> str:
    raw = Path(path).read_bytes() if limit_bytes is None else Path(path).open("rb").read(limit_bytes)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def line_windows(lines: list[str], res: ExtractResult) -> None:
    n = len(lines)
    step = WINDOW_LINES - OVERLAP_LINES
    for start in range(0, max(n, 1), step):
        block = lines[start:start + WINDOW_LINES]
        text = clean("\n".join(block))
        if text:
            res.chunks.append(Chunk(f"line {start + 1}-{start + len(block)}", text))
        if start + WINDOW_LINES >= n:
            break


class TextExtractor(Extractor):
    exts = ("txt", "md", "json")

    def extract(self, path: Path) -> ExtractResult:
        try:
            text = read_text(path)
        except Exception as exc:
            return ExtractResult(status="failed", error=f"read: {exc}")
        res = ExtractResult(meta={"title": None})
        lines = text.splitlines()
        first = next((l.strip("# ").strip() for l in lines if l.strip()), "")
        res.meta["title_guess"] = first[:200] or None
        line_windows(lines, res)
        res.status = "ok" if res.chunks else "empty"
        return res


class CsvExtractor(Extractor):
    exts = ("csv",)

    def extract(self, path: Path) -> ExtractResult:
        try:
            text = read_text(path)
            rows = list(csv.reader(io.StringIO(text)))
        except Exception as exc:
            return ExtractResult(status="failed", error=f"read: {exc}")
        res = ExtractResult(meta={"title": None})
        header = rows[0] if rows else []
        for start in range(0, len(rows), 200):
            block = rows[start:start + 200]
            body = block if start == 0 else [header] + block
            md = rows_to_markdown(body)
            if md:
                res.chunks.append(Chunk(f"rows {start + 1}-{start + len(block)}", md, has_table=True))
        res.status = "ok" if res.chunks else "empty"
        return res


# ------------------------------------------------------------------ solver
F06_SOLVER = re.compile(r"(MSC[\s.]*NASTRAN|NX\s+NASTRAN|SIMCENTER\s+NASTRAN|NEI\s+NASTRAN|NASTRAN)[^\n]{0,60}?(?:VERSION|V)\s*([\w.\-]+)?", re.I)
SOL_RE = re.compile(r"^\s*SOL\s+(\w+)", re.I | re.M)
SUBCASE_RE = re.compile(r"^\s*SUBCASE\s+(\d+)", re.I | re.M)
LABEL_RE = re.compile(r"^\s*(TITLE|SUBTITLE|LABEL)\s*=\s*(.+)$", re.I | re.M)
EPS_RE = re.compile(r"EPSILON\s*=?\s*([-+]?\d*\.\d+E[-+]\d+)", re.I)
CARD_RE = re.compile(r"^([A-Z][A-Z0-9]{2,7})[\s,*]", re.M)


class F06Extractor(Extractor):
    """Nastran .f06: metadata + message summary chunk + windowed text (capped)."""

    exts = ("f06",)
    MAX_BYTES = 40 * 1024 * 1024     # index first 40 MB of very large outputs

    def extract(self, path: Path) -> ExtractResult:
        try:
            text = read_text(path, self.MAX_BYTES)
        except Exception as exc:
            return ExtractResult(status="failed", error=f"read: {exc}")
        up = text.upper()
        m = F06_SOLVER.search(text)
        sol = SOL_RE.search(text)
        subcases = sorted({int(s) for s in SUBCASE_RE.findall(text)})
        labels = [f"{k.upper()}={v.strip()[:80]}" for k, v in LABEL_RE.findall(text)][:20]
        counts = {k: up.count(k) for k in ("USER FATAL", "SYSTEM FATAL", "USER WARNING", "SYSTEM WARNING")}
        eps = EPS_RE.findall(text)[:10]
        meta = {"title": next((v for v in labels if v.startswith("TITLE=")), None),
                "solver": (m.group(1).upper() if m else None), "solver_version": (m.group(2) if m else None),
                "sol": sol.group(1) if sol else None, "subcases": subcases[:200],
                "messages": counts, "epsilon": eps, "truncated": Path(path).stat().st_size > self.MAX_BYTES}
        summary = (f"NASTRAN OUTPUT SUMMARY\nsolver: {meta['solver']} {meta['solver_version'] or ''}\n"
                   f"SOL {meta['sol']}\nsubcases: {subcases[:50]}\nlabels: {'; '.join(labels)}\n"
                   f"messages: {counts}\nepsilon: {eps}")
        res = ExtractResult(meta=meta)
        res.chunks.append(Chunk("summary", summary))
        line_windows(text.splitlines(), res)
        res.status = "ok"
        return res


class BdfExtractor(Extractor):
    """Nastran bulk data (.bdf/.dat) and Abaqus (.inp): metadata only + case control."""

    exts = ("bdf", "dat", "inp")
    MAX_BYTES = 20 * 1024 * 1024

    def extract(self, path: Path) -> ExtractResult:
        try:
            text = read_text(path, self.MAX_BYTES)
        except Exception as exc:
            return ExtractResult(status="failed", error=f"read: {exc}")
        ext = Path(path).suffix.lower()
        res = ExtractResult()
        if ext == ".inp":
            kw = Counter(k.upper() for k in re.findall(r"^\*\s*([A-Za-z][A-Za-z ]+)", text, re.M))
            steps = re.findall(r"^\*STEP.*$", text, re.I | re.M)
            res.meta = {"title": None, "solver": "ABAQUS", "keywords": dict(kw.most_common(30))}
            res.chunks.append(Chunk("summary", f"ABAQUS INPUT SUMMARY\nsteps: {len(steps)}\nkeywords: {dict(kw.most_common(30))}"))
        else:
            sol = SOL_RE.search(text)
            cend = re.search(r"^\s*CEND\b", text, re.I | re.M)
            begin = re.search(r"^\s*BEGIN\s+BULK", text, re.I | re.M)
            if not (sol or begin):
                # .dat is ambiguous: treat as generic text data file
                return TextExtractor(self.cfg).extract(path)
            cards = Counter(CARD_RE.findall(text[begin.end():] if begin else text))
            labels = [f"{k.upper()}={v.strip()[:80]}" for k, v in LABEL_RE.findall(text)][:20]
            case_ctrl = text[: (cend.end() if cend else 0)] + "\n" + \
                text[(cend.end() if cend else 0):(begin.start() if begin else 0)]
            res.meta = {"title": next((v for v in labels if v.startswith("TITLE=")), None),
                        "solver": "NASTRAN", "sol": sol.group(1) if sol else None,
                        "subcases": sorted({int(s) for s in SUBCASE_RE.findall(text)})[:200],
                        "cards": dict(cards.most_common(40))}
            res.chunks.append(Chunk("summary", f"NASTRAN INPUT SUMMARY\nSOL {res.meta['sol']}\n"
                                    f"subcases: {res.meta['subcases'][:50]}\nlabels: {'; '.join(labels)}\n"
                                    f"cards: {res.meta['cards']}"))
            cc = clean(case_ctrl)[:6000]
            if cc:
                res.chunks.append(Chunk("case control", cc))
        res.status = "ok"
        return res


class Op2Extractor(Extractor):
    """Binary .op2: metadata only via pyNastran when available."""

    exts = ("op2",)

    def extract(self, path: Path) -> ExtractResult:
        stem = Path(path).stem
        try:
            from pyNastran.op2.op2 import OP2  # type: ignore
        except Exception:
            return ExtractResult(chunks=[Chunk("summary", f"NASTRAN OP2 binary results file: {stem} (pyNastran not installed; metadata only)")],
                                 meta={"title": stem, "solver": "NASTRAN"}, status="ok")
        try:
            op2 = OP2(debug=False, log=None)
            op2.read_op2(str(path), build_dataframe=False, skip_undefined_matrices=True)
            info = op2.get_op2_stats(short=True)
        except Exception as exc:
            return ExtractResult(status="failed", error=f"op2: {exc}")
        return ExtractResult(chunks=[Chunk("summary", f"NASTRAN OP2 RESULTS {stem}\n{info[:5000]}")],
                             meta={"title": stem, "solver": "NASTRAN"}, status="ok")
