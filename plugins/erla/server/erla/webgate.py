"""G2 web gate: the PreToolUse hook check for WebSearch and WebFetch.

Claude Code runs this before every WebSearch / WebFetch call (plugin hook, through
``scripts/webgate.py``). It applies the same check as the ``web_query_check`` tool
(``classify.web_query_check``: part-number tokens, email addresses, terms from flagged
documents in the index) to what the call would send to a third party: the search query,
or the URL to fetch. The WebFetch ``prompt`` is not checked: it is applied to the fetched
page by Claude, not sent to the site.

Protocol: hook JSON on stdin; on a block, a PreToolUse ``deny`` decision on stdout (the
reason reaches the model); on a pass, no output (the normal permission flow continues).
Always exit 0: every failure is turned into a deny here (fail closed, user decision
2026-10-12), and the hook's ``onFailure: block`` covers a crash before this code runs.

No library configured on this computer: nothing is indexed to leak, so the pattern part of
the check still runs and decides. Index present but unreadable: deny.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO
from urllib.parse import unquote_plus

GATED = ("WebSearch", "WebFetch")

FIX = ("Web search and fetch stay blocked until the check works: in Claude Desktop turn the ERLA plugin "
       "off and on (or run /erla:setup); tell the user.")


def outbound_text(tool: str, tool_input: dict[str, Any]) -> str | None:
    """What the call would send to a third party, or None for a tool the gate does not cover.
    A gated tool without its field raises (schema change), which the caller turns into a deny."""
    if tool == "WebSearch":
        return str(tool_input["query"])
    if tool == "WebFetch":
        url = str(tool_input["url"])
        decoded = unquote_plus(url)
        return url if decoded == url else f"{url}\n{decoded}"
    return None


def decide(payload: dict[str, Any]) -> str | None:
    """The deny reason for this hook call, or None to let it pass."""
    from .classify import web_allow_terms, web_query_check
    from .config import ConfigError, load_config

    tool = str(payload.get("tool_name") or "")
    text = outbound_text(tool, payload.get("tool_input") or {})
    if text is None or not text.strip():
        return None

    try:
        cfg = load_config()
    except ConfigError:
        cfg = None                       # no library on this computer: patterns only
    con = None
    if cfg is not None:
        from . import store
        con = store.connect(cfg.index_path)   # read-only; a missing index reads as empty
    try:
        reasons = web_query_check(text, con, web_allow_terms(cfg))
    finally:
        if con is not None:
            con.close()

    if cfg is not None:
        try:
            from . import report
            report.append_log(cfg, {"cmd": "hook.webcheck", "tool": tool,
                                    "verdict": "BLOCKED" if reasons else "SAFE", "n_reasons": len(reasons),
                                    "agent_id": payload.get("agent_id")})
        except Exception:
            pass                         # the audit line is best effort; the decision stands

    if not reasons:
        return None
    what = "search query" if tool == "WebSearch" else "URL"
    return (f"ERLA web gate (G2): this {what} was not sent because it {'; '.join(reasons)}. "
            "Rewrite it with generic technical terms only (no part numbers, program or customer names, "
            "emails, or file names and titles of restricted documents) and try again; if the term is "
            "essential, answer from the local library and report the gap.")


def deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    src = stdin if stdin is not None else sys.stdin
    dst = stdout if stdout is not None else sys.stdout
    try:
        if src is None:
            raise RuntimeError("no standard input")
        reason = decide(json.loads(src.read() or "{}"))
    except Exception as err:             # fail closed
        reason = (f"ERLA web gate (G2) could not check this request, so it was blocked: "
                  f"{type(err).__name__}: {err}. {FIX}")
    if reason and dst is not None:
        dst.write(json.dumps(deny(reason)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
