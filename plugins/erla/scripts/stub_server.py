#!/usr/bin/env python3
"""Minimal stdio MCP server (standard library only) shown while ERLA's runtime installs.

Exposes one tool, ``erla_setup_status``, that reports install progress, so the
user is never left with a silently failing server. Args: <install.log> <version> <python>.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else None
VERSION = sys.argv[2] if len(sys.argv) > 2 else "?"
PY = sys.argv[3] if len(sys.argv) > 3 else "?"
OLD_PY = tuple(int(x) for x in PY.split(".")[:2] if x.isdigit()) < (3, 11)


def status_text() -> str:
    if OLD_PY:
        return (f"ERLA needs Python 3.11 or newer; this computer's `python` is {PY}. Install Python from "
                "python.org (tick 'Add python.exe to PATH'), then run /erla:setup.")
    tail = ""
    if LOG and LOG.is_file():
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-12:])
    done = "ERLA runtime installed" in tail
    head = ("Install finished. Reconnect the erla server to load the library tools: in Claude Desktop / "
            "claude.ai turn the ERLA plugin off and on (or restart Claude Desktop); in Claude Code use /mcp."
            if done else
            f"ERLA {VERSION} is installing its Python runtime in the background (first run only, several "
            "minutes). Reconnect the erla server when this reports finished (Claude Desktop / claude.ai: "
            "turn the ERLA plugin off and on; Claude Code: /mcp), or run /erla:setup to watch the install.")
    return head + ("\n\nInstall log (last lines):\n" + tail if tail else "")


TOOL = {"name": "erla_setup_status", "description": "Report ERLA installation progress. Library tools appear "
        "after the install finishes and the server is reconnected.",
        "inputSchema": {"type": "object", "properties": {}}}


def reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    out["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:            # notification
            continue
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
            reply(msg_id, {"protocolVersion": pv, "capabilities": {"tools": {}},
                           "serverInfo": {"name": "erla", "version": VERSION},
                           "instructions": "ERLA is still installing. Call erla_setup_status and relay it."})
        elif method == "tools/list":
            reply(msg_id, {"tools": [TOOL]})
        elif method == "tools/call":
            reply(msg_id, {"content": [{"type": "text", "text": status_text()}]})
        elif method == "ping":
            reply(msg_id, {})
        elif method in ("resources/list", "prompts/list"):
            reply(msg_id, {method.split("/")[0]: []})
        else:
            reply(msg_id, error={"code": -32601, "message": f"method not available during install: {method}"})


if __name__ == "__main__":
    main()
