"""Extractor registry: extension -> extractor class."""

from __future__ import annotations

from .base import Chunk, Extractor, ExtractResult
from .legacy import DocExtractor, PptExtractor
from .office import DocxExtractor, PptxExtractor, XlsExtractor, XlsxExtractor
from .pdf import PdfExtractor
from .text import BdfExtractor, CsvExtractor, F06Extractor, Op2Extractor, TextExtractor

EXTRACTORS: dict[str, type[Extractor]] = {}
for _cls in (PdfExtractor, DocxExtractor, XlsxExtractor, XlsExtractor, PptxExtractor,
             DocExtractor, PptExtractor, TextExtractor, CsvExtractor, F06Extractor,
             BdfExtractor, Op2Extractor):
    for _e in _cls.exts:
        EXTRACTORS[_e] = _cls

COM_EXTS = {e for e, c in EXTRACTORS.items() if getattr(c, "needs_com", False)}

__all__ = ["Chunk", "Extractor", "ExtractResult", "EXTRACTORS", "COM_EXTS", "PdfExtractor",
           "DocxExtractor", "XlsxExtractor", "XlsExtractor", "PptxExtractor", "DocExtractor",
           "PptExtractor", "TextExtractor", "CsvExtractor", "F06Extractor", "BdfExtractor",
           "Op2Extractor"]
