#!/usr/bin/env python3
"""ERLA G2 web gate and G1.1 write guard launcher (standard library only).

Run by the plugin's PreToolUse hook (hooks/hooks.json) before every WebSearch / WebFetch,
Bash / PowerShell and MCP tool call, through uv like the MCP server
(``uv run --no-project --python >=3.11 webgate.py``).

Fast path (most calls, no runtime started): ``erla/shellgate.py`` from the plugin's server copy,
loaded by path, passes a call with nothing to check (a shell command with no network verb or URL
and no write, an ERLA tool, an MCP tool that reaches no site) and decides a shell write against
the protected-path list the runtime cached in the plugin's data folder. Everything else - web
tools, network commands, browser tools, a write without a valid cache - goes to ``erla.webgate``
in ERLA's private runtime, and this script relays its decision.

Fail closed: when the runtime is missing, being installed, out of date, or the check fails
or prints something that is not a decision, the call is denied with the reason. The hook's
``onFailure: block`` covers this script itself failing to start. A fast-path error never allows:
the call then goes to the runtime.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launch  # noqa: E402  (same folder: runtime location and install state)

TIMEOUT_S = 25          # the hook allows 30 s
SHELLGATE = launch.SERVER_SRC / "erla" / "shellgate.py"    # the runtime's own copy of the parser (same version)
CACHE = launch.DATA / "protected_paths.json"               # written by erla.webgate (save_protected_cache)
ALLOW = "allow"


def deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"ERLA web gate (G2) blocked this call: {reason}"}}))


def deny_as_is(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))


def load_shellgate() -> ModuleType | None:
    try:
        spec = importlib.util.spec_from_file_location("erla_shellgate_hook", SHELLGATE)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def runtime_ready() -> bool:
    return not launch.install_running() and launch.installed_ok()


def cached_protected() -> list[tuple[str, str]] | None:
    """The runtime's protected-path list, while it still describes this site: same ERLA_ROOT, same site
    config file (mtime and size), and the same ERLA version - or any version while the runtime is being
    replaced (an update), which is better than refusing every write until it finishes."""
    try:
        rec = json.loads(CACHE.read_text(encoding="utf-8"))
        if rec.get("erla_root", "") != os.environ.get("ERLA_ROOT", ""):
            return None
        cfg = Path(rec["config"])
        try:
            st = cfg.stat()
            stamp: list[int] | None = [st.st_mtime_ns, st.st_size]
        except OSError:
            stamp = None
        if stamp != rec.get("config_stamp"):
            return None
        if rec.get("version") != launch.wanted_version() and runtime_ready():
            return None
        return [(str(k), str(p)) for k, p in rec["items"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def local_decision(sg: ModuleType, payload: dict) -> str | None:
    """ALLOW, a deny reason, or None when the runtime must decide."""
    tool, inp = payload.get("tool_name"), payload.get("tool_input")
    if sg.fast_allow(tool, inp):
        return ALLOW
    shell = sg.shell_command(str(tool), inp)
    if shell is None:
        return None                           # a web tool or an MCP tool that may reach a site
    cmd, ps = shell
    if sg.scan(cmd)[0]:
        return None                           # a network command: the restricted-term check needs the index
    cwd = payload.get("cwd")
    analysis = sg.analyse(cmd, cwd if isinstance(cwd, str) and cwd else None, ps=ps)
    if analysis.empty:
        return ALLOW
    if analysis.refusal:
        return sg.write_reason(analysis, sg.Protected([]), cmd)
    items = cached_protected()
    if items is None:
        return None
    return sg.write_reason(analysis, sg.Protected(items), cmd) or ALLOW


def main() -> int:
    payload = sys.stdin.buffer.read()
    sg = load_shellgate()
    if sg is not None:
        try:
            parsed = json.loads(payload.decode("utf-8", "replace"))
            local = local_decision(sg, parsed) if isinstance(parsed, dict) else None
        except Exception:
            local = None                      # never allow on an error: the runtime decides (or denies)
        if local == ALLOW:
            return 0
        if local:
            deny_as_is(local)
            return 0
    if launch.install_running():
        deny("ERLA's runtime is being installed, so the restricted-term check cannot run yet. "
             "Web search and fetch (and shell or browser calls that need the check) resume when the install "
             "finishes; tell the user.")
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
