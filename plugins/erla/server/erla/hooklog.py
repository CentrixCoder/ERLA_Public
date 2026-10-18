"""Delegation log (ADR-0002 Phase 3, law L10): one line per ERLA sub-agent start and stop.

Run by the plugin's SubagentStart / SubagentStop hooks (scripts/subagent_log.py hands the hook JSON on
stdin to ``python -m erla.hooklog`` in ERLA's runtime). It appends ``{"cmd": "hook.subagent", ...}`` to the
site's retrieval log, so evaluations and reviews see from code, not from the model's wording, which
ERLA agents ran and for how long. Only ``erla:*`` agents are logged; other plugins' agents are not
ERLA's business. Logging never blocks or fails an agent: every error ends quietly with exit 0.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def record(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The log line for one hook event, or None when it is not an ERLA agent's start or stop."""
    event = str(payload.get("hook_event_name") or "")
    agent = str(payload.get("agent_type") or "")
    if event not in ("SubagentStart", "SubagentStop") or not agent.startswith("erla:"):
        return None
    out: dict[str, Any] = {"cmd": "hook.subagent", "event": event, "agent_type": agent,
                           "agent_id": str(payload.get("agent_id") or ""), "session_id": str(payload.get("session_id") or "")}
    if event == "SubagentStop":
        out["last_message_chars"] = len(str(payload.get("last_assistant_message") or ""))
    return out


def main() -> int:
    try:
        rec = record(json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}"))
        if rec is not None:
            from .config import load_config
            from .report import append_log
            append_log(load_config(validate=False), rec)
    except Exception:              # noqa: BLE001 - a log line is never worth blocking an agent
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
