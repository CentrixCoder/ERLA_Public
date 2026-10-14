# ERLA - Engineering Research Library Agent

Finds, extracts, verifies and cites engineering data from this computer's engineering library.

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) on PATH (it supplies Python 3.11+).
Run `/erla:setup`. It installs ERLA's Python runtime, checks the library folder(s), and starts the
first index.

MIT licensed: https://github.com/CentrixCoder/ERLA_Public. ERLA does not certify designs; check every value
against its cited source.

## Control panel: `/erla` (0.2.9)

`/erla` opens one pane above the message box. Every control is also reachable by command or chat.

| Card | What it does |
|---|---|
| Status | Index state, documents, OCR pending, semantic search built or not, the running job. *Refresh index*, *Refresh status*. |
| Search | *Ask ERLA* sends your question to Claude as if you typed it; *List files* lists matching library files in the panel. |
| Library folders | Add a folder (path or *Browse…*), remove one (second press confirms), file counts, *Index changes*. ERLA only reads these folders. |
| Settings | **Search in**: library + web (default), library only (web tools blocked), web only (library tools blocked). Units in answers, semantic search, leave out restricted documents. Kept across sessions. |
| Sources this session | Documents Claude opened, pages cited, **Open** (PDF viewer at the cited page), 👍 / 👎 feedback, *Export references*. |

The panel's buttons and the progress bar call ERLA's server themselves, and Claude Code checks those
calls like Claude's own. Without allow rules, auto mode refuses them, and the other modes ask every time
(*Always allow* does not save a rule for these calls). A plugin cannot grant itself the rules, so
`/erla:setup` offers to add them to your user settings, and adds them only when you say yes. Run it
outside auto mode, because auto mode also refuses Claude editing its own permissions. The rules are
`mcp__plugin_erla_erla__` followed by `site_info`, `library_stats`, `library_files`, `index_status`,
`index_start`, `add_library_folders`, `remove_library_folder`, `record_feedback` and `export_packet`.
A refused call names the rule it needs.

## Indexing progress bar (0.2.6)

While an ERLA index job runs, a progress bar appears above the message box on its own:

```
ERLA indexing · Extracting text (step 1/4)
██████████████████░░░░░░░░░░░░░░░░░░░░░░ 44%  1,600/3,654 files · 3.1 files/s · ETA 11 min
```

- Appears within seconds when a job starts (first index, `index_start`, adding a library
  folder, re-survey), and also picks up jobs already running when a session opens.
- One bar per stage: text extraction, OCR of scanned PDFs, table/property backfill, embeddings.
  Rate and ETA are measured live.
- Disappears when the job ends, with a short "indexing finished" notice.
- `/erla-progress` hides or shows it; a new job always shows it again.
- Polls ERLA's `index_status` tool every 4 s while a job runs and every 20 s otherwise.
  It reads status only and never starts or changes a job.
