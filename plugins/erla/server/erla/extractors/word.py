"""Word (.docx) extraction that reads the document as Word shows it.

python-docx's ``paragraph.text`` sees only plain runs. On engineering documents that loses values:
text inside tracked insertions, content controls and equations disappears, a superscript exponent
joins its base ("E, 10<sup>3</sup> ksi" reads "E, 103 ksi"), and Symbol-font characters read as Latin
letters ("25 μm" typed in Symbol reads "25 mm"). This reader walks the WordprocessingML itself:

- tracked changes: insertions kept, deletions dropped (the revised text), chunk flagged
- superscript numbers after a base: ``^`` ("10^3", "in^2", "10^-6"); a superscript o after a number: °
- Symbol font (``w:sym`` and runs in the Symbol font) mapped to Unicode; Wingdings check boxes mapped;
  any other symbol-font character flagged, never guessed
- OMML equations linearised ("σ = (P)/(A)"); old Equation Editor / MathType objects flagged (not text)
- content controls, hyperlinks, smart tags, fields' shown result, text boxes (once), legacy check boxes
- automatic numbering of headings and numbered paragraphs ("3.2 Material Properties")
- each table with the caption next to it, nested tables, footnotes, endnotes, headers and footers

Read only; no macros or fields are executed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]  (python-docx's own XML library)

from .base import Chunk, Extractor, ExtractResult, clean, rows_to_markdown, split_long

VERSION = "docx-2"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
R_FOOTNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
R_ENDNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
R_HEADER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
R_FOOTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _m(tag: str) -> str:
    return f"{{{M_NS}}}{tag}"


def _wval(el: Any, child: str, attr: str = "val") -> str | None:
    c = el.find(_w(child)) if el is not None else None
    return None if c is None else c.get(_w(attr))


# Adobe Symbol encoding, 0x20-0xFE -> Unicode. Bracket pieces map to the nearest whole symbol.
_SYMBOL = dict(zip(range(0x20, 0x7F),
                   " !∀#∃%&∋()∗+,−./0123456789:;<=>?≅ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_‾αβχδεφγηιϕκλμνοπθρστυϖωξψζ{|}∼"))
_SYMBOL.update(zip(range(0xA0, 0xFF),
                   "€ϒ′≤⁄∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…|―↵ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇®©™∏√⋅¬∧∨⇔⇐⇑⇒⇓◊〈®©™∑"
                   "(||[|[{{{|￿〉∫⌠|⌡)||]|]}}}"))
del _SYMBOL[0xF0]   # unused slot in the Symbol font
# Wingdings: only the check boxes and marks that carry meaning in compliance matrices and checklists
_WINGDINGS = {0x6C: "●", 0x6E: "■", 0x6F: "□", 0xA8: "☐", 0xFB: "✗", 0xFC: "✓", 0xFD: "☒", 0xFE: "☑",
              **{0x80 + i: c for i, c in enumerate("⓪①②③④⑤⑥⑦⑧⑨⑩")}}     # circled step numbers
# exact font names ("Wingdings 2" differs). SymbolProp BT / SymbolMono BT: Bitstream's Symbol-compatible
# fonts (WordPerfect); on the site's files they carry μ, ε, τ at Symbol's codes.
_SYMBOL_FONTS = {"symbol": _SYMBOL, "symbolprop bt": _SYMBOL, "symbolmono bt": _SYMBOL, "wingdings": _WINGDINGS}
_DINGBAT_FONTS = ("symbol", "wingdings", "webdings", "zapf", "dingbat", "mt extra", "marlett")

_NUMBER_TAIL = re.compile(r"[\d.)\]]$")
_EXPONENT = re.compile(r"[-−–+]?\d{1,3}")
CAPTION_RE = re.compile(r"^\s*(?:Table|TABLE|Tab\.)\s*[A-Z]?[\dIVX]+(?:[.\-–]\d+)*\b")
OLE_EQUATION = re.compile(r"^(?:Equation\.|MathType)", re.I)


# ------------------------------------------------------------------ numbering
def _roman(n: int) -> str:
    out = ""
    for v, s in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                 (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out, n = out + s, n - v
    return out


def _letters(n: int) -> str:
    return chr(96 + (n - 1) % 26 + 1) * ((n - 1) // 26 + 1) if n > 0 else ""


def _fmt(n: int, fmt: str) -> str:
    if fmt in ("lowerLetter", "upperLetter"):
        s = _letters(n)
        return s.upper() if fmt == "upperLetter" else s
    if fmt in ("lowerRoman", "upperRoman"):
        s = _roman(n)
        return s.upper() if fmt == "upperRoman" else s
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt == "none":
        return ""
    return str(n)


class _Numbering:
    """Word's automatic list numbering, enough for headings and numbered clauses ("3.2.1", "(a)")."""

    def __init__(self, root: Any):
        self.levels: dict[str, dict[int, dict[str, Any]]] = {}    # numId -> ilvl -> {fmt, text, start}
        # Word continues one sequence across list instances of the same abstract list; an instance
        # with a start override restarts it at its first use
        self.abstract_of: dict[str, str] = {}
        self.restarts: dict[str, list[int]] = {}
        self.counters: dict[str, list[int]] = {}
        if root is None:
            return
        abstract: dict[str, dict[int, dict[str, Any]]] = {}
        for an in root.findall(_w("abstractNum")):
            abstract[an.get(_w("abstractNumId")) or ""] = {
                int(lvl.get(_w("ilvl")) or 0): self._level(lvl) for lvl in an.findall(_w("lvl"))}
        for num in root.findall(_w("num")):
            num_id = num.get(_w("numId")) or ""
            aid = _wval(num, "abstractNumId") or ""
            lv = {k: dict(v) for k, v in abstract.get(aid, {}).items()}
            for ov in num.findall(_w("lvlOverride")):
                i = int(ov.get(_w("ilvl")) or 0)
                lvl = ov.find(_w("lvl"))
                if lvl is not None:
                    lv[i] = self._level(lvl)
                start = _wval(ov, "startOverride")
                if start is not None and i in lv and start.lstrip("-").isdigit():
                    lv[i]["start"] = int(start)
                    self.restarts.setdefault(num_id, []).append(i)
            self.levels[num_id] = lv
            self.abstract_of[num_id] = aid

    @staticmethod
    def _level(lvl: Any) -> dict[str, Any]:
        start = _wval(lvl, "start")
        return {"fmt": _wval(lvl, "numFmt") or "decimal", "text": _wval(lvl, "lvlText") or "",
                "start": int(start) if start and start.lstrip("-").isdigit() else 1,
                "legal": lvl.find(_w("isLgl")) is not None}

    def label(self, num_id: str, ilvl: int) -> str:
        lv = self.levels.get(num_id)
        if not lv or ilvl not in lv:
            return ""
        me = lv[ilvl]
        if me["fmt"] == "bullet":
            return ""
        cnt = self.counters.setdefault("a" + self.abstract_of.get(num_id, num_id), [0] * 9)
        for i in self.restarts.pop(num_id, []):
            cnt[i:] = [0] * (9 - i)
        for i in range(ilvl):                       # a level that never ran shows its start value
            if cnt[i] == 0 and i in lv:
                cnt[i] = lv[i]["start"]
        cnt[ilvl] = cnt[ilvl] + 1 if cnt[ilvl] else me["start"]
        for i in range(ilvl + 1, 9):
            cnt[i] = 0

        def sub(m: re.Match[str]) -> str:
            i = int(m.group(1)) - 1
            fmt = "decimal" if me["legal"] else lv.get(i, {}).get("fmt", "decimal")
            return _fmt(cnt[i], fmt)
        return re.sub(r"%([1-9])", sub, me["text"]).strip()


