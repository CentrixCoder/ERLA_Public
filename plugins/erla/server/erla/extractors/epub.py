"""EPUB 2 and 3 extractor (standard library only: zipfile, xml.etree, html.parser).

An EPUB is a ZIP holding an OPF package whose spine gives the reading order of its XHTML content
documents. Text is extracted per spine item in that order: headings, paragraphs, list items, tables
as markdown rows (property extraction A3 and verify_packet read them like PDF tables) and code
blocks verbatim. Locators:

  p.<label>             printed page from the book's page-list (EPUB 3 nav page-list or EPUB 2 NCX
                        pageList), for the spine items the page-list covers
  item <n> §<heading>   otherwise: spine position n (1-based, OPF order) and the section heading
  item <n>              text of spine item n before its first heading

A locator that repeats gets " (2)", a long section " part 2" (as for DOCX).

DRM: content documents encrypted under META-INF/encryption.xml (other than font obfuscation) are
detected and reported, never decrypted. A damaged archive or package fails closed with the reason;
an unreadable spine item leaves a marker chunk with a quality warning at its own locator.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from .base import Chunk, Extractor, ExtractResult, rows_to_markdown, split_long

MAX_MEMBER = 64 * 1024 * 1024       # bytes uncompressed per archive member (zip-bomb bound)
MAX_TABLE = 6000                    # a longer table is split into row blocks, header repeated
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
DRM_FILES = {"META-INF/rights.xml": "Adobe ADEPT", "META-INF/license.lcpl": "Readium LCP",
             "META-INF/sinf.xml": "Apple FairPlay"}
XHTML_TYPES = {"application/xhtml+xml", "text/html", "application/x-dtbook+xml"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK = HEADINGS | {"p", "div", "li", "dt", "dd", "blockquote", "figcaption", "figure", "section", "article",
                    "aside", "header", "footer", "nav", "main", "ul", "ol", "dl", "hr", "address", "body"}
SKIP = {"head", "script", "style", "svg", "annotation", "annotation-xml", "noscript"}
NAV_SKIP = {"page-list", "doc-pagelist", "landmarks"}
INVISIBLE = str.maketrans("", "", "\xad​‌‍﻿")


class EpubError(Exception):
    """The archive or its package cannot be read: the document fails closed."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el: ET.Element, name: str) -> str:
    """Attribute by local name (opf:scheme and scheme alike)."""
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return ""


def _norm(text: str) -> str:
    return " ".join(text.translate(INVISIBLE).split())


def _read(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.flag_bits & 0x1:
        raise EpubError(f"{name} is password-protected in the archive; ERLA does not decrypt files")
    if info.file_size > MAX_MEMBER:
        raise EpubError(f"{name}: {info.file_size} bytes uncompressed exceeds the {MAX_MEMBER} byte limit")
    with zf.open(info) as f:
        data = f.read(MAX_MEMBER + 1)
    if len(data) > MAX_MEMBER:
        raise EpubError(f"{name}: more than {MAX_MEMBER} bytes uncompressed")
    return data


def _xml(data: bytes, what: str) -> ET.Element:
    if b"<!ENTITY" in data:
        raise EpubError(f"{what}: XML entity declarations are refused")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise EpubError(f"{what}: malformed XML ({exc})") from exc


def _join(base: str, href: str) -> tuple[str, str] | None:
    """Resolve an href against a member's folder -> (archive path, fragment); None if it leaves the archive."""
    part, _, frag = href.strip().partition("#")
    if re.match(r"^[a-z][a-z0-9+.-]*:", part, re.I):
        return None                                   # an external link, not a member
    path = posixpath.normpath(posixpath.join(base, unquote(part))) if part else ""
    if path.startswith("../") or path.startswith("/") or path == "..":
        return None
    return path, unquote(frag)


def _decode(data: bytes) -> tuple[str, str | None]:
    """Content document bytes -> (text, warning if characters had to be replaced)."""
    if data.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    else:
        m = re.match(rb"\s*<\?xml[^>]*encoding\s*=\s*[\"']([A-Za-z0-9._-]+)", data)
        enc = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return data.decode(enc), None
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace"), (f"text is not valid {enc}: undecodable characters were "
                                                        "replaced - check values against the book")


