"""Extractor base types.

Every extractor turns one file into a list of :class:`Chunk` objects, each with
a precise ``locator`` (page / sheet!range / slide / section / line span).
Extractors never write to the source file (guardrail G1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNIT_RE = re.compile(
    r"\b(ksi|psi|MPa|GPa|Pa|kPa|lbf|lb|kip|kips|N|kN|mm|in\.?|inch|ft|m|cm|"
    r"N-m|N·m|in-lb|in-lbs|ft-lb|°F|°C|degF|degC|Hz|msi|lb/in\^?3|kg/m\^?3)\b"
)


@dataclass
class Chunk:
    locator: str
    text: str
    has_table: bool = False
    quality: dict | None = None     # A5/A6: OCR confidence + suspicious numeric tokens (never alters text)

    @property
    def units(self) -> str:
        found = sorted({m.group(1) for m in UNIT_RE.finditer(self.text)})
        return ",".join(found[:20])


@dataclass
class ExtractResult:
    chunks: list[Chunk] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)   # title, authors, year, page_count...
    ocr_used: bool = False
    ocr_pending: bool = False   # scanned doc whose OCR was deferred
    status: str = "ok"          # ok | empty | failed | skipped
    error: str = ""

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in self.chunks)


def clean(text: str) -> str:
    """Normalise whitespace while keeping line structure."""
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_long(text: str, max_chars: int = 6000) -> list[str]:
    """Split text on paragraph boundaries so no piece exceeds ``max_chars``
    (~1500 tokens)."""
    if len(text) <= max_chars:
        return [text]
    parts, buf = [], ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > max_chars and buf:
            parts.append(buf)
            buf = ""
        while len(para) > max_chars:
            parts.append(para[:max_chars])
            para = para[max_chars:]
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        parts.append(buf)
    return parts


def rows_to_markdown(rows: list[list[Any]]) -> str:
    """Render a 2-D list as a markdown table (first row = header)."""
    rows = [["" if v is None else str(v).replace("|", "/").replace("\n", " ").strip() for v in r]
            for r in rows if any(v not in (None, "") for v in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(rows[0]) + " |"
    sep = "|" + "---|" * width
    body = ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join([head, sep, *body])


def col_letter(idx: int) -> str:
    """1-based column index -> Excel letters."""
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


class Extractor:
    """Base class. Subclasses set ``exts`` and implement :meth:`extract`."""

    exts: tuple[str, ...] = ()
    needs_com: bool = False
    version: str = ""      # set when the output changes: the indexer re-reads files read by another version

    def __init__(self, cfg=None):
        self.cfg = cfg

    def extract(self, path: Path) -> ExtractResult:  # pragma: no cover - abstract
        raise NotImplementedError
