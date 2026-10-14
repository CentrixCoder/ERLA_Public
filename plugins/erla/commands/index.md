---
description: Show ERLA index state and start a refresh or re-index when needed
argument-hint: [status | refresh | full]
---

Before anything else, check that ERLA is reachable from this chat (ERLA's server runs on the user's computer):
- Look for the erla tools (`site_info`, `library_search`, …). Names may carry a prefix such as `mcp__remote-devices__plugin_erla_erla__` and may be deferred, so search the tool list before deciding they are absent.
- Absent, and this chat has no computer-link tools (`mcp__remote-devices__*`) → the chat is not linked to the computer. Say so in one line: open this chat in the Claude desktop app on the computer with ERLA and choose "Link to this computer" (or start a new chat from the desktop app with that computer selected), and keep the app running and the computer awake. Stop there; do not suggest `/erla:setup`, which cannot fix a missing link.
- `site_info` fails because the computer is offline, asleep or not responding → say so and ask the user to wake it and open the Claude desktop app.
- Linked (or local) but the erla tools are absent or `site_info` errors → check the ERLA plugin is enabled (Settings → Plugins), then point to `/erla:setup`.
- Never answer library questions from memory.

Report the state of this computer's ERLA index, then act on: $ARGUMENTS (default: status).
1. `site_info` and `library_stats`: library folders, last index date, `index state` (CURRENT / STALE with reasons), files pending OCR, PDFs awaiting table scan, vector coverage, flags. Then `index_status` for any running job.
2. `status` → report only.
3. `refresh` (or STALE with no argument) → if no job is running, call `index_start()` (incremental: index → deferred OCR → table backfill → embeddings). If a job is running, do not start another.
4. `full` → a full re-index can take hours: state the library size, ask me to confirm, and only then call `index_start(full=true, confirm=true)`.
5. The progress bar above the message box shows a running job (`/erla-progress` toggles it); report the starting state once with `index_status` instead of polling.