# ------------------------------------------------------------------ content
class _Table:
    """Rows of one HTML table; colspan and rowspan positions become blank cells (as merged PDF cells)."""

    def __init__(self) -> None:
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.caption: list[str] | None = None
        self.caption_text = ""
        self.depth = 1
        self.colspan = 1
        self.rowspan = 1
        self.spans: dict[int, int] = {}

    def _fill(self) -> None:
        row = self.row
        if row is None:
            return
        while len(row) in self.spans:
            c = len(row)
            row.append("")
            self.spans[c] -= 1
            if not self.spans[c]:
                del self.spans[c]

    def _end_cell(self) -> None:
        if self.cell is None:
            return
        if self.row is None:
            self.row = []
        col = len(self.row)
        self.row.append(_norm("".join(self.cell)))
        self.row.extend([""] * (self.colspan - 1))
        if self.rowspan > 1:
            for c in range(col, col + self.colspan):
                self.spans[c] = self.rowspan - 1
        self.cell = None

    def _end_row(self) -> None:
        self._end_cell()
        if self.row is not None:
            self._fill()
            self.rows.append(self.row)
        self.row = None

    def start(self, tag: str, attrs: dict[str, str | None]) -> None:
        if tag == "table":
            self.depth += 1
        if self.depth > 1 or tag in ("br", "p", "div", "li") or tag in HEADINGS:
            for buf in (self.cell, self.caption):     # a nested table or a block inside a cell: its text
                if buf is not None:                   # stays in the cell, words kept apart
                    buf.append(" ")
                    break
            return
        if tag == "tr":
            self._end_row()
            self.row = []
            self._fill()
        elif tag in ("td", "th"):
            self._end_cell()
            if self.row is None:
                self.row = []
            self._fill()
            self.cell = []

            def span(name: str) -> int:
                v = (attrs.get(name) or "1").strip()
                return min(max(int(v), 1), 50) if v.isdigit() else 1
            self.colspan, self.rowspan = span("colspan"), span("rowspan")
        elif tag == "caption":
            self.caption = []

    def data(self, text: str) -> None:
        if self.cell is not None:
            self.cell.append(text)
        elif self.caption is not None:
            self.caption.append(text)

    def end(self, tag: str) -> bool:
        """True when the outermost table closes."""
        if tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self._end_row()
                return True
            return False
        if self.depth > 1:
            return False
        if tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr":
            self._end_row()
        elif tag == "caption" and self.caption is not None:
            self.caption_text = _norm("".join(self.caption))
            self.caption = None
        return False

    def text(self) -> str:
        rows = [r for r in self.rows if any(c for c in r)]
        md = rows_to_markdown(rows)
        if len(md) > MAX_TABLE and len(rows) > 2:
            blocks, size, cur = [], 0, []
            for r in rows[1:]:
                line = len(" | ".join(r)) + 4
                if cur and size + line > MAX_TABLE:
                    blocks.append(rows_to_markdown([rows[0], *cur]))
                    cur, size = [], 0
                cur.append(r)
                size += line
            if cur:
                blocks.append(rows_to_markdown([rows[0], *cur]))
            md = "\n\n".join(blocks)
        if not md:
            return ""
        return f"{self.caption_text}\n{md}" if self.caption_text else md


