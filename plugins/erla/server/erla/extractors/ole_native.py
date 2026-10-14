"""Office-free text extraction for legacy binary .doc (Word 97-2003) and .ppt (PowerPoint 97-2003).

Pure parsing of the OLE compound file with `olefile`; no Office process, no macros, nothing executed
(G1.3/G1.4). Used first; MS Office COM is only a fallback when this yields no text.

Word: FIB -> Clx piece table in the 0Table/1Table stream -> text pieces in WordDocument
      (compressed cp1252 or UTF-16LE). Main-document text only (ccpText); field codes removed,
      field results kept.
PowerPoint: record walk of the 'PowerPoint Document' stream. Slide text from SlideListWithText
      (presentation order, so `slide <n>` locators are exact); text found only in slide drawings
      or notes is returned as one extra chunk with locator `other text` (slide not resolvable).
"""

from __future__ import annotations

import struct

import olefile


class NativeError(Exception):
    pass


def _meta_title(ole) -> tuple[str | None, str | None]:
    try:
        m = ole.get_metadata()
        dec = lambda b: (b.decode("cp1252", "ignore").strip() or None) if isinstance(b, bytes) else b  # noqa: E731
        return dec(m.title), dec(m.author)
    except Exception:
        return None, None


# ----------------------------------------------------------------------------- Word
def _strip_fields(s: str) -> str:
    out, depth, in_code = [], 0, []
    for ch in s:
        if ch == "\x13":
            depth += 1
            in_code.append(True)
        elif ch == "\x14" and depth:
            in_code[-1] = False
        elif ch == "\x15" and depth:
            depth -= 1
            in_code.pop()
        elif not (depth and in_code[-1]):
            out.append(ch)
    return "".join(out)


def doc_text(path: str) -> tuple[str, str | None, str | None]:
    """Return (main text, title, author) of a Word 97-2003 .doc."""
    if not olefile.isOleFile(path):
        raise NativeError("not an OLE compound file")
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("WordDocument"):
            raise NativeError("no WordDocument stream")
        wd = ole.openstream("WordDocument").read()
        if len(wd) < 0x1AA or struct.unpack_from("<H", wd, 0)[0] != 0xA5EC:
            raise NativeError("bad FIB")
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        if flags & 0x0100:
            raise NativeError("encrypted")
        tname = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(tname):
            raise NativeError(f"missing {tname}")
        tbl = ole.openstream(tname).read()
        ccp_text = struct.unpack_from("<i", wd, 0x4C)[0]
        fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x1A2)
        clx = tbl[fc_clx:fc_clx + lcb_clx]
        i = 0
        while i < len(clx) and clx[i] == 0x01:                 # skip Prc (property modifiers)
            i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
        if i >= len(clx) or clx[i] != 0x02:
            raise NativeError("no piece table")
        lcb = struct.unpack_from("<I", clx, i + 1)[0]
        plc = clx[i + 5:i + 5 + lcb]
        n = (lcb - 4) // 12
        cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
        parts, done = [], 0
        for k in range(n):
            if done >= ccp_text:
                break
            fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
            cnt = min(cps[k + 1] - cps[k], ccp_text - done)
            if fc & 0x40000000:
                off = (fc & ~0x40000000) // 2
                parts.append(wd[off:off + cnt].decode("cp1252", "replace"))
            else:
                parts.append(wd[fc:fc + 2 * cnt].decode("utf-16-le", "replace"))
            done += cnt
        title, author = _meta_title(ole)
    text = _strip_fields("".join(parts))
    return text, title, author


# ----------------------------------------------------------------------------- PowerPoint
_CONTAINER_VER = 0xF
_SLWT, _SLIDE_PERSIST = 0x0FF0, 0x03F3
_TEXT_CHARS, _TEXT_BYTES = 0x0FA0, 0x0FA8
_SLIDE, _NOTES = 0x03EE, 0x03F0


