"""Legacy Word (.doc) and PowerPoint (.ppt) extraction through MS Office COM.

Safety (guardrails G1.3/G1.4):
- Office runs in a dedicated child process with its own Office instance
  (DispatchEx), invisible, alerts off, macros force-disabled
  (AutomationSecurity = msoAutomationSecurityForceDisable).
- Files are opened read-only, never saved, links not updated, a dummy password
  is supplied so protected files fail instead of prompting.
- A .doc is read by saving a .docx copy into ``logs/doc_copy/`` (a G1.2 location checked by safeio;
  never next to the original, G1.1), deleted after reading, and reading that copy with the Word reader (word.py), so tracked changes,
  exponents, Symbol-font characters, equations, numbering and tables survive. The Office-free parser
  (ole_native) is the fallback.
- Each file has a timeout; on hang the worker and its Office process are killed.
"""

from __future__ import annotations

import multiprocessing as mp
import subprocess
import time
from pathlib import Path

from .base import Chunk, Extractor, ExtractResult, clean

FORCE_DISABLE_MACROS = 3
DUMMY_PASSWORD = "erla-no-password"
WD_FORMAT_DOCX = 16          # wdFormatDocumentDefault: macro-free .docx
PARA_WINDOW_CHARS = 6000


def _office_pids(image: str) -> set[int]:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


# --------------------------------------------------------------- child side
def _word_extract(app, path: str) -> dict:
    doc = app.Documents.Open(FileName=path, ConfirmConversions=False, ReadOnly=True,
                             AddToRecentFiles=False, PasswordDocument=DUMMY_PASSWORD,
                             Revert=False, Visible=False, NoEncodingDialog=True)
    try:
        props = {}
        for key in ("Title", "Author"):
            try:
                props[key] = str(doc.BuiltInDocumentProperties(key).Value or "").strip() or None
            except Exception:
                props[key] = None
        text = doc.Content.Text or ""
    finally:
        doc.Close(SaveChanges=0)
    return {"chunks": para_windows(text), "meta": {"title": props.get("Title"), "authors": props.get("Author")}}


def _word_to_docx(app, path: str, out: str) -> dict:
    """Save a .docx copy of ``path`` at ``out`` (a temporary folder). The original is opened read-only
    and closed unsaved; the copy is macro-free."""
    doc = app.Documents.Open(FileName=path, ConfirmConversions=False, ReadOnly=True,
                             AddToRecentFiles=False, PasswordDocument=DUMMY_PASSWORD,
                             Revert=False, Visible=False, NoEncodingDialog=True)
    try:
        doc.SaveAs2(FileName=out, FileFormat=WD_FORMAT_DOCX, AddToRecentFiles=False)
    finally:
        doc.Close(SaveChanges=0)
    return {"docx": out}


def para_windows(text: str) -> list[tuple[str, str]]:
    paras = [p.strip() for p in text.replace("\x07", " ").replace("\x0c", "\r").replace("\x0b", "\r").split("\r")]
    chunks, buf, start = [], [], 1
    size = 0
    for i, p in enumerate(paras, 1):
        if not p:
            continue
        if size + len(p) > PARA_WINDOW_CHARS and buf:
            chunks.append((f"para {start}-{i - 1}", "\n".join(buf)))
            buf, size, start = [], 0, i
        if not buf:
            start = i
        buf.append(p)
        size += len(p) + 1
    if buf:
        chunks.append((f"para {start}-{len(paras)}", "\n".join(buf)))
    return chunks


