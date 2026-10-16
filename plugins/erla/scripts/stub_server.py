#!/usr/bin/env python3
"""Minimal stdio MCP server (standard library only) shown while ERLA's runtime installs.

Exposes one tool, ``erla_setup_status``, that reports install progress, so the
user is never left with a silently failing server.

Hand-off (0.4.2): Claude Code keeps a plugin's server connected for the whole session, and
neither /reload-plugins nor /mcp Reconnect restarted the stub (tested 2026-10-16), so the
session kept the stub after the install finished. With ``--handoff``, the stub watches the
install; once the runtime stamp says this version and no install holds the lock, it starts
the real server, replays the client's ``initialize`` to it, forwards every later message both
ways, and tells the client that its tools, resources and prompts changed. No reconnect.

Args: <install.log> <version> <python>
      [--handoff <installed.json> <install.lock> <MCP_MODE.md> -- <real server command...>]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else None
VERSION = sys.argv[2] if len(sys.argv) > 2 else "?"
PY = sys.argv[3] if len(sys.argv) > 3 else "?"
OLD_PY = tuple(int(x) for x in PY.split(".")[:2] if x.isdigit()) < (3, 11)
POLL_S = float(os.environ.get("ERLA_STUB_POLL_S", "2"))
HANDOFF_ID = "erla-stub-handoff"

RESTART = ("quit and reopen the Claude app (the erla server restarts with it; /reload-plugins and /mcp "
           "Reconnect do not restart it)")


def _handoff_args(argv: list[str]) -> tuple[Path, Path, Path, list[str]] | None:
    if "--handoff" not in argv or "--" not in argv:
        return None
    i, j = argv.index("--handoff"), argv.index("--")
    stamp, lock, instructions = argv[i + 1:i + 4]
    return Path(stamp), Path(lock), Path(instructions), argv[j + 1:]


HANDOFF = _handoff_args(sys.argv)
state = {"failed": ""}       # why the hand-off failed, when it did


def status_text() -> str:
    if OLD_PY:
        return (f"ERLA needs Python 3.11 or newer; this computer's `python` is {PY}. Install Python from "
                "python.org (tick 'Add python.exe to PATH'), then run /erla:setup.")
    tail = ""
    if LOG and LOG.is_file():
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-12:])
    done = "ERLA runtime installed" in tail
    if state["failed"]:
        head = (f"Install finished: ERLA {VERSION}, but the library server could not start here "
                f"({state['failed']}). To load it, {RESTART}.")
    elif done and HANDOFF:
        head = f"Install finished: ERLA {VERSION}. The library tools are loading; they appear within seconds."
    elif done:
        head = f"Install finished: ERLA {VERSION}. To load the library tools, {RESTART}."
    else:
        head = (f"ERLA {VERSION} is installing its Python runtime in the background (first run only, several "
                "minutes). " + ("The library tools appear by themselves when it finishes; "
                                if HANDOFF else f"When this reports finished, {RESTART}; ")
                + "run /erla:setup to watch the install.")
    return head + ("\n\nInstall log (last lines):\n" + tail if tail else "")


def instructions() -> str:
    """The real server's instructions (MCP_MODE.md), so they hold after a hand-off."""
    note = ("ERLA is still installing: until its library tools appear, call erla_setup_status and relay it. "
            "The library tools appear in this session by themselves when the install finishes.")
    text = ""
    if HANDOFF and HANDOFF[2].is_file():
        text = HANDOFF[2].read_text(encoding="utf-8", errors="replace")
    return f"{note}\n\n{text}" if text else note


TOOL = {"name": "erla_setup_status", "description": "Report ERLA installation progress. The library tools "
        "appear when the install finishes.", "inputSchema": {"type": "object", "properties": {}}}

out_lock = threading.Lock()


def send(obj: dict) -> None:
    write_line(json.dumps(obj).encode("utf-8") + b"\n")


def write_line(raw: bytes) -> None:
    with out_lock:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()


def reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    out["error" if error else "result"] = error or result
    send(out)


class Real:
    """The real server, once started: the client's messages go to it, its messages go to the client."""

    def __init__(self, cmd: list[str], init_params: dict) -> None:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.stdin, self.stdout = self.proc.stdin, self.proc.stdout
        self.forward(json.dumps({"jsonrpc": "2.0", "id": HANDOFF_ID, "method": "initialize",
                                 "params": init_params}).encode("utf-8") + b"\n")
        got: list[bytes] = []
        reader = threading.Thread(target=lambda: got.append(self._initialize_reply()), daemon=True)
        reader.start()
        reader.join(timeout=120)
        if not got or not got[0]:
            self.proc.kill()
            raise RuntimeError("the server did not answer initialize")
        self.forward(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        threading.Thread(target=self._pump, daemon=True).start()

    def _initialize_reply(self) -> bytes:
        for raw in self.stdout:
            try:
                if json.loads(raw).get("id") == HANDOFF_ID:
                    return raw
            except ValueError:
                pass
            write_line(raw)           # a notification sent before it answered: pass it on
        return b""

    def _pump(self) -> None:
        for raw in self.stdout:
            write_line(raw)
        # the real server ended: end the stub too (0 when the client closed first, else as a crashed server)
        os._exit(0 if self.stdin.closed else 1)

    def forward(self, raw: bytes) -> None:
        self.stdin.write(raw)
        self.stdin.flush()


real: list[Real] = []                  # set once, by the hand-off
init_params: dict = {}


def ready() -> bool:
    assert HANDOFF is not None
    stamp, lock, _, cmd = HANDOFF
    try:
        current = json.loads(stamp.read_text(encoding="utf-8")).get("version") == VERSION
    except (OSError, ValueError):
        return False
    return current and not lock.exists() and bool(cmd) and Path(cmd[0]).is_file()


def watch() -> None:
    while not ready():
        time.sleep(POLL_S)
    while not init_params:            # the client has not initialized yet
        time.sleep(0.1)
    try:
        server = Real(HANDOFF[3], init_params) if HANDOFF else None
    except (OSError, RuntimeError) as exc:
        state["failed"] = str(exc)
        return
    if server is None:
        return
    real.append(server)
    for kind in ("tools", "resources", "prompts"):
        send({"jsonrpc": "2.0", "method": f"notifications/{kind}/list_changed"})


def local(msg: dict) -> None:
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:                # notification
        return
    if method == "initialize":
        init_params.update(msg.get("params") or {})
        pv = init_params.get("protocolVersion", "2025-06-18")
        caps = {k: {"listChanged": True} for k in ("tools", "resources", "prompts")} if HANDOFF else {"tools": {}}
        reply(msg_id, {"protocolVersion": pv, "capabilities": caps,
                       "serverInfo": {"name": "erla", "version": VERSION}, "instructions": instructions()})
    elif method == "tools/list":
        reply(msg_id, {"tools": [TOOL]})
    elif method == "tools/call":
        reply(msg_id, {"content": [{"type": "text", "text": status_text()}]})
    elif method == "ping":
        reply(msg_id, {})
    elif method in ("resources/list", "prompts/list", "resources/templates/list"):
        key = {"resources/list": "resources", "prompts/list": "prompts", "resources/templates/list": "resourceTemplates"}
        reply(msg_id, {key[method]: []})
    else:
        reply(msg_id, error={"code": -32601, "message": f"method not available during install: {method}"})


def main() -> None:
    if HANDOFF and not OLD_PY:
        threading.Thread(target=watch, daemon=True).start()
    for raw in sys.stdin.buffer:
        if real:
            real[0].forward(raw)
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        local(msg)
    if real:                          # the client closed: close the real server's input so it ends too
        real[0].stdin.close()
        try:
            real[0].proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            real[0].proc.kill()


if __name__ == "__main__":
    main()