def _records(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        vi, rtype, rlen = struct.unpack_from("<HHI", buf, i)
        body = i + 8
        if body + rlen > end:
            return
        yield vi & 0xF, vi >> 4, rtype, body, body + rlen
        i = body + rlen


def ppt_text(path: str) -> tuple[list[tuple[str, str]], str | None]:
    """Return ([(locator, text)], title) of a PowerPoint 97-2003 .ppt (current revision only).

    [MS-PPT] resolution: CurrentUser stream -> offsetToCurrentEdit -> UserEditAtom chain
    (offsetLastEdit) -> PersistDirectoryAtoms (newest wins) -> DocumentContainer (docPersistIdRef)
    -> SlideListWithText instance 0 gives slide order; each SlidePersistAtom.persistIdRef resolves
    to that slide's container. Older revisions left by "fast save" are ignored. Notes (SLWT
    instance 2) go to one chunk `other text`.
    """
    if not olefile.isOleFile(path):
        raise NativeError("not an OLE compound file")
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("PowerPoint Document"):
            raise NativeError("no PowerPoint Document stream")
        buf = ole.openstream("PowerPoint Document").read()
        cu = ole.openstream("Current User").read() if ole.exists("Current User") else b""
        title, _a = _meta_title(ole)

    def hdr(off):
        if off < 0 or off + 8 > len(buf):
            raise NativeError(f"bad record offset {off}")
        vi, rt, rl = struct.unpack_from("<HHI", buf, off)
        return vi & 0xF, vi >> 4, rt, off + 8, min(off + 8 + rl, len(buf))

    def atom(rtype, b, e):
        if rtype == _TEXT_CHARS:
            return buf[b:e].decode("utf-16-le", "replace")
        if rtype == _TEXT_BYTES:
            return buf[b:e].decode("cp1252", "replace")
        return None

    def texts(b, e):
        out = []
        for ver, _i, rtype, rb, re_ in _records(buf, b, e):
            if ver == _CONTAINER_VER:
                out += texts(rb, re_)
            else:
                t = atom(rtype, rb, re_)
                if t and t.strip():
                    out.append(t)
        return out

    # --- persist directory of the current revision
    if len(cu) < 20:
        raise NativeError("no CurrentUser atom")
    edit_off = struct.unpack_from("<I", cu, 16)[0]
    edits, seen = [], set()
    while edit_off and edit_off not in seen:
        seen.add(edit_off)
        _v, _i, rt, b, e = hdr(edit_off)
        if rt != 0x0FF5 or e - b < 20:
            raise NativeError("bad UserEditAtom")
        last, pdir, docref = struct.unpack_from("<III", buf, b + 8)
        edits.append((pdir, docref))
        edit_off = last
    if not edits:
        raise NativeError("no UserEditAtom")
    persist: dict[int, int] = {}
    for pdir, _d in reversed(edits):                   # oldest first, newest overrides
        _v, _i, rt, b, e = hdr(pdir)
        i = b
        while i + 4 <= e:
            h = struct.unpack_from("<I", buf, i)[0]
            pid, n = h & 0xFFFFF, h >> 20
            for k in range(n):
                if i + 8 + 4 * k <= e:
                    persist[pid + k] = struct.unpack_from("<I", buf, i + 4 + 4 * k)[0]
            i += 4 + 4 * n
    doc_off = persist.get(edits[0][1])
    if doc_off is None:
        raise NativeError("document persist id unresolved")
    _v, _i, rt, db, de = hdr(doc_off)
    if rt != 0x03E8:
        raise NativeError("DocumentContainer not found")

    slides, notes = [], []
    for ver, inst, rtype, rb, re_ in _records(buf, db, de):
        if not (ver == _CONTAINER_VER and rtype == _SLWT and inst in (0, 2)):
            continue
        cur = None
        for _v2, _in, rt2, ab, ae in _records(buf, rb, re_):
            if rt2 == _SLIDE_PERSIST:
                cur = [struct.unpack_from("<I", buf, ab)[0], []]
                (slides if inst == 0 else notes).append(cur)
            elif cur is not None:
                t = atom(rt2, ab, ae)
                if t and t.strip():
                    cur[1].append(t)
    out = []
    for n, (ref, parts) in enumerate(slides, 1):
        off = persist.get(ref)
        if off is not None:
            _v, _i, rt, b, e = hdr(off)
            if rt == _SLIDE:
                parts += [t for t in texts(b, e) if t not in parts]
        if parts:
            out.append((f"slide {n}", "\n".join(parts).replace("\r", "\n")))
    other = []
    for ref, parts in notes:
        off = persist.get(ref)
        if off is not None:
            _v, _i, rt, b, e = hdr(off)
            if rt == _NOTES:
                parts += [t for t in texts(b, e) if t not in parts]
        other += ["NOTES: " + t for t in parts]
    if other:
        out.append(("other text", "\n".join(other).replace("\r", "\n")))
    return out, title