def _ppt_extract(app, path: str) -> dict:
    pres = app.Presentations.Open(path, True, False, False)  # ReadOnly, Untitled, WithWindow
    chunks = []
    try:
        title = None
        try:
            title = str(pres.BuiltInDocumentProperties("Title").Value or "").strip() or None
        except Exception:
            pass
        for i in range(1, pres.Slides.Count + 1):
            slide = pres.Slides(i)
            parts = []
            for j in range(1, slide.Shapes.Count + 1):
                shp = slide.Shapes(j)
                try:
                    if shp.HasTextFrame and shp.TextFrame.HasText:
                        parts.append(shp.TextFrame.TextRange.Text)
                    if shp.HasTable:
                        tbl = shp.Table
                        for r in range(1, tbl.Rows.Count + 1):
                            cells = [tbl.Cell(r, c).Shape.TextFrame.TextRange.Text
                                     for c in range(1, tbl.Columns.Count + 1)]
                            parts.append(" | ".join(cells))
                except Exception:
                    continue
            try:
                notes = slide.NotesPage.Shapes.Placeholders(2).TextFrame.TextRange.Text
                if notes and notes.strip():
                    parts.append("NOTES: " + notes)
            except Exception:
                pass
            if parts:
                chunks.append((f"slide {i}", "\n".join(parts)))
    finally:
        pres.Close()
    return {"chunks": chunks, "meta": {"title": title, "slide_count": len(chunks)}}


def _worker(kind: str, conn) -> None:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    image = "WINWORD.EXE" if kind == "word" else "POWERPNT.EXE"
    before = _office_pids(image)
    if kind == "word":
        app = win32com.client.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
    else:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        try:
            app.DisplayAlerts = 1  # ppAlertsNone
        except Exception:
            pass
    try:
        app.AutomationSecurity = FORCE_DISABLE_MACROS
    except Exception:
        pass
    time.sleep(0.5)
    conn.send(("pids", sorted(_office_pids(image) - before)))
    try:
        while True:
            msg = conn.recv()
            if msg is None:
                break
            try:
                if isinstance(msg, tuple) and kind == "word":       # ("docx", source, temporary copy)
                    data = _word_to_docx(app, msg[1], msg[2])
                else:
                    data = _word_extract(app, str(msg)) if kind == "word" else _ppt_extract(app, str(msg))
                conn.send(("ok", data))
            except Exception as exc:
                conn.send(("err", str(exc)[:500]))
    finally:
        try:
            app.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()


# -------------------------------------------------------------- parent side
class ComWorker:
    """Long-lived Office worker with per-file timeout and hard kill on hang."""

    def __init__(self, kind: str, timeout_s: float = 120):
        self.kind, self.timeout_s = kind, timeout_s
        self.proc = None
        self.conn = None
        self.office_pids: list[int] = []

    def _start(self) -> None:
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        self.proc = ctx.Process(target=_worker, args=(self.kind, child), daemon=True)
        self.proc.start()
        self.conn = parent
        if not parent.poll(90):
            self.kill()
            raise RuntimeError(f"{self.kind} COM worker failed to start")
        tag, pids = parent.recv()
        self.office_pids = pids

    def kill(self) -> None:
        for pid in self.office_pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        if self.proc is not None and self.proc.is_alive():
            self.proc.kill()
        self.proc, self.conn, self.office_pids = None, None, []

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.send(None)
                self.proc.join(30)
            except Exception:
                pass
        self.kill()

    def run(self, path: Path | tuple[str, str, str]) -> tuple[str, object]:
        """Extract the text of ``path``, or for ``("docx", source, out)`` save a .docx copy at ``out``."""
        if self.proc is None or not self.proc.is_alive():
            self._start()
        self.conn.send(path if isinstance(path, tuple) else str(path))
        if not self.conn.poll(self.timeout_s):
            self.kill()
            return "err", f"timeout after {self.timeout_s}s"
        try:
            return self.conn.recv()
        except EOFError:
            self.kill()
            return "err", "worker died"