class _Content(HTMLParser):
    """One XHTML content document -> events in reading order:
    ("h1".."h6", text), ("p", text), ("li", text), ("pre", verbatim text), ("table", markdown),
    ("page", anchor id) where an element carries an id the page-list points at."""

    def __init__(self, anchors: set[str]):
        super().__init__(convert_charrefs=True)
        self.anchors = anchors
        self.events: list[tuple[str, str]] = []
        self.buf: list[str] = []
        self.kind = "p"
        self.skip = 0                     # depth inside a skipped element (head, script, page-list nav, ...)
        self.skip_tag = ""
        self.pre: list[str] | None = None
        self.table: _Table | None = None
        self.pending: list[str] = []      # page anchors met inside a table or code block

    def _flush(self, keep_kind: bool = False) -> None:
        raw = "".join(self.buf).translate(INVISIBLE)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        text = "\n".join(line for line in lines if line)
        if text:
            self.events.append((self.kind, f"- {text}" if self.kind == "li" else text))
        self.buf.clear()
        if not keep_kind:
            self.kind = "p"

    def _anchor(self, ident: str) -> None:
        if self.table is not None or self.pre is not None:
            self.pending.append(ident)         # a page break inside a table or code block: the block
            return                             # stays whole on the page it starts on
        self._flush(keep_kind=True)
        self.events.append(("page", ident))

    def _release(self) -> None:
        for ident in self.pending:
            self.events.append(("page", ident))
        self.pending.clear()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip:
            if tag == self.skip_tag:
                self.skip += 1
            return
        a = dict(attrs)
        if tag in SKIP or (tag == "nav" and NAV_SKIP & set(f"{a.get('epub:type') or ''} {a.get('role') or ''}".split())):
            self.skip, self.skip_tag = 1, tag      # a nav document in the spine: its page-list is not text
            return
        if self.table is not None:
            self.table.start(tag, a)
        elif self.pre is not None:
            if tag == "br":
                self.pre.append("\n")
        elif tag == "pre":
            self._flush()
            self.pre = []
        elif tag == "table":
            self._flush()
            self.table = _Table()
        elif tag == "br":
            self.buf.append("\n")
        elif tag in BLOCK:
            inherit = self.kind == "li" and tag in ("p", "div") and not "".join(self.buf).strip()
            self._flush(keep_kind=inherit)         # <li><p>text</p></li> stays a list item
            if not inherit:
                self.kind = tag if tag in HEADINGS or tag == "li" else "p"
        for key in ("id", "name"):
            v = a.get(key)
            if v and v in self.anchors:
                self._anchor(v)
                break

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            if tag == self.skip_tag:
                self.skip -= 1
            return
        if self.table is not None:
            if self.table.end(tag):
                md = self.table.text()
                if md:
                    self.events.append(("table", md))
                self.table = None
                self._release()
        elif self.pre is not None:
            if tag == "pre":
                text = "".join(self.pre).translate(INVISIBLE).replace("\r\n", "\n").replace("\xa0", " ")
                text = text[1:] if text.startswith("\n") else text
                if text.strip():
                    self.events.append(("pre", text.rstrip()))
                self.pre = None
                self._release()
        elif tag in BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.table is not None:
            self.table.data(data)
        elif self.pre is not None:
            self.pre.append(data)
        else:
            self.buf.append(data)

    def finish(self) -> list[tuple[str, str]]:
        self.close()
        if self.table is not None:             # unclosed at the end of the document
            md = self.table.text()
            if md:
                self.events.append(("table", md))
            self.table = None
        if self.pre is not None:
            self.events.append(("pre", "".join(self.pre).strip("\n")))
            self.pre = None
        self._flush()
        self._release()
        return self.events


