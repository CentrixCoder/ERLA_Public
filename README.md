# ERLA — Engineering Research Library Agent

[![CI](https://github.com/CentrixCoder/ERLA_Public/actions/workflows/ci.yml/badge.svg)](https://github.com/CentrixCoder/ERLA_Public/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.5.1-blue)](https://github.com/CentrixCoder/ERLA_Public/releases/latest)

**Find · Verify · Build.** ERLA is a Claude plugin that brings a growing team of specialized agents
to engineering research. Each agent focuses on a targeted field of engineering, and together they
find, verify and cite engineering data from your own document library and, for gaps, the web.

**[⬇ Download the latest ERLA plugin (erla-plugin.zip)](https://github.com/CentrixCoder/ERLA_Public/releases/latest/download/erla-plugin.zip)**

## What's new

Updated automatically at every release; older versions are listed under
[Releases](https://github.com/CentrixCoder/ERLA_Public/releases).

<!-- release-notes:start -->
### 0.5.1

In short: ERLA never forces an answer.
- **When ERLA and its checker disagree.** The Professor now runs on Claude Opus 5.5. If its result and
  ERLA's still differ by 5% or more, or reach opposite conclusions, after ERLA has fixed what the sources
  support, the check is repeated once on Claude Fable. If they still do not agree, ERLA stops and answers
  "UNRESOLVED - no solution can be established from the available sources", with both results and what
  is missing. Such an answer cannot be logged or exported as a solution.
- **New rule G3.7:** neither ERLA nor its checker ever produces, adjusts or chooses a value or conclusion
  to satisfy a request or to make two results agree.

### 0.5.0

In short: an independent checker now vets ERLA's computed and design answers before you see them.
- **The Professor.** A second ERLA agent checks an answer on its own: it re-opens every source, works out
  each value itself before it sees ERLA's, and runs sanity checks (units, magnitudes, method limits,
  conservative assumptions). Its verdict - PASS, PASS WITH FINDINGS or FAIL - appears in a new
  section G of the Result Packet. It is an AI check, not a certification and not a substitute for the
  checker of record.
- **Checked in code, not on trust.** The checker cannot see ERLA's numbers until it has recorded its
  own; the comparison is computed by code; a verdict that contradicts it is refused. A packet that needs
  a check cannot be logged or exported without one, or without a stated reason it could not run.
- **You decide when it runs.** `erla check-policy auto|always|off` in a terminal (default `auto`:
  answers with computed or design values). Only you can change it; the control panel's Settings card
  shows the current choice. `/erla:check` checks any answer, or your own hand calculation, on request.
- **Sharper value checks.** A table value is confirmed in its row and column, or reported as
  unconfirmed; an honest "not found" answer is no longer logged as unverified.

### 0.4.2

In short: right after an install or update, the control panel tells you what to do instead of showing errors.
- **No reconnect after an install.** When ERLA's runtime finishes installing, its library tools appear in
  the same session by themselves.
- **Setup card.** While the runtime installs, the control panel shows a "Setup" card with the install
  progress and a **Check again** button, and the title shows the plugin's version. Before, every button
  showed a "no connected MCP tool" error. After an update, quit and reopen the Claude app to restart a
  server that was already running.

<!-- release-notes:end -->

## What ERLA does

- **Searches your engineering library.** ERLA indexes the PDFs, Office files and text in the folders
  you choose: handbooks, standards, test and analysis reports, prior FEA runs.
- **Answers with traceable Result Packets.** Every value comes with its source, page locator,
  authority tier and confidence, so you can check it against the original.
- **Built for specialists.** ERLA is a lead agent that plans the research, checks every value and
  issues the answer. In the full ERLA it hands field-specific work to specialist agents (damage
  tolerance and joints first, with more fields being added) and verifies what they return. This free
  version is the lead agent on its own; see [Free version](#free-version).
- **Keeps your library safe.** ERLA only reads your library folders. It never changes, moves or
  deletes anything in them. Documents marked ITAR, EAR, CUI or proprietary stay local and are never
  used in web searches.

## What you need

| Requirement | Notes |
|---|---|
| Claude | The Claude desktop app, claude.ai, or Claude Code, on a plan that supports plugins. |
| [uv](https://docs.astral.sh/uv/) | Starts ERLA and downloads **Python 3.11+** for it if you don't have Python. |
| Windows, macOS or Linux | |
| An engineering library | One or more folders of PDF, Word, Excel, PowerPoint or text files. |
| Optional | [Tesseract](https://github.com/tesseract-ocr/tesseract) for OCR of scanned PDFs; Microsoft Office on Windows for legacy `.doc` / `.ppt` files. |

## Install

### 1. Install uv (once per computer)

| System | Command |
|---|---|
| Windows (PowerShell) | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| macOS / Linux | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

Then fully quit and reopen the Claude app so it finds uv.

### 2. Install ERLA

Pick **one** of these per computer. Two copies of ERLA on the same computer shadow each other.

**Claude app (desktop app or claude.ai)**
1. Download [erla-plugin.zip](https://github.com/CentrixCoder/ERLA_Public/releases/latest/download/erla-plugin.zip).
2. In the Claude app: **Plugins → + Add → Upload**, choose the zip, then **Upload**.
3. On first start ERLA installs its own Python runtime in the background (a few minutes). Ask Claude
   to "check ERLA setup status"; when it reports the runtime installed, turn the ERLA plugin off and
   on (Settings → Plugins).

**Claude Code** (terminal, or the Code tab of the desktop app)
```
/plugin marketplace add CentrixCoder/ERLA_Public
/plugin install erla@erla-tools
```
Then run:
```
/erla:setup
```
Setup installs ERLA's Python runtime (a few minutes the first time). When it asks, turn the ERLA
plugin off and on (desktop app: Settings → Plugins) or run `/mcp` (terminal).

### 3. Choose your library folders

The first time ERLA starts, a folder picker opens (it may be behind other windows). Choose your
library folder; you can add more later by asking "add a library folder", or remove one with
"remove the library folder D:\Old". ERLA reports the folder size and asks before the first index.
A large library can take hours to index, and you can search while it runs.

ERLA keeps its index, settings and logs in your user profile, never inside your library.

## Using ERLA

| Command | What it does |
|---|---|
| `/erla` | Control panel (Claude Code): index status, search, library folders, settings, sources cited this session |
| `/erla:find <question>` | Engineering lookup with cited sources, e.g. `/erla:find 7075-T6 plate Ftu B-basis \| short` |
| `/erla:files <topic>` | Lists the library files related to a topic |
| `/erla:index` | Shows the index state and starts a refresh |
| `/erla:setup` | Installs or repairs ERLA's runtime and library folders |
| `/erla:protocol-review` | Reviews ERLA's protocols |

You can also just ask in plain language, for example "find the Kt for a shoulder fillet, r/d 0.1".

## Free version

This repository publishes the free core of ERLA: the lead agent with its full library search,
verification, guardrails and Result Packets. The specialist agents (damage tolerance, joints and
those still to come) and their calculation tools are not included. When a question falls in a
specialist's field, the free version answers it as the lead agent and says in the Result Packet
that no specialist reviewed it.

## Updating

- **Claude Code:** `/plugin marketplace update erla-tools`, then restart Claude Code or run `/mcp`.
- **Claude app:** download [erla-plugin.zip](https://github.com/CentrixCoder/ERLA_Public/releases/latest/download/erla-plugin.zip)
  again and upload it over the old version.

The download link above always points to the newest release.

<!-- public:begin authority-tiers -->
## Authority tiers (T1 to T6)

Every document in the library and every web page ERLA reads gets an **authority tier**, T1 to T6.
The tier says how much weight a source carries as engineering evidence. T1 is the strongest and T6
the weakest. It is not a judgement of whether a particular value is right: a T1 standard can contain
a misprint, which is why every value is still opened on its page and checked by `verify_packet`.
The definitions are in `rules_and_guidelines.md` §1.

| Tier | Source class | Examples | What ERLA does with it |
|---|---|---|---|
| **T1** | Governing standards, specifications and design-basis data | MMPDS / MIL-HDBK-5, ASTM, SAE AMS, MIL-STD, ISO, EN/Eurocode, ASME BPVC, AWS D1.x, NASA-STD, company design manuals | Design values; confidence can be HIGH |
| **T2** | Authoritative references | Peterson's, Roark's, Shigley, Bruhn, Niu, ASM Handbooks, ESDU, NASA TM/TR/CR, NIST, peer-reviewed journals, solver theory manuals | Design values and methods; confidence can be HIGH |
| **T3** | Validated internal work | Signed analysis and test reports, correlated FEA runs, approved memos | Design values, at most MEDIUM–HIGH confidence; analysis and test documents are never ranked above T3 |
| **T4** | Manufacturer and vendor data | OEM datasheets, catalogue data, application notes | Supporting data, at most MEDIUM; not a design value on its own |
| **T5** | Informal sources | Conference slides, theses, preprints, internal drafts, uncorrelated FEA runs, solver input/output and calculation files | LOW–MEDIUM; reported as a lead or for comparison, not as a design value |
| **T6** | Unvetted web | Forums, blogs, Q&A sites, Wikipedia | A lead only: ERLA follows it to the primary source and cites that, never the T6 page |

How the tiers are used:
- **Ranking:** results are ordered by tier first, then relevance, then date (R1.1).
- **Design values come from T1–T3 only** (R1.3). When only T4–T6 sources exist, the Result Packet
  says so.
- **Confidence follows the tier:** HIGH needs T1–T2 with matching conditions; T3–T4 give MEDIUM;
  T5–T6 give LOW. OCR, chart reading, unit conversion or an unknown revision lowers it one step
  (rules §2).
- **Filtering:** `tier 1,2` in `/erla:find` and `/erla:files`, `--tier T1,T2` on the CLI, or
  `tiers` on the MCP tools.

How a tier is assigned:
- **Library documents** (rules in `source_authority.yaml`):
  1. Start from the folder's tier hint, a known publisher, or the document type: standard T1;
     handbook, textbook or paper T2; test or analysis report T3; datasheet T4; slides, solver files
     and calculations T5.
  2. Raise to T2 when the path or title matches an authoritative reference, and to T1 when a
     standard or handbook matches a standard-ID pattern.
  3. Cap analysis and test documents at T3 and solver and calculation files at T5 (T3 when they sit
     under a signed-report path). Caps always win.
- **Web pages** (`source_tier`, `erla tier <url>`): domains listed in `source_authority.yaml` take
  their listed tier; other `.gov`, `.mil` and `.edu` sites are T2; anything else is T5.
- A site can override the rules with its own copy of `source_authority.yaml` in `<site>/config/`.
  After changing the rules, run `erla reclassify --dry-run` to see the changes, then `erla reclassify`.

The tier is separate from the restricted-document flag (ITAR, EAR, CUI, proprietary). A T1 document
can also be flagged local-only.
<!-- public:end authority-tiers -->

## About this repository

This is the public release of ERLA. Each ERLA release is copied here automatically: `plugins/erla`
holds the plugin's files (used by the Claude Code install), and every
[release](https://github.com/CentrixCoder/ERLA_Public/releases) carries the same files as
`erla-plugin.zip`. Please report problems and ideas in [Issues](https://github.com/CentrixCoder/ERLA_Public/issues);
changes made directly to `plugins/erla` are overwritten by the next release.

## License and disclaimer

ERLA is released under the [MIT License](LICENSE).

ERLA is a research aid. It does not certify designs or replace engineering judgement. Check every
value against its cited source before you use it.
