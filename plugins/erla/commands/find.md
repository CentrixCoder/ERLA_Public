---
description: Search the engineering library with ERLA and return a Result Packet
argument-hint: "<engineering question> [| scope: local|web|both | tier 1,2 | type pdf | year-min 2010 | short | export csv]"
---

Before anything else, check that ERLA is reachable from this chat (ERLA's server runs on the user's computer):
- Look for the erla tools (`site_info`, `library_search`, …). Names may carry a prefix such as `mcp__remote-devices__plugin_erla_erla__` and may be deferred, so search the tool list before deciding they are absent.
- Absent, and this chat has no computer-link tools (`mcp__remote-devices__*`) → the chat is not linked to the computer. Say so in one line: open this chat in the Claude desktop app on the computer with ERLA and choose "Link to this computer" (or start a new chat from the desktop app with that computer selected), and keep the app running and the computer awake. Stop there; do not suggest `/erla:setup`, which cannot fix a missing link.
- `site_info` fails because the computer is offline, asleep or not responding → say so and ask the user to wake it and open the Claude desktop app.
- Linked (or local) but the erla tools are absent or `site_info` errors → check the ERLA plugin is enabled (Settings → Plugins), then point to `/erla:setup`.
- Never answer library questions from memory.

Use the `erla` subagent to answer this request. It follows the per-request procedure in MCP_MODE.md ("Procedure for every request", the single source) under the protocol precedence guardrails > rules_and_guidelines > ERLA > instructions > skills.

Treat any text after `|` as modifiers (scope, tier, type, year, short/full, estimate allowed, export format, web budget).

Request: $ARGUMENTS