# ------------------------------------------------------------------- reader
class _Doc:
    """Walks one WordprocessingML tree. Collects prose by heading section, tables and quality flags."""

    def __init__(self, styles: Any, numbering: Any):
        self.styles: dict[str, dict[str, Any]] = {}
        if styles is not None:
            raw: dict[str, dict[str, Any]] = {}
            for st in styles.findall(_w("style")):
                ppr = st.find(_w("pPr"))
                npr = ppr.find(_w("numPr")) if ppr is not None else None
                raw[st.get(_w("styleId")) or ""] = {
                    "name": (_wval(st, "name") or "").lower(), "based": _wval(st, "basedOn"),
                    "outline": _wval(ppr, "outlineLvl"),
                    "num": (_wval(npr, "numId"), _wval(npr, "ilvl")) if npr is not None else None}
            for sid in raw:
                self.styles[sid] = self._resolve(raw, sid)
        self.numbering = _Numbering(numbering)
        self.flags: dict[str, Any] = {}
        self._pending_boxes: list[str] = []

    @staticmethod
    def _resolve(raw: dict[str, dict[str, Any]], sid: str) -> dict[str, Any]:
        out = {"name": raw[sid]["name"], "outline": None, "num": None}
        seen: set[str] = set()
        cur: str | None = sid
        while cur and cur in raw and cur not in seen:
            seen.add(cur)
            if out["outline"] is None and raw[cur]["outline"] is not None:
                out["outline"] = raw[cur]["outline"]
            if out["num"] is None and raw[cur]["num"] is not None:
                out["num"] = raw[cur]["num"]
            cur = raw[cur]["based"]
        return out

    # ---------------------------------------------------------- flags
    def _flag(self, key: str, value: Any = True) -> None:
        if key in ("unmapped_symbols",):
            lst = self.flags.setdefault(key, [])
            if value not in lst and len(lst) < 20:
                lst.append(value)
        elif key == "ole_equations":
            self.flags[key] = self.flags.get(key, 0) + 1
        else:
            self.flags[key] = value

    def take_flags(self) -> dict[str, Any]:
        f, self.flags = self.flags, {}
        return f

    # ---------------------------------------------------------- inline text
    def _symbol(self, font: str, code: int) -> str:
        if 0xF000 <= code <= 0xF0FF:
            code -= 0xF000
        table = _SYMBOL_FONTS.get(font.strip().lower())
        ch = table.get(code) if table else None
        if ch and ch != "￿":
            return ch
        self._flag("unmapped_symbols", f"{font} 0x{code:02X}")
        return "�"

    def _run(self, r: Any, out: list[str]) -> None:
        rpr = r.find(_w("rPr"))
        valign = _wval(rpr, "vertAlign")
        fonts = rpr.find(_w("rFonts")) if rpr is not None else None
        font = ""
        if fonts is not None:
            font = fonts.get(_w("ascii")) or fonts.get(_w("hAnsi")) or ""
        sym_font = font.strip().lower().startswith(_DINGBAT_FONTS)
        parts: list[str] = []
        for c in r:
            tag = etree.QName(c).localname if isinstance(c.tag, str) else ""
            if tag == "t":
                txt = c.text or ""
                if sym_font:   # 8-bit / F0xx codes in a symbol font; real Unicode text passes through
                    txt = "".join(self._symbol(font, ord(ch)) if ch != " " and (ord(ch) < 0x100 or
                                  0xF020 <= ord(ch) <= 0xF0FF) else ch for ch in txt)
                parts.append(txt)
            elif tag == "tab" or tag == "ptab":
                parts.append("\t")
            elif tag in ("br", "cr"):
                parts.append("\n")
            elif tag == "noBreakHyphen":
                parts.append("-")
            elif tag == "sym":
                try:
                    parts.append(self._symbol(c.get(_w("font")) or "", int(c.get(_w("char")) or "0", 16)))
                except ValueError:
                    self._flag("unmapped_symbols", f"{c.get(_w('font'))} {c.get(_w('char'))}")
            elif tag == "footnoteReference":
                parts.append(f"[{c.get(_w('id'))}]")
            elif tag == "endnoteReference":
                parts.append(f"[e{c.get(_w('id'))}]")
            elif tag == "fldChar" and c.get(_w("fldCharType")) == "begin":
                box = c.find(f"{_w('ffData')}/{_w('checkBox')}")
                if box is not None:
                    checked = box.find(_w("checked"))
                    on = ((checked.get(_w("val")) or "1") if checked is not None
                          else _wval(box, "default") or "0")
                    parts.append("☒" if on in ("1", "true", "on") else "☐")
            elif tag == "object":
                ole = c.find(".//{urn:schemas-microsoft-com:office:office}OLEObject")
                if ole is not None and OLE_EQUATION.match(ole.get("ProgID") or ""):
                    self._flag("ole_equations")
                    parts.append("[equation object]")
            elif tag in ("drawing", "pict", "AlternateContent"):
                self._boxes(c)
        text = "".join(parts)
        if valign == "superscript" and text:
            prev = "".join(out)
            s = text.strip()
            if prev and not prev[-1].isspace() and _EXPONENT.fullmatch(s):
                text = "^" + s.replace("−", "-").replace("–", "-")
            elif s in ("o", "O") and prev and _NUMBER_TAIL.search(prev.rstrip()):
                text = "°"
        out.append(text)

    def _boxes(self, el: Any) -> None:
        """Text boxes inside a drawing: read once (the Choice branch, never also its Fallback copy)."""
        has_choice = el.find(f".//{{{MC_NS}}}Choice") is not None
        for box in el.iter(_w("txbxContent")):
            if _owner_box(box) is not None:
                continue                        # a box inside a box: read with its outer box
            if has_choice and any(a.tag == f"{{{MC_NS}}}Fallback" for a in box.iterancestors()):
                continue
            for p in _own_paras(box):
                t = self.para_text(p)
                if t.strip():
                    self._pending_boxes.append(t)

    def _inline(self, el: Any, out: list[str]) -> None:
        for c in el:
            if not isinstance(c.tag, str):
                continue
            q = etree.QName(c)
            if q.namespace == M_NS:
                if q.localname in ("oMath", "oMathPara"):
                    self._flag("equations")
                    out.append(" " + _omml(c).strip() + " ")
                continue
            tag = q.localname
            if tag == "r":
                self._run(c, out)
            elif tag in ("del", "moveFrom"):
                self._flag("tracked_changes")
            elif tag in ("ins", "moveTo"):
                self._flag("tracked_changes")
                self._inline(c, out)
            elif tag == "sdt":
                content = c.find(_w("sdtContent"))
                if content is not None:
                    self._inline(content, out)
            elif tag in ("hyperlink", "smartTag", "customXml", "fldSimple", "bdo", "dir", "sdtContent"):
                self._inline(c, out)

    def para_text(self, p: Any) -> str:
        saved, self._pending_boxes = self._pending_boxes, []
        out: list[str] = []
        self._inline(p, out)
        text = "".join(out)
        if self._pending_boxes:
            text = "\n".join([text, *self._pending_boxes]) if text.strip() else "\n".join(self._pending_boxes)
        self._pending_boxes = saved
        return re.sub(r"[ \t]+", " ", text).strip()

    # ---------------------------------------------------------- paragraph properties
    def para_info(self, p: Any) -> tuple[int | None, str, str]:
        """(heading level or None, numbering label, style name) of a paragraph."""
        ppr = p.find(_w("pPr"))
        sid = _wval(ppr, "pStyle") or ""
        st = self.styles.get(sid, {"name": "", "outline": None, "num": None})
        name = st["name"] or sid.lower()
        level: int | None = None
        outline = _wval(ppr, "outlineLvl") or st["outline"]
        m = re.fullmatch(r"heading\s*([1-9])", name)   # built-in names only: "Heading 1 Text" is body text
        if m:
            level = int(m.group(1))
        elif outline is not None and outline.isdigit() and int(outline) < 9:
            level = int(outline) + 1
        num_id, ilvl = st["num"] or (None, None)
        npr = ppr.find(_w("numPr")) if ppr is not None else None
        if npr is not None:
            num_id = _wval(npr, "numId") or num_id
            ilvl = _wval(npr, "ilvl") or ilvl
        label = ""
        if num_id and num_id != "0":
            label = self.numbering.label(num_id, int(ilvl or 0))
        return level, label, name


