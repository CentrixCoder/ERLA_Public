"""Single guarded writer (guardrails G1.1 / G1.2).

Every file-system write made by the toolkit goes through this module, in two layers:

1. :meth:`Guard.check` (or :func:`check`) validates a target and returns a
   :data:`WritablePath`. This is the only way to obtain one.
2. The ``*_at`` functions perform I/O and accept only a :data:`WritablePath`
   (enforced statically by pyright, by convention at run time). The convenience
   functions (``write_text``, ``append_text`` ...) do both steps.

Allowed targets (G1.2):
  * ``out_dir``, ``log_dir``, ``web_cache_dir`` and the model cache ``<agent_root>/models``
  * the index file and its SQLite side files (``-wal``, ``-shm``, ``-journal``)
  * the vector-index files in the agent root (``vectors.faiss``, ``vectors.f16.npy``,
    ``vector_ids.npy`` and their ``.tmp`` staging names)
Setup scope (``setup=True``; only ``erla init`` / ``erla set-library``, which the user runs
explicitly - the G1.2 exception for ``config/*.yaml``): ``config/``, ``eval/``,
``SITE_NOTES.md`` and ``PROTOCOL_REVIEW.md`` in the site folder.

Never allowed: anything inside a library root (G1.1) or outside the agent root.
``tests/test_safeio.py`` checks by AST scan that no other ``erla`` module calls a raw
file-system write primitive.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, NewType, Union

from .config import ReadOnlyViolation

if TYPE_CHECKING:
    from .config import Config

WritablePath = NewType("WritablePath", Path)

VECTOR_FILES = frozenset({"vectors.faiss", "vectors.tmp", "vectors.f16.npy", "vectors.f16.tmp.npy",
                          "vector_ids.npy", "vector_ids.tmp.npy"})
SQLITE_SIDECARS = ("", "-wal", "-shm", "-journal")
SETUP_DIRS = ("config", "eval")
SETUP_FILES = ("SITE_NOTES.md", "PROTOCOL_REVIEW.md")
RUNTIME_DIRS = ("out", "logs", "web_cache", "models")
TOOLS_DIR = "tools"
# the Engineer Tools store is a write location only in builds that ship erla/engtools.py (the full edition)
TOOLS_SHIPPED = (Path(__file__).with_name("engtools.py")).is_file()


def _norm(p: Path | str) -> Path:
    """Resolved, case-normalised (Windows) path used only for comparisons."""
    return Path(os.path.normcase(str(Path(p).resolve())))


def _inside(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Guard:
    """Write policy for one site. Build with :meth:`from_config` or :meth:`for_site`."""

    agent_root: Path
    library_roots: tuple[Path, ...]
    dirs: tuple[Path, ...] = ()
    files: frozenset[Path] = field(default_factory=lambda: frozenset[Path]())

    @classmethod
    def from_config(cls, cfg: Config) -> Guard:
        idx = _norm(cfg.index_path)
        files = {Path(str(idx) + s) for s in SQLITE_SIDECARS}
        files |= {_norm(cfg.agent_root / n) for n in VECTOR_FILES}
        dirs = (cfg.out_dir, cfg.log_dir, cfg.web_cache_dir, cfg.agent_root / "models")
        if TOOLS_SHIPPED:                     # the Engineer Tools store (G1.2, full edition only)
            dirs += (cfg.agent_root / TOOLS_DIR,)
        return cls(agent_root=_norm(cfg.agent_root),
                   library_roots=tuple(_norm(r) for r in cfg.library_roots),
                   dirs=tuple(_norm(d) for d in dirs), files=frozenset(files))

    @classmethod
    def for_site(cls, site: Path | str, library_roots: Iterable[Path | str]) -> Guard:
        """Policy for a site folder that may not have a config yet (``erla init``)."""
        root = _norm(site)
        return cls(agent_root=root, library_roots=tuple(_norm(r) for r in library_roots),
                   dirs=tuple(root / d for d in RUNTIME_DIRS))

    def _setup_ok(self, t: Path) -> bool:
        r = self.agent_root
        if any(t == r / d for d in SETUP_DIRS) or _inside(t, r / "eval"):
            return True
        if t.parent == r / "config" and t.suffix == ".yaml":
            return True
        return t.parent == r and t.name in {os.path.normcase(f) for f in SETUP_FILES}

    def allows(self, path: Path | str, *, setup: bool = False) -> bool:
        try:
            self.check(path, setup=setup)
            return True
        except ReadOnlyViolation:
            return False

    def check(self, path: Path | str, *, setup: bool = False) -> WritablePath:
        """Return the resolved path if writing it is allowed, else raise ReadOnlyViolation."""
        t = _norm(path)
        for lib in self.library_roots:
            if _inside(t, lib):
                raise ReadOnlyViolation(f"Write refused - inside library root (G1.1): {path}")
        if not _inside(t, self.agent_root):
            raise ReadOnlyViolation(f"Write refused - outside agent root (G1.2): {path}")
        if t in self.files or any(_inside(t, d) for d in self.dirs) or (setup and self._setup_ok(t)):
            return WritablePath(Path(path).resolve())
        raise ReadOnlyViolation(f"Write refused - not a G1.2 location: {path}")


Policy = Union[Guard, "Config"]


def as_guard(g: Policy) -> Guard:
    return g if isinstance(g, Guard) else Guard.from_config(g)


def check(g: Policy, path: Path | str, *, setup: bool = False) -> WritablePath:
    return as_guard(g).check(path, setup=setup)


# ------------------------------------------------- layer 2: I/O on checked paths
def mkdir_at(p: WritablePath) -> WritablePath:
    p.mkdir(parents=True, exist_ok=True)
    return p


def parent_at(p: WritablePath) -> WritablePath:
    """Create the parent directory of a checked path (the parent may be the agent root)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_text_at(p: WritablePath, text: str, encoding: str = "utf-8") -> WritablePath:
    """Write atomically: a reader sees the old content or the new, never a partly written file
    (index_status read the job status file empty while the job rewrote it, 0.2.18).

    The staging file sits next to the target, in the same checked location. On Windows the final
    rename fails while a reader holds the target open; reads are brief, so it is retried with short
    sleeps (many chances to land between two reads) for up to 2 s, and only then falls back to a plain
    write. 0.3.2: ten attempts were not enough on a loaded machine and the fallback let a reader see
    an empty file."""
    parent_at(p)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding=encoding)
    deadline = time.monotonic() + 2.0
    attempt = 0
    while True:
        try:
            os.replace(tmp, p)
            return p
        except PermissionError:
            attempt += 1
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.002 * attempt, 0.02))
    tmp.unlink(missing_ok=True)
    p.write_text(text, encoding=encoding)
    return p


