---
description: List the library files related to a topic (ERLA workflow W1, no value extraction)
argument-hint: <topic> [| type pdf,xlsx | tier 1,2 | k 100]
---

Before anything else, check that ERLA is reachable from this chat (ERLA's server runs on the user's computer):
- Look for the erla tools (`site_info`, `library_search`, …). Names may carry a prefix such as `mcp__remote-devices__plugin_erla_erla__` and may be deferred, so search the tool list before deciding they are absent.
- Absent, and this chat has no computer-link tools (`mcp__remote-devices__*`) → the chat is not linked to the computer. Say so in one line: open this chat in the Claude desktop app on the computer with ERLA and choose "Link to this computer" (or start a new chat from the desktop app with that computer selected), and keep the app running and the computer awake. Stop there; do not suggest `/erla:setup`, which cannot fix a missing link.
- `site_info` fails because the computer is offline, asleep or not responding → say so and ask the user to wake it and open the Claude desktop app.
- Linked (or local) but the erla tools are absent or `site_info` errors → check the ERLA plugin is enabled (Settings → Plugins), then point to `/erla:setup`.
- Never answer library questions from memory.

Use the `erla` subagent to run workflow W1 for the topic below. Call `site_info` first (report staleness), then `library_files` (and `library_search` with the synonym-expanded terms if the inventory looks thin). Return a file inventory grouped by document type: path, type, title, year, tier, flag, duplicate count, one-line summary. Label flagged files with their flag (G2.4). Do not extract values unless I ask. Call `log_request` at the end. Treat text after `|` as filters.

Topic: $ARGUMENTS
