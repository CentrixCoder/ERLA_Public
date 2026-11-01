#!/usr/bin/env python3
"""ERLA plugin launcher (standard library only).

Started through uv (.mcp.json: ``uv run --no-project --python >=3.11 launch.py``), so the
same command works on Windows, macOS and Linux and uv supplies Python 3.11+ when none is
installed.

Server mode (default, started by Claude Code from .mcp.json):
  * runtime installed and current, no install running -> run the ERLA MCP server
    from the private venv
  * an install is running -> serve a small stub MCP server that reports install
    progress (never start the real server while pip is replacing its files)
  * otherwise -> start the installer in the background and serve the stub.

Only one installer runs at a time (atomic lock file). An interactive install
(/erla:setup) that finds another install running waits for it to finish.

Install mode:   uv run --no-project --python ">=3.11" launch.py --install
                (used by /erla:setup; prints progress)

The private environment lives in $CLAUDE_PLUGIN_DATA (kept across plugin updates);
the ERLA site (config, index, logs, out) lives in %LOCALAPPDATA%\\ERLA unless
ERLA_ROOT is set, so it is shared with the ERLA desktop extension.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_SRC = PLUGIN_ROOT / "server"
MIN_PY = (3, 11)


def data_dir() -> Path:
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "ERLA" / "plugin-runtime"


DATA = data_dir()
VENV = DATA / "venv"
VPY = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
STAMP = DATA / "installed.json"
LOG = DATA / "install.log"
LOCK = DATA / "install.lock"


def wanted_version() -> str:
    text = (SERVER_SRC / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "0"


def venv_healthy() -> bool:
    """The venv exists and the interpreter it was built from is still there."""
    if not VPY.is_file():
        return False
    try:
        cfg = (VENV / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return False
    m = re.search(r"^home\s*=\s*(.+?)\s*$", cfg, re.M)
    return bool(m) and Path(m.group(1)).is_dir()


def installed_ok() -> bool:
    if not (STAMP.is_file() and venv_healthy()):
        return False
    try:
        return json.loads(STAMP.read_text(encoding="utf-8")).get("version") == wanted_version()
    except (OSError, ValueError):
        return False


def _log(msg: str, echo: bool) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if echo:
        # /erla:setup pipes stdout in the console code page (cp1252): a runtime path under a Cyrillic or CJK user
        # name raised UnicodeEncodeError and aborted the install (correctness audit 2026-10-31)
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


RECONNECT = ("A session that started during the install switches to it by itself; if ERLA's library tools "
             "do not appear, quit and reopen the Claude app (/reload-plugins and /mcp Reconnect do not "
             "restart a running server).")
LOCK_STALE_S = 3600          # an install lock older than this is treated as abandoned


def install_running() -> bool:
    if not LOCK.is_file():
        return False
    try:
        return time.time() - LOCK.stat().st_mtime < LOCK_STALE_S
    except OSError:
        return False


def take_lock() -> bool:
    """Create the install lock atomically. False if another live install holds it."""
    DATA.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if install_running():
                return False
            LOCK.unlink(missing_ok=True)       # stale lock from a crashed install
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def wait_for_install(echo: bool, timeout_s: float = LOCK_STALE_S, poll_s: float = 2.0) -> int:
    _log("another ERLA install is already running; waiting for it to finish", echo)
    end = time.time() + timeout_s
    while install_running() and time.time() < end:
        time.sleep(poll_s)
    if installed_ok():
        _log(f"ERLA runtime {wanted_version()} is installed. {RECONNECT}", echo)
        return 0
    _log(f"ERROR: the other install did not finish with ERLA {wanted_version()}; see {LOG}", echo)
    return 1


def install(echo: bool = True) -> int:
    if sys.version_info < MIN_PY:
        _log(f"ERROR: Python {MIN_PY[0]}.{MIN_PY[1]}+ required, found {sys.version.split()[0]}", echo)
        return 2
    if not take_lock():
        return wait_for_install(echo) if echo else 0
    try:
        _log(f"installing ERLA {wanted_version()} into {VENV}", echo)
        if not venv_healthy():                  # missing, or its base Python was removed
            subprocess.run([sys.executable, "-m", "venv", "--clear", str(VENV)], check=True)
        steps = [[str(VPY), "-m", "pip", "install", "--upgrade", "pip"],
                 [str(VPY), "-m", "pip", "install", "--upgrade", str(SERVER_SRC)]]
        for cmd in steps:
            _log("running: " + " ".join(cmd[2:]), echo)
            with open(LOG, "a", encoding="utf-8") as fh:
                rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
            if rc != 0:
                _log(f"ERROR: step failed (exit {rc}); see {LOG}", echo)
                return rc
        STAMP.write_text(json.dumps({"version": wanted_version(), "python": sys.version.split()[0],
                                     "installed": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        _log(f"ERLA runtime installed. {RECONNECT}", echo)
        return 0
    finally:
        LOCK.unlink(missing_ok=True)


def start_background_install() -> None:
    if install_running():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    flags = 0x00000008 | 0x00000200 | 0x08000000 if sys.platform == "win32" else 0
    with open(LOG, "a", encoding="utf-8") as fh:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--install", "--quiet"],
                         stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                         creationflags=flags, start_new_session=(sys.platform != "win32"), close_fds=True)


def server_env() -> dict[str, str]:
    env = dict(os.environ)
    roots = env.get("ERLA_LIBRARY_ROOTS", "")
    if "${" in roots:                          # setting not filled in yet
        env.pop("ERLA_LIBRARY_ROOTS", None)
    env.pop("PYTHONPATH", None)
    return env


# -P: never import from the current directory; only the installed package is used.
def server_cmd() -> list[str]:
    return [str(VPY), "-P", "-m", "erla.mcp_server"]


def main(argv: list[str]) -> int:
    if "--install" in argv:
        return install(echo="--quiet" not in argv)
    DATA.mkdir(parents=True, exist_ok=True)
    if installed_ok() and not install_running():
        return subprocess.call(server_cmd(), env=server_env(), cwd=str(DATA))
    if sys.version_info >= MIN_PY and not install_running():
        start_background_install()
    # the stub hands over to the real server by itself once the install finishes (no reconnect)
    stub = PLUGIN_ROOT / "scripts" / "stub_server.py"
    mcp_mode = SERVER_SRC / "erla" / "core" / "protocols" / "MCP_MODE.md"
    return subprocess.call([sys.executable, str(stub), str(LOG), wanted_version(),
                            f"{sys.version_info[0]}.{sys.version_info[1]}",
                            "--handoff", str(STAMP), str(LOCK), str(mcp_mode), "--", *server_cmd()],
                           env=server_env(), cwd=str(DATA))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
