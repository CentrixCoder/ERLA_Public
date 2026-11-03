"""G2 web gate and G1.1 write guard: the PreToolUse hook check for web, shell and MCP tool calls.

Claude Code runs this before every WebSearch / WebFetch, Bash / PowerShell and MCP tool call
(plugin hook, through ``scripts/webgate.py``, which decides most shell calls itself with
``erla.shellgate``). It applies the same check as the ``web_query_check`` tool
(``classify.web_query_check``: part-number tokens, email addresses, terms from flagged
documents in the index) to what the call would send to a third party: the search query,
the URL to fetch, the text of a network shell command (``curl``, ``Invoke-WebRequest``,
a Python one-liner) or of a browser MCP tool. The WebFetch ``prompt`` is not checked: it is
applied to the fetched page by Claude, not sent to the site. A shell command that writes
into a library root, the site config or ERLA's protocol files is denied (G1.1; the site's
``web.allow_terms`` changes only with ``erla web-allow``, run by the user). Any outbound text naming a
shadow library (libgen, sci-hub, z-library, Anna's Archive ...) is denied as an unofficial copy source
(G5.4, ``classify.shadow_library``), and WebSearch / WebFetch calls are counted per session and denied
beyond 15 of each unless the site config raises ``web.max_searches`` / ``web.max_fetches`` (G8.2).

Protocol: hook JSON on stdin; on a block, a PreToolUse ``deny`` decision on stdout (the
reason reaches the model); on a pass, no output (the normal permission flow continues).
Always exit 0: every failure is turned into a deny here (fail closed, user decision
2026-10-12), and the hook's ``onFailure: block`` covers a crash before this code runs.

No library configured on this computer: nothing is indexed to leak, so the pattern part of
the check still runs and decides. Index present but unreadable: deny.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, TextIO, cast
from urllib.parse import unquote_plus

from . import shellgate

# The tools this gate inspects by name, plus every MCP tool (MCP_MATCHER; shellgate.mcp_text decides which
# send anything). The plugin's PreToolUse matcher (packaging/plugin/hooks/hooks.json) is MATCHER, and
# _OUTBOUND below has an extractor for each GATED name (tests/test_webgate.py and test_wiring.py check they agree).
GATED = ("WebSearch", "WebFetch", "Bash", "PowerShell")
MCP_MATCHER = "mcp__.*"
MATCHER = "|".join((*GATED, MCP_MATCHER))
CACHE_NAME = "protected_paths.json"     # in the plugin's data folder: the launcher's write check reads it
# G8.2: web calls per session (the hook input's session_id), counted in the plugin's data folder (protected from
# shell writes as ERLA's runtime) or, outside the plugin, the site's log folder. The cap is the user's: only the site
# config's web.max_searches / web.max_fetches raise it (no MCP tool writes the site config, and the shell gate
# protects its folder). A count that cannot be kept denies the call (fail closed).
WEB_CAP = 15
CAPPED = {"WebSearch": ("search", "max_searches"), "WebFetch": ("fetch", "max_fetches")}
COUNTER_NAME = "web_calls.log"

FIX = ("Web search and fetch stay blocked until the check works: in Claude Desktop turn the ERLA plugin "
       "off and on (or run /erla:setup); tell the user.")


def _search_text(tool_input: dict[str, Any]) -> str:
    # the domain filters go to the search provider with the query
    domains: list[str] = []
    for k in ("allowed_domains", "blocked_domains"):
        v: Any = tool_input.get(k) or []
        domains += [str(v)] if isinstance(v, str) else [str(d) for d in cast(list[Any], v)]
    return "\n".join([str(tool_input["query"]), *domains])


def _fetch_text(tool_input: dict[str, Any]) -> str:
    url = str(tool_input["url"])
    decoded = unquote_plus(url)
    return url if decoded == url else f"{url}\n{decoded}"


def _shell_text(tool_input: dict[str, Any]) -> str:
    # what a network command could send; a command without a network verb or URL sends nothing to check
    cmd = tool_input["command"]
    if not isinstance(cmd, str):
        raise KeyError("command")
    return shellgate.network_text(cmd) if shellgate.scan(cmd)[0] else ""


_OUTBOUND: dict[str, Callable[[dict[str, Any]], str]] = {"WebSearch": _search_text, "WebFetch": _fetch_text,
                                                          "Bash": _shell_text, "PowerShell": _shell_text}


def outbound_text(tool: str, tool_input: dict[str, Any]) -> str | None:
    """What the call would send to a third party, or None for a tool the gate does not cover.
    A gated tool without its field raises (schema change), and so does a GATED name without an
    extractor; the caller turns either into a deny (fail closed). An MCP tool is covered when
    it runs a network shell command or may reach a site (shellgate.mcp_text)."""
    if tool.startswith("mcp__"):
        texts: list[str] = []
        cmd = shellgate.mcp_command(tool, tool_input)
        if cmd is not None and shellgate.scan(cmd)[0]:
            texts.append(shellgate.network_text(cmd))
        sent = shellgate.mcp_text(tool, tool_input)
        if sent:
            texts.append(sent)
        return "\n".join(texts) if texts else None
    if tool not in GATED:
        return None
    return _OUTBOUND[tool](tool_input)


def protected_paths(cfg: Any) -> list[tuple[str, str]]:
    """What no shell command may write (G1.1): the library roots, the site config folder (web.allow_terms and
    the rest of the site's settings), ERLA's protocol files, its runtime and the plugin itself (the gate's own
    code and this list's cache)."""
    from .config import AGENT_ROOT, PACKAGE_DIR, core_dir, protocol_dir
    items: list[tuple[str, str]] = []
    site = AGENT_ROOT
    if cfg is not None:
        items += [("a library root", str(r)) for r in cfg.library_roots]
        site = Path(cfg.agent_root)
    items += [("the site config folder", str(site / "config")), ("the site config folder", str(AGENT_ROOT / "config"))]
    pdir = protocol_dir()
    if pdir == core_dir():               # a source checkout: the protocol files sit beside other files
        names = ["guardrails.md", "rules_and_guidelines.md", "ERLA.md", "instructions.md", "skills.md",
                 "MCP_MODE.md", "CLOUD_MODE.md", *(p.name for p in pdir.glob("*_skills.md"))]
        items += [("ERLA's protocol files", str(pdir / n)) for n in names]
    else:
        items.append(("ERLA's protocol files", str(pdir)))
        items.append(("ERLA's protocol files", str(core_dir() / "config")))
    items.append(("ERLA's runtime", str(PACKAGE_DIR)))
    for var, kind in (("CLAUDE_PLUGIN_ROOT", "the ERLA plugin"), ("CLAUDE_PLUGIN_DATA", "ERLA's runtime")):
        if os.environ.get(var):
            items.append((kind, os.environ[var]))
    return items


def _config_stamp(path: Path) -> list[int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def save_protected_cache(items: list[tuple[str, str]]) -> None:
    """Best effort: the protected list for the plugin launcher, so it decides later shell writes without
    starting this runtime. Valid while ERLA_ROOT, the site config file and the version are unchanged
    (scripts/webgate.py checks them)."""
    if not os.environ.get("CLAUDE_PLUGIN_DATA"):
        return
    from . import __version__, safeio
    from .config import DEFAULT_CONFIG
    record = {"version": __version__, "erla_root": os.environ.get("ERLA_ROOT", ""), "config": str(DEFAULT_CONFIG),
              "config_stamp": _config_stamp(DEFAULT_CONFIG), "items": [list(i) for i in items]}
    roots = [Path(p) for kind, p in items if kind == "a library root"]
    try:
        safeio.write_plugin_data(CACHE_NAME, json.dumps(record), roots)    # refused inside a library root (G1.1)
    except OSError:
        pass                             # the launcher then asks this runtime again


def web_cap(cfg: Any, key: str) -> int:
    """The site's web.max_searches / web.max_fetches: a whole number of at least 1, else WEB_CAP (a malformed
    value never raises the cap)."""
    web: Any = getattr(cfg, "web", None) or {}
    value: Any = cast(dict[str, Any], web).get(key) if isinstance(web, dict) else None
    return value if type(value) is int and value >= 1 else WEB_CAP


def count_web_call(cfg: Any, session_id: str, kind: str) -> int:
    """Record one web call of this session and return the session's count of that kind, this one included.
    Raises when the count cannot be kept (the caller denies)."""
    from . import safeio
    listed: list[Any] = list(getattr(cfg, "library_roots", None) or [])
    roots = [Path(str(r)) for r in listed]
    target = safeio.plugin_data_path(COUNTER_NAME, roots)
    if target is None:
        if cfg is None:
            raise RuntimeError("no plugin data folder and no site to keep the web-call count (G8.2)")
        target = safeio.check(cfg, cfg.log_dir / COUNTER_NAME)
    session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return safeio.tally_at(target, f"{session} {kind}")


def _over_cap(cfg: Any, payload: dict[str, Any], tool: str) -> str | None:
    """G8.2: the deny reason when this session has used its web searches or fetches, else None. A call without a
    session_id (not from Claude Code, which always sends one) is not counted."""
    session = payload.get("session_id")
    if not isinstance(session, str) or not session.strip():
        return None
    kind, key = CAPPED[tool]
    cap = web_cap(cfg, key)
    n = count_web_call(cfg, session, kind)
    if n <= cap:
        return None
    _log(cfg, {"cmd": "hook.webcap", "tool": tool, "verdict": "BLOCKED", "count": n, "cap": cap,
               "agent_id": payload.get("agent_id")})
    return (f"ERLA web gate (G8.2): this web {kind} was not sent: this session has made its {cap} web {kind}es, the "
            "cap for one request. Answer from the library and the pages already found, report what is still "
            f"missing as a gap, or ask the user to raise web.{key} in the site config (only the user can).")


def decide(payload: dict[str, Any]) -> str | None:
    """The deny reason for this hook call, or None to let it pass."""
    from .classify import shadow_library, web_allow_terms, web_query_check
    from .config import ConfigError, ConfigFormatError, load_config

    tool = str(payload.get("tool_name") or "")
    tool_input: dict[str, Any] = payload.get("tool_input") or {}
    # the launcher's fast path: the same test, so both agree. A GATED name the parser does not know never takes it.
    if (tool not in GATED or tool in shellgate.KNOWN_TOOLS) and shellgate.fast_allow(tool, tool_input):
        return None

    try:
        cfg = load_config()
    except ConfigFormatError:
        raise                            # a malformed site config is not "no library": blocked by main()
    except ConfigError:
        cfg = None                       # no library on this computer: patterns only

    shell = shellgate.shell_command(tool, tool_input)
    if shell is not None:
        cmd, ps = shell
        cwd = payload.get("cwd")
        analysis = shellgate.analyse(cmd, cwd if isinstance(cwd, str) and cwd else None, ps=ps)
        if not analysis.empty:
            items = protected_paths(cfg)
            save_protected_cache(items)
            reason = shellgate.write_reason(analysis, shellgate.Protected(items), cmd)
            if reason:
                _log(cfg, {"cmd": "hook.writecheck", "tool": tool, "verdict": "BLOCKED",
                           "agent_id": payload.get("agent_id")})
                return reason

    text = outbound_text(tool, tool_input)
    if text is None or not text.strip():
        return None
    what = {"WebSearch": "search query", "WebFetch": "URL"}.get(tool, "command or browser request")
    shadow = shadow_library(text)
    if shadow:
        _log(cfg, {"cmd": "hook.webcheck", "tool": tool, "verdict": "BLOCKED", "gate": "G5.4",
                   "agent_id": payload.get("agent_id")})
        return (f"ERLA web gate: this {what} was not sent: unofficial copy source (G5.4) - it names "
                f"'{shadow}', a site that serves pirated copies of standards and books. Use only official, "
                "publisher, government or open-access sources; if none has the document, report it as a gap "
                "(or access-restricted) instead.")
    con = None
    if cfg is not None:
        from . import store
        con = store.connect(cfg.index_path)   # read-only; a missing index reads as empty
    try:
        reasons = web_query_check(text, con, web_allow_terms(cfg))
    finally:
        if con is not None:
            con.close()

    _log(cfg, {"cmd": "hook.webcheck", "tool": tool, "verdict": "BLOCKED" if reasons else "SAFE",
               "n_reasons": len(reasons), "agent_id": payload.get("agent_id")})

    if not reasons:
        return _over_cap(cfg, payload, tool) if tool in CAPPED else None
    return (f"ERLA web gate (G2): this {what} was not sent because it {'; '.join(reasons)}. "
            "Rewrite it with generic technical terms only (no part numbers, program or customer names, "
            "emails, or file names and titles of restricted documents) and try again; if the term is "
            "essential, answer from the local library and report the gap.")


def _log(cfg: Any, record: dict[str, Any]) -> None:
    if cfg is None:
        return
    try:
        from . import report
        report.append_log(cfg, record)
    except Exception:
        pass                             # the audit line is best effort; the decision stands


def deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    dst = stdout if stdout is not None else sys.stdout
    try:
        # the hook JSON is UTF-8; a pipe's default text encoding on Windows is the ANSI code page, which turned
        # "Zéphyrine" into "ZÃ©phyrine" before the check
        src = stdin if stdin is not None else (
            io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace") if sys.stdin else None)
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