def _omml(el: Any) -> str:
    """Linear text of an Office Math element: fractions (a)/(b), powers a^b, subscripts a_b, roots."""
    def kids(e: Any, tag: str) -> Any:
        return e.find(_m(tag))

    def lin(e: Any) -> str:
        if e is None:
            return ""
        return "".join(node(c) for c in e if isinstance(c.tag, str))

    def wrap(s: str) -> str:
        s = s.strip()
        return s if re.fullmatch(r"[\w.]+", s) else f"({s})"

    def chr_of(e: Any, pr: str, default: str) -> str:
        p = e.find(_m(pr))
        c = p.find(_m("chr")) if p is not None else None
        return default if c is None else (c.get(_m("val")) or "")

    def node(e: Any) -> str:
        q = etree.QName(e)
        if q.namespace != M_NS:
            return ""
        t = q.localname
        if t == "r":
            return "".join(x.text or "" for x in e.iter(_m("t")))
        if t.endswith("Pr"):
            return ""
        if t == "f":
            return f"{wrap(lin(kids(e, 'num')))}/{wrap(lin(kids(e, 'den')))}"
        if t == "sSup":
            return f"{lin(kids(e, 'e'))}^{wrap(lin(kids(e, 'sup')))}"
        if t == "sSub":
            return f"{lin(kids(e, 'e'))}_{wrap(lin(kids(e, 'sub')))}"
        if t == "sSubSup":
            return f"{lin(kids(e, 'e'))}_{wrap(lin(kids(e, 'sub')))}^{wrap(lin(kids(e, 'sup')))}"
        if t == "sPre":
            return f"_{wrap(lin(kids(e, 'sub')))}^{wrap(lin(kids(e, 'sup')))}{lin(kids(e, 'e'))}"
        if t == "rad":
            deg = lin(kids(e, "deg")).strip()
            return (f"root({deg})" if deg else "sqrt") + f"({lin(kids(e, 'e'))})"
        if t == "d":
            p = e.find(_m("dPr"))
            ec = p.find(_m("endChr")) if p is not None else None
            bc = p.find(_m("begChr")) if p is not None else None
            sc = p.find(_m("sepChr")) if p is not None else None
            beg = "(" if bc is None else (bc.get(_m("val")) or "")
            end = ")" if ec is None else (ec.get(_m("val")) or "")
            sep = "|" if sc is None else (sc.get(_m("val")) or "")
            return beg + sep.join(lin(x) for x in e.findall(_m("e"))) + end
        if t == "nary":
            op = chr_of(e, "naryPr", "∫")
            lo, hi = lin(kids(e, "sub")).strip(), lin(kids(e, "sup")).strip()
            return op + (f"_{wrap(lo)}" if lo else "") + (f"^{wrap(hi)}" if hi else "") + " " + lin(kids(e, "e"))
        if t == "func":
            return f"{lin(kids(e, 'fName'))} {lin(kids(e, 'e'))}"
        if t == "acc":
            return lin(kids(e, "e")) + chr_of(e, "accPr", "̂")
        if t in ("limLow", "limUpp"):
            return f"{lin(kids(e, 'e'))}{'_' if t == 'limLow' else '^'}{wrap(lin(kids(e, 'lim')))}"
        if t == "eqArr":
            return "; ".join(lin(x) for x in e.findall(_m("e")))
        if t == "m":
            return "[" + "; ".join(", ".join(lin(c) for c in row.findall(_m("e")))
                                   for row in e.findall(_m("mr"))) + "]"
        return lin(e)

    return re.sub(r"\s+", " ", node(el) if etree.QName(el).localname == "oMath"
                  else " ".join(node(x) for x in el.iter(_m("oMath")))).strip()


