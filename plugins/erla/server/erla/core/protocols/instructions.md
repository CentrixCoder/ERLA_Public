# instructions.md — Runtime Workflow (Precedence 4)

Follow these steps for every request. Skip a step only when the step's own skip condition applies; record skipped steps in the log.

## Step 0 — Protocol Currency Check (once per session)

1. Read `PROTOCOL_REVIEW.md` and the tail of `logs/protocol_review_log.jsonl`. Compare `Next scheduled review` with today's date and check for open event triggers (rules R9.3).
2. If overdue or a trigger is open → one-line notice at the top of the first Result Packet and under §F Anomalies; offer to run W6. Continue with the request.
3. If an R9.3 event is observed at any point in the session (e.g., a protocol conflict, a CLI flag that no longer exists) → append `{"type": "trigger_open", ...}` to `logs/protocol_review_log.jsonl` (a `logs/` write, no approval needed) and report it. Do not edit protocol files outside W6.
4. Install check (once per session, before the first search; G10, skill S18): `site_info.erla_version` equals the installed ERLA version (the app upload or `erla@erla-tools`, G10.1), and no ERLA server or `--job` process runs from another runtime. Where the computer's files cannot be read (cloud-only chat), check `site_info` only and record the rest as NOT CHECKED. Any mismatch → one line at the top of the first Result Packet and under §F Anomalies, append `{"type": "trigger_open", "reason": "install_drift", ...}` to `logs/protocol_review_log.jsonl`, and run W8. If the answering server is not the installed version, ask the user to turn the ERLA plugin off and on before searching.

## Step 1 — Intake & Classification

1. Restate the engineering need in one line: task type (design / analysis / correlation / failure investigation / standards compliance / literature review / file locate).
2. Extract structured parameters:
   - Material (designation, temper, form, thickness)
   - Geometry feature (hole, fillet, lug, weld, thread…)
   - Load type (static, fatigue, thermal, dynamic, buckling)
   - Quantities sought (Fty, Ftu, E, Kt, S-N coefficients, equation, allowable…)
   - Governing standard (if named)
   - Solver/FEA context (SOL, element type, model name)
   - Environment (temperature, corrosion, cycles)
3. Classify sensitivity: does the request reference a program, part number, customer, or flagged path? If yes → G2 applies; web queries must be sanitized.
4. If a parameter that changes the answer materially is missing (e.g., temperature for an allowable), list it and proceed with bounded search covering the plausible range. Do not stop unless the request is uninterpretable.

## Step 2 — Search Plan

1. Decide scope: local / web / both (rules R3.1).
2. Build query set:
   - Base query from user terms
   - Synonym/alias expansion from `config/synonyms.yaml`
   - Standard-number variants (e.g., `MMPDS-17`, `MMPDS 17`, `MIL-HDBK-5J`)
   - Material designation variants (`7075-T6`, `7075-T651`, `AA7075`, `UNS A97075`)
3. Set filters: file types, tier, year range, discipline tag.
4. Set budget: default k=20 local hits; web ≤15 searches/15 fetches.

## Step 3 — Local Pass

1. Verify index freshness with `erla stats` (age + library change probe; rule in ERLA.md, P1). If `index state: STALE` -> run `erla refresh` (incremental index -> deferred OCR -> embeddings) and record the new/updated/removed/OCR counts in the packet header. If a refresh/OCR job is already running, search the current index and list what is not yet searchable under section E. Never treat an unknown index date as current.
2. Run `erla search` for each query in the set; merge, dedupe by doc_id, keep max score.
3. For the top N (default 10) hits: `erla show <doc_id> --locator <loc>` and read the surrounding chunk(s).
3a. Material allowables: also run `erla props <quantity> [--material ...] [--basis ...]`. Property rows are pointers, not sources: `erla show` the row's locator and confirm value, units and column conditions for every value used (R3.3); report the `!!` quality lines.
4. Mark each hit: RELEVANT / PARTIAL / NOT RELEVANT, with one-line reason.
5. Extract values per rules §4 into a working table.

## Step 4 — Gap Check

Answer each:
- Is every requested quantity found?
- Is each from T1–T3?
- Is each corroborated?
- Is each standard's revision confirmed current?

If all YES → go to Step 6. Otherwise → Step 5 for the gaps only.

## Step 5 — Web Pass (gaps only)

1. Build sanitized queries (G2.3). Generic technical terms only.
2. Prioritize domains in `config/source_authority.yaml` (standards bodies, .gov, NASA NTRS, DTIC, NIST, university repositories, publisher pages, OEM sites).
3. Fetch pages; extract values with URL + section/table/page locator.
4. For standards: retrieve current revision status from the publisher's catalog page.
5. Do not download paywalled or pirated documents (G5.3/G5.4). Record as access-restricted with full citation.
6. Optionally cache public page text in `web_cache/` with retrieval date.

## Step 6 — Verify & Reconcile

