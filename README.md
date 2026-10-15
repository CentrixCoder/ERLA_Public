# ERLA — Engineering Research Library Agent

[![CI](https://github.com/CentrixCoder/ERLA_Public/actions/workflows/ci.yml/badge.svg)](https://github.com/CentrixCoder/ERLA_Public/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.3.7-blue)](https://github.com/CentrixCoder/ERLA_Public/releases/latest)

**Find · Verify · Build.** ERLA is a Claude plugin that brings a growing team of specialized agents
to engineering research. Each agent focuses on a targeted field of engineering, and together they
find, verify and cite engineering data from your own document library and, for gaps, the web.

**[⬇ Download the latest ERLA plugin (erla-plugin.zip)](https://github.com/CentrixCoder/ERLA_Public/releases/latest/download/erla-plugin.zip)**

## What's new

Updated automatically at every release; older versions are listed under
[Releases](https://github.com/CentrixCoder/ERLA_Public/releases).

<!-- release-notes:start -->
### 0.3.7

- Maintenance release; no user-facing notes were written for this version.

### 0.3.6

- **New name.** ERLA is the Engineering Research Library Agent.
- **First free public release.** The lead ERLA agent with full library search, verification, guardrails
  and Result Packets. When a question falls in a specialist field (damage tolerance, joints and more
  to come), the free version answers it as the lead agent and says in the Result Packet that no
  specialist reviewed it.
- **Stronger protection for restricted documents.** The web gate now blocks names and codes that
  appear only in restricted (ITAR, EAR, CUI, proprietary) documents, including names inside the
  documents, and sees through disguises such as spaced-out letters, look-alike letters, invisible
  characters and encoded URLs. More part-number and email forms are caught.
- **Better prompt-injection reporting.** Instructions hidden in library documents, including polite or
  indirect ones, are reported as anomalies and never followed.
- **Fairer authority tiers.** A document that only mentions a handbook or standard no longer gets that
  document's tier.
- **Honest estimates.** When you ask for an estimate, ERLA works from your inputs or values it verified,
  and never states remembered figures as facts.

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