def _quality(flags: dict[str, Any]) -> dict[str, Any] | None:
    keep = {k: v for k, v in flags.items() if k in ("tracked_changes", "unmapped_symbols", "ole_equations")}
    return {"docx": keep} if keep else None


def _merge(into: dict[str, Any], flags: dict[str, Any]) -> None:
    for k, v in flags.items():
        if k == "unmapped_symbols":
            lst = into.setdefault(k, [])
            lst.extend(x for x in v if x not in lst)
        elif k == "ole_equations":
            into[k] = into.get(k, 0) + v
        else:
            into[k] = v


def _part_xml(doc: Any, reltype: str) -> list[Any]:
    out = []
    for rel in doc.part.rels.values():
        if rel.reltype == reltype and not rel.is_external:
            out.append(etree.fromstring(rel.target_part.blob, _PARSER))
    return out


_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)




def _owner_box(el: Any) -> Any:
    """The nearest text box holding ``el``, or None."""
    for a in el.iterancestors():
        if a.tag == _w("txbxContent"):
            return a
    return None


def _own_paras(container: Any) -> list[Any]:
    """Paragraphs of ``container`` that are not inside a text box of their own (those are read with
    the paragraph that anchors the box)."""
    box = container if container.tag == _w("txbxContent") else None
    return [p for p in container.iter(_w("p")) if _owner_box(p) is box]