def append_text_at(p: WritablePath, text: str) -> WritablePath:
    with open(parent_at(p), "a", encoding="utf-8") as fh:
        fh.write(text)
    return p


def create_exclusive_at(p: WritablePath, text: str) -> WritablePath:
    """Atomically create ``p``; raises FileExistsError if it exists (lock files)."""
    fd = os.open(str(parent_at(p)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return p


def touch_at(p: WritablePath) -> None:
    """Update mtime only (lock heartbeat); silently ignores a vanished file."""
    try:
        os.utime(p, None)
    except OSError:
        pass


def unlink_at(p: WritablePath, missing_ok: bool = True) -> None:
    p.unlink(missing_ok=missing_ok)


def replace_at(src: WritablePath, dst: WritablePath) -> WritablePath:
    os.replace(src, dst)
    return dst


@contextmanager
def open_at(p: WritablePath, mode: str = "w", *, newline: str | None = None,
            encoding: str | None = "utf-8") -> Generator[IO[Any]]:
    if not any(c in mode for c in "wax+"):
        raise ValueError(f"open_at needs a write mode, got {mode!r}")
    with open(parent_at(p), mode, newline=newline, encoding=None if "b" in mode else encoding) as fh:
        yield fh


def np_save_at(p: WritablePath, arr: Any) -> WritablePath:
    import importlib
    numpy: Any = importlib.import_module("numpy")   # numpy stubs leave `arr` partially unknown
    numpy.save(parent_at(p), arr)
    return p


def np_open_memmap_at(p: WritablePath, dtype: Any, shape: tuple[int, ...]) -> Any:
    import numpy as np
    return np.lib.format.open_memmap(parent_at(p), mode="w+", dtype=dtype, shape=shape)


def faiss_write_at(p: WritablePath, faiss: Any, index: Any) -> WritablePath:
    faiss.write_index(index, str(parent_at(p)))
    return p


# ---------------------------------------------- convenience: check, then write
def mkdir(g: Policy, path: Path | str, *, setup: bool = False) -> WritablePath:
    return mkdir_at(check(g, path, setup=setup))


def write_text(g: Policy, path: Path | str, text: str, *, setup: bool = False) -> WritablePath:
    return write_text_at(check(g, path, setup=setup), text)


def append_text(g: Policy, path: Path | str, text: str, *, setup: bool = False) -> WritablePath:
    return append_text_at(check(g, path, setup=setup), text)


def append_jsonl(g: Policy, path: Path | str, record: dict[str, Any], *, setup: bool = False) -> WritablePath:
    import json
    return append_text(g, path, json.dumps(record, default=str) + "\n", setup=setup)


@contextmanager
def open_write(g: Policy, path: Path | str, mode: str = "w", *, newline: str | None = None,
               setup: bool = False) -> Generator[IO[Any]]:
    with open_at(check(g, path, setup=setup), mode, newline=newline) as fh:
        yield fh


def unlink(g: Policy, path: Path | str, *, missing_ok: bool = True) -> None:
    unlink_at(check(g, path), missing_ok=missing_ok)


def copyfile(g: Policy, src: Path | str, dst: Path | str, *, setup: bool = False) -> WritablePath:
    d = parent_at(check(g, dst, setup=setup))
    shutil.copyfile(src, d)
    return d


def rmtree(g: Policy, path: Path | str) -> None:
    """Remove a folder and its contents. The folder itself must be a checked location, and no
    location is removable as a whole (``out/`` or ``tools/`` stay; only folders inside them go)."""
    p = check(g, path)
    guard = as_guard(g)
    if _norm(p) in guard.dirs or _norm(p) == guard.agent_root:
        raise ReadOnlyViolation(f"Write refused - a whole write location cannot be removed: {path}")
    shutil.rmtree(p, ignore_errors=False)
