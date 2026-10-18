---
description: Independent check of a Result Packet or of your own calculation - ERLA sends it to its Professor checker and returns the Check Report
argument-hint: "<what to check: a Result Packet, or a calculation with its inputs and sources> [| short]"
---

Before anything else, check that ERLA is reachable from this chat (ERLA's server runs on the user's computer):
- Look for the erla tools (`site_info`, `library_search`, …). Names may carry a prefix such as `mcp__remote-devices__plugin_erla_erla__` and may be deferred, so search the tool list before deciding they are absent.
- Absent, and this chat has no computer-link tools (`mcp__remote-devices__*`) → the chat is not linked to the computer. Say so in one line: open this chat in the Claude desktop app on the computer with ERLA and choose "Link to this computer", and keep the app running and the computer awake. Stop there.
- `site_info` fails because the computer is offline, asleep or not responding → say so and ask the user to wake it and open the Claude desktop app.
- Linked (or local) but the erla tools are absent or `site_info` errors → check the ERLA plugin is enabled (Settings → Plugins), then point to `/erla:setup`.

Send this request to the `erla` subagent (the lead agent). ERLA builds a Check Brief from what the user gave (Part 1: question, inputs with sources, method, quantities to estimate; Part 2: the values and conclusion to check), sends it to the `professor` agent and relays its Check Report to the user unchanged in substance. The check runs whatever the check policy, because the user asked for it. It is an AI check, not a certification.

Request: $ARGUMENTS