def _blocks(parent: Any) -> list[Any]:
    """Paragraphs and tables of a body or cell in document order, content controls unwrapped."""
    out: list[Any] = []
    for el in parent:
        if el.tag in (_w("p"), _w("tbl")):
            out.append(el)
        elif el.tag == _w("sdt"):
            content = el.find(_w("sdtContent"))
            if content is not None:
                out += _blocks(content)
        elif el.tag == _w("customXml"):
            out += _blocks(el)
    return out


class DocxExtractor(Extractor):
    exts = ("docx",)
    version = VERSION

    def extract(self, path: Path) -> ExtractResult:
        import docx
        from docx.table import Table

        try:
            d = docx.Document(str(path))
        except Exception as exc:
            return ExtractResult(status="failed", error=f"open: {exc}")
        res = ExtractResult()
        cp = d.core_properties
        res.meta = {
            "title": (cp.title or "").strip() or None,
            "authors": (cp.author or "").strip() or None,
            "year": cp.created.year if cp.created else None,
        }
        try:
            styles = d.styles.element
        except Exception:
            styles = None
        try:
            numbering = d.part.numbering_part.element
        except Exception:      # no numbering part
            numbering = None
        w = _Doc(styles, numbering)

        def cell_text(tc: Any, nested: list[Any]) -> str:
            parts: list[str] = []
            for el in _blocks(tc):
                if el.tag == _w("tbl"):
                    nested.append(el)
                    continue
                _lvl, label, _name = w.para_info(el)
                parts.append(" ".join(x for x in (label, w.para_text(el)) if x))
            return " ".join(x for x in parts if x)

        def read_table(tbl: Any, loc: str) -> list[tuple[str, str, dict[str, Any]]]:
            """(locator, markdown, flags) of a table and the tables nested in it."""
            nested: list[Any] = []
            # each cell read once, in document order (numbering counts); merged cells repeat its text
            texts = {tc: cell_text(tc, nested) for tr in tbl.findall(_w("tr")) for tc in tr.findall(_w("tc"))}
            try:
                rows = [[texts.get(c._tc, "") for c in row.cells] for row in Table(tbl, d._body).rows]
            except Exception:
                rows = []
            md = rows_to_markdown(rows)
            out = [(loc, md, w.take_flags())] if md else []
            w.take_flags()
            for k, n in enumerate(nested, 1):
                out += read_table(n, f"{loc}.{k}")
            return out

        # one pass in document order: Word numbers headings, clauses and captions as it goes
        items: list[tuple[Any, ...]] = []
        n_tables = 0
        for el in _blocks(d.element.body):
            if el.tag == _w("tbl"):
                n_tables += 1
                items.append(("t", read_table(el, f"table {n_tables}")))
            else:
                level, label, _name = w.para_info(el)
                text = w.para_text(el)
                items.append(("p", level, f"{label} {text}" if label and text else text, w.take_flags()))

        heading_path: list[str] = []
        pending: list[tuple[int, str]] = []    # headings not yet followed by body text
        buf: list[str] = []
        buf_flags: dict[str, Any] = {}
        doc_flags: dict[str, Any] = {}
        tables: list[Chunk] = []

        def flush() -> None:
            text = clean("\n".join(buf))
            if text:
                loc = "§" + (" > ".join(heading_path) if heading_path else "body")
                q = _quality(buf_flags)
                for j, piece in enumerate(split_long(text)):
                    res.chunks.append(Chunk(loc if j == 0 else f"{loc} part {j + 1}", piece, quality=q))
            _merge(doc_flags, buf_flags)
            buf.clear()
            buf_flags.clear()

        def emit_headings() -> None:
            """Headings with no body text under them: their own chunk, so their words stay searchable."""
            if pending:
                loc = "§" + " > ".join(heading_path)
                res.chunks.append(Chunk(loc, clean("\n".join(t for _l, t in pending))))
                pending.clear()

        def near(i: int) -> str:
            return items[i][2] if 0 <= i < len(items) and items[i][0] == "p" else ""

        for i, it in enumerate(items):
            if it[0] == "t":
                prev, nxt = near(i - 1), near(i + 1)
                cap = prev if CAPTION_RE.match(prev) else (nxt if CAPTION_RE.match(nxt) else "")
                for j, (loc, md, fl) in enumerate(it[1]):
                    _merge(doc_flags, fl)
                    text = f"{cap[:300]}\n{md}" if cap and j == 0 else md
                    tables.append(Chunk(loc, text, has_table=True, quality=_quality(fl)))
                continue
            _p, level, text, fl = it
            if level is not None and text:
                flush()
                if any(lv >= level for lv, _t in pending):
                    emit_headings()             # a sibling or parent heading follows: that section had no body
                heading_path[:] = heading_path[: level - 1] + [text[:80]]
                pending.append((level, text))
            elif text:
                buf.extend(t for _l, t in pending)   # the headings open the chunk of the body beneath them
                pending.clear()
                buf.append(text)
            _merge(buf_flags, fl)
        flush()
        emit_headings()
        res.chunks += tables

        for reltype, loc, prefix in ((R_FOOTNOTES, "footnotes", ""), (R_ENDNOTES, "endnotes", "e")):
            lines: list[str] = []
            for root in _part_xml(d, reltype):
                for note in root:
                    if (note.get(_w("type")) or "normal") != "normal":
                        continue        # separators
                    t = " ".join(x for x in (w.para_text(p) for p in _own_paras(note)) if x)
                    if t:
                        lines.append(f"[{prefix}{note.get(_w('id'))}] {t}")
            fl = w.take_flags()
            _merge(doc_flags, fl)
            text = clean("\n".join(lines))
            for j, piece in enumerate(split_long(text) if text else []):
                res.chunks.append(Chunk(loc if j == 0 else f"{loc} part {j + 1}", piece, quality=_quality(fl)))
        hf: list[str] = []
        for reltype in (R_HEADER, R_FOOTER):   # document numbers, revisions and markings live here
            for root in _part_xml(d, reltype):
                t = "\n".join(x for x in (w.para_text(p) for p in _own_paras(root)) if x)
                if t and t not in hf:
                    hf.append(t)
        w.take_flags()
        if hf:
            res.chunks.append(Chunk("headers and footers", clean("\n".join(hf))))

        if doc_flags.get("tracked_changes"):
            res.meta["tracked_changes"] = True
        if not res.meta["title"]:
            res.meta["title_guess"] = next((it[2][:200] for it in items if it[0] == "p" and it[2]), None)
        res.status = "ok" if res.chunks else "empty"
        return res
