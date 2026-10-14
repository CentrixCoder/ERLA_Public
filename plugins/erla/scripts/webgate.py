#!/usr/bin/env python3
"""ERLA G2 web gate launcher (standard library only).

Run by the plugin's PreToolUse hook (hooks/hooks.json) before every WebSearch / WebFetch,
through uv like the MCP server (``uv run --no-project --python >=3.11 webgate.py``). It hands
the hook JSON to ``erla.webgate`` in ERLA's private runtime and relays its decision.

Fail closed: when the runtime is missing, being installed, out of date, or the check fails
or prints something that is not a decision, the call is denied with the reason. The hook's
``onFailure: block`` covers this script itself failing to start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launch  # noqa: E402  (same folder: runtime location and install state)

TIMEOUT_S = 25          # the hook allows 30 s


def deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"ERLA web gate (G2) blocked this call: {reason}"}}))


def main() -> int:
    payload = sys.stdin.buffer.read()
    if launch.install_running():
        deny("ERLA's runtime is being installed, so the restricted-term check cannot run yet. "
             "Web search and fetch resume when the install finishes; tell the user.")
        return 0
    if not launch.installed_ok():
        deny("ERLA's runtime is not installed or is out of date, so the restricted-term check cannot run. "
             "Ask the user to run /erla:setup (or turn the ERLA plugin off and on).")
        return 0
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    try:
        # -P: import only the installed package, never the current directory
        run = subprocess.run([str(launch.VPY), "-P", "-m", "erla.webgate"], input=payload,
                             capture_output=True, timeout=TIMEOUT_S, env=env, cwd=str(launch.DATA))
    except (OSError, subprocess.TimeoutExpired) as err:
        deny(f"the check did not complete ({type(err).__name__}). Tell the user.")
        return 0
    out = run.stdout.decode("utf-8", "replace").strip()
    if run.returncode != 0:
        tail = run.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        deny(f"the check failed (exit {run.returncode}: {tail[0][:300]}). Tell the user.")
        return 0
    if out:
        try:
            decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
        except (ValueError, KeyError, TypeError):
            decision = None
        if decision != "deny":
            deny("the check returned an unreadable answer. Tell the user.")
            return 0
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