class _ComExtractor(Extractor):
    kind = "word"
    needs_com = True

    def __init__(self, cfg=None, worker: ComWorker | None = None):
        super().__init__(cfg)
        timeout = float((cfg.legacy_office.get("per_file_timeout_s", 120)) if cfg else 120)
        # worker=False -> native parser only (COM disabled for this call)
        self.worker = None if worker is False else (worker if worker is not None else ComWorker(self.kind, timeout))
        self._own = worker is None

    def _native(self, path: Path):
        from . import ole_native
        if self.kind == "word":
            text, title, author = ole_native.doc_text(str(path))
            return {"chunks": para_windows(text), "meta": {"title": title, "authors": author, "parser": "native"}}
        chunks, title = ole_native.ppt_text(str(path))
        return {"chunks": chunks, "meta": {"title": title, "slide_count": sum(c[0].startswith("slide") for c in chunks),
                                           "parser": "native"}}

    def extract(self, path: Path) -> ExtractResult:
        native_err = None
        try:
            data = self._native(Path(path))
            if not any(clean(t) for _l, t in data["chunks"]):
                raise ValueError("no text")
            tag = "ok"
        except Exception as exc:                       # fall back to Office COM
            native_err = f"{type(exc).__name__}: {exc}"
            if self.worker is None:
                return ExtractResult(status="failed", error=f"native: {native_err}; com: unavailable")
            tag, data = self.worker.run(Path(path))
        if tag != "ok":
            return ExtractResult(status="failed", error=f"native: {native_err}; com: {data}")
        res = ExtractResult(meta=data["meta"])
        for loc, text in data["chunks"]:
            t = clean(text)
            if t:
                res.chunks.append(Chunk(loc, t, has_table=" | " in t))
        if not res.meta.get("title") and res.chunks:
            res.meta["title_guess"] = res.chunks[0].text.split("\n", 1)[0][:200]
        res.status = "ok" if res.chunks else "empty"
        return res

    def close(self) -> None:
        if self._own:
            self.worker.close()


def _sweep(cfg, folder: Path, age_s: float = 600) -> None:
    """Remove copies and Word's ``~WRD*.tmp`` files that a killed save left in the copy folder; recent
    ones may belong to a save still running."""
    from .. import safeio
    now = time.time()
    for f in folder.iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > age_s:
                safeio.unlink_at(safeio.check(cfg, f))
        except OSError:
            pass                                # still open in a Word that has not exited yet


class DocExtractor(_ComExtractor):
    """Word through COM: a temporary .docx copy read by the Word reader (word.py); without Word, or
    when the copy fails, the Office-free parser. ``meta['com_error']`` keeps a failed copy's reason
    (the indexer's circuit breaker counts its timeouts)."""
    exts = ("doc",)
    kind = "word"
    version = "doc-2"

    def extract(self, path: Path) -> ExtractResult:
        if self.worker is None or self.cfg is None:   # no Word, or no site to hold the copy: as before
            return super().extract(path)
        import uuid

        from .. import safeio
        from .word import DocxExtractor
        folder = safeio.mkdir_at(safeio.check(self.cfg, self.cfg.log_dir / "doc_copy"))
        out = safeio.check(self.cfg, folder / f"{uuid.uuid4().hex}.docx")
        try:
            tag, data = self.worker.run(("docx", str(Path(path).resolve()), str(out)))
            if tag == "ok" and out.is_file():
                res = DocxExtractor(self.cfg).extract(out)
                if res.status == "ok":
                    res.meta["parser"] = "word-com-docx"
                    return res
                data = res.error or "Word copy has no text"     # the Office-free parser may still read it
            com_error = str(data)[:300]
        finally:
            safeio.unlink_at(out)
            _sweep(self.cfg, folder)
        worker, self.worker = self.worker, None          # fallback: the Office-free parser only
        try:
            res = super().extract(path)
        finally:
            self.worker = worker
        res.meta["com_error"] = com_error
        if res.status == "failed":
            res.error = f"{res.error}; com: {com_error}"
        return res


class PptExtractor(_ComExtractor):
    exts = ("ppt",)
    kind = "powerpoint"