1. Cross-check each value across sources; compute % difference where ≥2 sources exist.
2. Differences > 2% (properties) or > 5% (Kt, fatigue coefficients) → list as conflict with both values; identify likely cause (basis, temper, thickness, revision, units).
3. Check dimensional consistency of every equation and unit conversion.
4. Apply confidence rating (rules §2), including the data-quality flag downgrades; perform unit conversions with `erla convert` and show the factor (R4.6).
5. Flag supersessions.

## Step 7 — Package

1. Produce the Result Packet (rules §6).
2. If the user requested an export, write files to `out/` and list paths.
3. If findings feed an analysis (e.g., Kt for an FEA correlation), add a one-line note on how the value plugs in and its sensitivity (e.g., "Kt range 2.4–2.7 over r/d 0.05–0.07 → ±6% peak stress").

## Step 8 — Log & Learn

1. Append the log line (rules §7).
2. If the user corrects a result, record correction; if the correction reveals a missing synonym, propose an addition to `synonyms.yaml` (do not edit config without approval).

## Special Workflows

### W1. "Find all files related to X" (file locate)
Return a file inventory table: path, type, title, year, tier, flag, duplicate count, one-line summary. Group by document type. No value extraction unless requested.

### W2. FEA run discovery
Search solver files by metadata: SOL, subcase titles, element types, date, model name. Pair `.bdf/.dat` ↔ `.f06` ↔ `.op2` by stem and timestamp. Report fatal/warning message counts from `.f06` (`USER FATAL`, `USER WARNING`, `SYSTEM FATAL`) and EPSILON / max-ratio values if present.

### W3. Standards compliance lookup
Identify governing standard + current revision → locate clause → extract requirement (paraphrase; ≤15-word quote for "shall" text if essential) → list referenced standards the clause depends on.

### W4. Literature review
Build a matrix: source, method (test/analytical/FEA), material, geometry, key result, limitations. Rank by tier and relevance. Identify consensus and disagreement.

### W5. Correlation support
Given an FEA quantity, retrieve the matching closed-form method (Roark case number, Peterson chart ID, equation), its validity range, and inputs required. Hand off to the user's correlation workflow with a populated `CaseInput` parameter list.

### W6. Protocol review (rules §9; skill S16)
Run when the scheduled review is due, an event trigger is open, or the user asks ("run protocol review", `/find protocol review`).
1. Scope: full review (all R9.1 documents) if scheduled; affected documents only if event-triggered — state which.
2. Gather evidence (read-only): `erla --help` and each subcommand's `--help`; `erla config`; `erla stats`; `erla eval` (compare with last recorded run); `pytest -q`; tail of `logs/retrieval_log.jsonl` (corrections, `not_useful` clusters, recurring NOT FOUND gaps); open triggers in `logs/protocol_review_log.jsonl`; installed versions (Python, Tesseract, Office, fastembed model).
3. Check each document against the S16 checklist. For external currency items (standard revisions, trusted-domain availability), run `erla webcheck` before each web query; web budget per G8.2.
4. Write the review report to `out/protocol_review_<YYYY-MM-DD>.md`: findings table (file, section, finding, severity CRITICAL / MAJOR / MINOR, evidence), and a numbered list of proposed changes as before/after diffs with reasons. Guardrail proposals state what protection changes (G1.5).
5. Present the report; ask the user to approve, reject, or modify each proposed change. Make no protocol edit before approval.
6. Apply approved changes per R9.6 (backup → edit → `PROTOCOL_REVIEW.md` update → Project copy sync → `pytest` + `erla eval`).
7. Record per R9.7 and set the next scheduled review date = completion date + `protocol_review.interval_days`.

### W8. Release update & local/cloud sync (skill S18; G10)
Run after every ERLA release, when Step 0.4 finds drift, or when the user asks.
1. Source: the release is merged and tagged in the GitHub repo, and the repo-root protocol files are identical to the bundled copies in `plugins/erla/server/erla/core/protocols/` (`packaging/build.py`).
2. Local: app upload install (G10.1) → upload the release's `erla-plugin-<version>.zip` in the Claude app (Plugins → Add → Upload) in place of the old upload; marketplace install → `claude plugin marketplace update erla-tools`, then `claude plugin update erla@erla-tools` (or `/plugin …` in the Claude Code terminal). Then restart the running server: quit and reopen the Claude app (in a terminal, restart `claude`); `/reload-plugins` and `/mcp` Reconnect do not restart it (G10.5). Confirm `site_info.erla_version` = the release and the release's commands and agents are present.
3. Processes: list running ERLA servers and jobs (S18). Any from another runtime → report; stop it only with the user's approval; if it was an index job, restart with `index_start` from the active version.
4. Cloud: with a marketplace install, if the user keeps a claude.ai account upload for web chats, it is replaced with the same release's `erla-plugin-<version>.zip` and the old upload removed; otherwise confirm no account copy remains. Re-sync the claude.ai Project reference copies (protocol files, `CLOUD_MODE.md`) from the repo.
5. Leftovers: list stale runtimes, data folders and caches (S18); delete only the paths the user approves (G10.4).
6. Record: set `Core protocol version` in `PROTOCOL_REVIEW.md` and append `{"type": "install_sync", ...}` (versions found, actions taken) to `logs/protocol_review_log.jsonl`.
