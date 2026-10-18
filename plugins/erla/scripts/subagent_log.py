#!/usr/bin/env python3
"""ERLA delegation-log launcher (standard library only; ADR-0002 Phase 3).

Run by the plugin's SubagentStart / SubagentStop hooks (hooks/hooks.json) through uv, like webgate.py.
It hands the hook JSON to ``erla.hooklog`` in ERLA's private runtime, which appends one line per ERLA
sub-agent start or stop to the site's retrieval log. It never blocks an agent: when the runtime is
missing or being installed, or anything fails, it exits 0 and logs nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launch  # noqa: E402  (same folder: runtime location and install state)

TIMEOUT_S = 10


def main() -> int:
    payload = sys.stdin.buffer.read()
    try:
        if launch.install_running() or not launch.installed_ok():
            return 0
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        subprocess.run([str(launch.VPY), "-P", "-m", "erla.hooklog"], input=payload, capture_output=True,
                       timeout=TIMEOUT_S, env=env, cwd=str(launch.DATA))
    except Exception:              # noqa: BLE001 - logging must never stop an agent
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