class _PageListNav(HTMLParser):
    """EPUB 3 navigation document: the <a href> targets and labels inside <nav epub:type="page-list">."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0                      # nesting inside the page-list nav
        self.href: str | None = None
        self.label: list[str] = []
        self.targets: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "nav":
            kinds = f"{a.get('epub:type') or ''} {a.get('role') or ''}".split()
            if self.depth or "page-list" in kinds or "doc-pagelist" in kinds:
                self.depth += 1
        elif self.depth and tag == "a" and a.get("href"):
            self.href, self.label = a.get("href"), []

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav" and self.depth:
            self.depth -= 1
        elif tag == "a" and self.href is not None:
            label = _norm("".join(self.label))
            if label:
                self.targets.append((self.href, label))
            self.href = None

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.label.append(data)


# ------------------------------------------------------------------ package
class _Package:
    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.names = set(zf.namelist())
        self.lower = {n.lower(): n for n in self.names}
        try:
            container = _xml(_read(zf, "META-INF/container.xml"), "META-INF/container.xml")
        except KeyError as exc:
            raise EpubError("no META-INF/container.xml: not an EPUB package") from exc
        rootfiles = [e for e in container.iter() if _local(e.tag) == "rootfile" and e.get("full-path")]
        opf = next((e for e in rootfiles if e.get("media-type") == "application/oebps-package+xml"),
                   rootfiles[0] if rootfiles else None)
        if opf is None:
            raise EpubError("container.xml names no package (rootfile)")
        self.opf_path = self.member(str(opf.get("full-path")))
        if self.opf_path is None:
            raise EpubError(f"package file {opf.get('full-path')} is missing from the archive")
        self.opf = _xml(self.read(self.opf_path), self.opf_path)
        self.base = posixpath.dirname(self.opf_path)
        self.version = self.opf.get("version") or ""
        self.manifest: dict[str, tuple[str, str, str]] = {}      # id -> (archive path, media type, properties)
        for el in self.opf.iter():
            if _local(el.tag) == "item" and el.get("id") and el.get("href"):
                hit = _join(self.base, str(el.get("href")))
                if hit:
                    self.manifest[str(el.get("id"))] = (hit[0], el.get("media-type") or "", el.get("properties") or "")
        spine = next((e for e in self.opf.iter() if _local(e.tag) == "spine"), None)
        if spine is None:
            raise EpubError("the package has no spine (reading order)")
        self.toc_id = spine.get("toc") or ""
        self.spine: list[str] = [str(e.get("idref")) for e in spine if _local(e.tag) == "itemref" and e.get("idref")]
        if not self.spine:
            raise EpubError("the spine is empty")

    def member(self, path: str) -> str | None:
        """Archive name for a package path (exact, else case-insensitive as some readers allow)."""
        return path if path in self.names else self.lower.get(path.lower())

    def read(self, path: str) -> bytes:
        name = self.member(path)
        if name is None:
            raise KeyError(path)
        return _read(self.zf, name)

    def metadata(self) -> dict[str, object]:
        md = next((e for e in self.opf.iter() if _local(e.tag) == "metadata"), None)
        found: dict[str, list[ET.Element]] = {}
        for el in (md.iter() if md is not None else []):
            found.setdefault(_local(el.tag), []).append(el)

        def texts(name: str) -> list[str]:
            return [t for t in (_norm(e.text or "") for e in found.get(name, [])) if t]

        isbn = None
        for el in found.get("identifier", []):
            t = _norm(el.text or "")
            digits = re.sub(r"[^0-9Xx]", "", t.lower().removeprefix("urn:isbn:"))
            if (_attr(el, "scheme").upper() == "ISBN" or t.lower().startswith("urn:isbn:")
                    or re.fullmatch(r"(?:97[89][- ]?)?\d[\d -]{7,15}[\dXx]", t)) and len(digits) in (10, 13):
                isbn = digits.upper()
                break
        date = next(iter(texts("date")), None)
        year = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", date or "")
        title = next(iter(texts("title")), None)
        return {"title": title, "authors": "; ".join(texts("creator")) or None,
                "publisher": next(iter(texts("publisher")), None), "date": date,
                "year": int(year.group(1)) if year else None, "isbn": isbn,
                "language": next(iter(texts("language")), None), "epub_version": self.version}

    def page_targets(self) -> tuple[list[tuple[str, str, str]], str]:
        """[(archive path, fragment, printed label)] from the EPUB 3 nav page-list, else the NCX pageList."""
        out: list[tuple[str, str, str]] = []
        nav = next((v for v in self.manifest.values() if "nav" in v[2].split()), None)
        if nav and self.member(nav[0]):
            p = _PageListNav()
            p.feed(_decode(self.read(nav[0]))[0])
            p.close()
            for href, label in p.targets:
                hit = _join(posixpath.dirname(nav[0]), href)
                if hit:
                    out.append((hit[0], hit[1], label))
            if out:
                return out, "nav page-list"
        ncx = self.manifest.get(self.toc_id) or next(
            (v for v in self.manifest.values() if v[1] == "application/x-dtbncx+xml"), None)
        if ncx and self.member(ncx[0]):
            root = _xml(self.read(ncx[0]), ncx[0])
            for pt in (e for e in root.iter() if _local(e.tag) == "pageTarget"):
                label = next((_norm(e.text or "") for e in pt.iter() if _local(e.tag) == "text"), "")
                src = next((e.get("src") or "" for e in pt.iter() if _local(e.tag) == "content"), "")
                hit = _join(posixpath.dirname(ncx[0]), src) if src else None
                if label and hit:
                    out.append((hit[0], hit[1], label))
            if out:
                return out, "NCX pageList"
        return [], ""

    def drm(self, content: set[str]) -> str | None:
        """Why the content is protected, or None. Font obfuscation alone is not DRM (it hides fonts only)."""
        if "META-INF/encryption.xml" not in self.names:
            return None
        scheme = next((s for f, s in DRM_FILES.items() if f in self.names), "unknown scheme")
        try:
            root = _xml(_read(self.zf, "META-INF/encryption.xml"), "META-INF/encryption.xml")
        except EpubError as exc:
            return f"DRM-protected EPUB? META-INF/encryption.xml cannot be read ({exc}); treated as protected"
        members = {self.member(c) or c for c in content}
        hit = 0
        for ed in (e for e in root.iter() if _local(e.tag) == "EncryptedData"):
            alg = next((m.get("Algorithm") or "" for m in ed.iter() if _local(m.tag) == "EncryptionMethod"), "")
            uri = next((c.get("URI") or "" for c in ed.iter() if _local(c.tag) == "CipherReference"), "")
            target = _join("", uri)
            if alg not in FONT_OBFUSCATION and target and (self.member(target[0]) or target[0]) in members:
                hit += 1
        if not hit:
            return None
        return (f"DRM-protected EPUB ({scheme}): {hit} content document(s) are encrypted. ERLA does not remove "
                "DRM; add a DRM-free copy of the book to the library")


# ------------------------------------------------------------------ assembly
class _Chunks:
    """Collects blocks under the current locator and emits unique, split chunks."""

    def __init__(self, res: ExtractResult):
        self.res = res
        self.loc = ""
        self.blocks: list[str] = []
        self.quality: dict[str, object] | None = None
        self.used: dict[str, int] = {}

    def unique(self, loc: str) -> str:
        n = self.used.get(loc.lower(), 0) + 1
        self.used[loc.lower()] = n
        return loc if n == 1 else f"{loc} ({n})"

    def flush(self) -> None:
        text = "\n\n".join(self.blocks).strip("\n")
        self.blocks = []
        if not text.strip() or not self.loc:
            return
        loc = self.unique(self.loc)
        for j, piece in enumerate(split_long(text)):
            table = any(line.startswith("|---") for line in piece.splitlines())
            self.res.chunks.append(Chunk(loc if j == 0 else self.unique(f"{loc} part {j + 1}"), piece,
                                         has_table=table, quality=self.quality))

    def start(self, loc: str) -> None:
        self.flush()
        self.loc = loc


class EpubExtractor(Extractor):
    exts = ("epub",)

    def extract(self, path: Path) -> ExtractResult:
        try:
            with zipfile.ZipFile(str(path)) as zf:
                return self._extract(zf)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as exc:
            return ExtractResult(status="failed", error=f"damaged EPUB archive: {exc}")
        except EpubError as exc:
            return ExtractResult(status="failed", error=f"EPUB: {exc}")
        except (OSError, RuntimeError, ValueError, zlib.error) as exc:   # bad deflate data, unknown compression
            return ExtractResult(status="failed", error=f"EPUB unreadable: {type(exc).__name__}: {exc}")

    def _extract(self, zf: zipfile.ZipFile) -> ExtractResult:
        pkg = _Package(zf)
        res = ExtractResult(meta=pkg.metadata())
        items = [(n, pkg.manifest.get(idref)) for n, idref in enumerate(pkg.spine, 1)]
        content = {m[0] for _, m in items if m}
        why = pkg.drm(content)
        if why:
            res.status, res.error = "failed", why
            res.meta["drm"] = True
            return res
        targets, source = pkg.page_targets()
        anchors: dict[str, dict[str, str]] = {}
        starts: dict[str, str] = {}
        for p, frag, label in targets:
            if frag:
                anchors.setdefault(p, {}).setdefault(frag, label)
            else:
                starts.setdefault(p, label)
        covered = set(anchors) | set(starts)
        res.meta.update({"spine_items": len(items), "page_list": len(targets) or None,
                         "page_count": len(targets) or None,
                         "locators": f"printed pages ({source}) + spine item sections" if targets
                         else "spine item + section heading"})
        out = _Chunks(res)
        problems: list[str] = []
        page: str | None = None
        read = 0
        for n, item in items:
            if item is None:
                problems.append(f"spine item {n}: not in the manifest")
                continue
            ipath, mtype, _props = item
            if mtype and mtype not in XHTML_TYPES:
                continue                                  # an image or SVG page in the spine: no text
            why_not = None
            try:
                data = pkg.read(ipath)
                text, warn = _decode(data)
                if not re.search(r"<(?:html|body)\b", text[:20000], re.I):
                    why_not = "not readable XHTML (damaged or encrypted)"
            except KeyError:
                why_not, text, warn = "missing from the archive", "", None
            except EpubError as exc:
                why_not, text, warn = str(exc), "", None
            if why_not:
                msg = f"EPUB spine item {n} ({ipath}) not indexed: {why_not}"
                problems.append(msg)
                out.start(f"item {n}")
                out.quality = {"warnings": [msg]}
                out.blocks.append(f"[{msg}]")
                out.flush()
                out.quality, out.loc, page = None, "", None
                continue
            read += 1
            if ipath not in covered:
                page = None                               # the page-list does not reach this item:
            if ipath in starts:                           # never carry a printed page into it
                page = starts[ipath]
                out.start(f"p.{page}")
            elif page is None:
                out.start(f"item {n}")
            quality: dict[str, object] | None = {"warnings": [f"EPUB spine item {n}: {warn}"]} if warn else None
            if quality != out.quality:                    # the warning travels with this item's text only
                out.flush()
                out.quality = quality
            parser = _Content(set(anchors.get(ipath, {})))
            parser.feed(text)
            for kind, value in parser.finish():
                if kind == "page":
                    page = anchors[ipath][value]
                    out.start(f"p.{page}")
                elif kind in HEADINGS:
                    if page is None:
                        out.start(f"item {n} §{value[:80]}")
                    out.blocks.append(value)
                else:
                    out.blocks.append(value)
            if page is None:
                out.flush()
                out.loc = ""
        out.flush()
        if problems:
            res.error = "; ".join(problems)[:500]
            res.meta["quality_warnings"] = problems[:50]
        if not read:
            res.status = "failed"
            res.error = "EPUB has no readable content documents: " + (res.error or "empty spine")
        else:
            res.status = "ok" if any(not c.text.startswith("[EPUB spine item") for c in res.chunks) else "empty"
        return res
