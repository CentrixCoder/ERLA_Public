#!/usr/bin/env python3
"""ERLA G3 answer gate launcher (standard library only).

Run by the plugin's Stop hook and its SubagentStop hook for the ERLA agent (hooks/hooks.json) when a turn ends,
through uv like the MCP server (``uv run --no-project --python >=3.11 answergate.py``). It loads
``erla/answergate.py`` from the plugin's server copy by path (no runtime started) and prints its decision: block
the stop once when the answer reports values no verify_packet of the session passed, or warn the user when they are
still unverified on the continuation.

A gate that could not run is not a pass (L10), but a Stop hook that blocks on its own failure would send the agent
back with nothing it can fix: an error here ends the turn with a warning to the user that the answer was not
checked. ``onFailure`` does not apply to Stop hooks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launch  # noqa: E402  (same folder: the plugin's server copy)

GATE = launch.SERVER_SRC / "erla" / "answergate.py"


def main() -> int:
    try:
        inp = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        spec = importlib.util.spec_from_file_location("erla_answergate_hook", GATE)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {GATE.name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        out = mod.decide(inp if isinstance(inp, dict) else {})
    except Exception as exc:                         # never silent: the user sees the answer went unchecked
        out = {"systemMessage": f"ERLA answer gate (G3) could not check this answer ({type(exc).__name__}): treat "
                                "its values as NOT VERIFIED unless the packet shows a passing verify_packet."}
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
