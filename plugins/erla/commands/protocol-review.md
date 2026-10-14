---
description: Run the ERLA protocol review (rules section 9, workflow W6, skill S16)
argument-hint: [full | event-triggered | <files to review>]
---

Before anything else, check that ERLA is reachable from this chat (ERLA's server runs on the user's computer):
- Look for the erla tools (`site_info`, `library_search`, …). Names may carry a prefix such as `mcp__remote-devices__plugin_erla_erla__` and may be deferred, so search the tool list before deciding they are absent.
- Absent, and this chat has no computer-link tools (`mcp__remote-devices__*`) → the chat is not linked to the computer. Say so in one line: open this chat in the Claude desktop app on the computer with ERLA and choose "Link to this computer" (or start a new chat from the desktop app with that computer selected), and keep the app running and the computer awake. Stop there; do not suggest `/erla:setup`, which cannot fix a missing link.
- `site_info` fails because the computer is offline, asleep or not responding → say so and ask the user to wake it and open the Claude desktop app.
- Linked (or local) but the erla tools are absent or `site_info` errors → check the ERLA plugin is enabled (Settings → Plugins), then point to `/erla:setup`.
- Never answer library questions from memory.

Use the `erla` subagent to run workflow W6 (rules §9, skill S16). Scope: $ARGUMENTS (default: full review if the scheduled review is due, otherwise the documents affected by open triggers; state which).
1. Gather evidence with `protocol_status`, `site_info`, `library_stats` and `run_eval` (compare with the last recorded run; an MRR or Recall@10 drop > 0.05 is a finding).
2. Review the protocol resources (`erla://protocols/*`, including MCP_MODE) and `erla://site/notes` against the S16 checklist, including that the tool table in MCP_MODE matches the server's tools and that the plugin commands (`/erla:find`, `/erla:files`, `/erla:index`, `/erla:protocol-review`, `/erla:setup`) match the procedure.
3. External currency checks (standard revisions) only with queries that `web_query_check` returns SAFE; web budget per G8.2.
4. Write the findings (severity CRITICAL / MAJOR / MINOR, evidence) and the proposed before/after changes with `export_packet` (name `protocol_review_<YYYY-MM-DD>`); guardrail proposals state what protection changes (G1.5).
5. Present them and ask me to approve, reject or modify each. Make no protocol or configuration change before approval. A check that cannot run is `NOT RUN`; the review is then PARTIAL and the next scheduled date is not advanced.
