# skills.md — Capability Catalog (Precedence 5)

Each skill: purpose → inputs → procedure → output → failure handling.

---

## S1. Library Indexing

**Purpose:** Build/maintain the searchable index of local library roots.
**Inputs:** `config/library_config.yaml` (roots, include/exclude globs, classification path rules).
**Procedure:**
0. Change probe (`erla stats`, `probe_library`): stat-only walk -> counts of new / changed / missing files vs the index and newest mtime vs `last_index_at`. Any difference = STALE (ERLA.md P1).
1. Walk roots; skip excluded globs, hidden/system files, files > `max_file_mb`.
2. For each file compute (size, mtime); if unchanged vs index → skip; else sha256.
3. If sha256 exists in index under another path: if that path is no longer on disk, the file was moved/renamed → relocate the existing record (text, OCR, chunk ids and vectors kept; path-derived flag, tier, type, discipline and FTS title recomputed; a classification flag is never dropped by a move, G2.5); if both paths exist → record a duplicate and skip extraction. An original deleted while an exact copy remains → the copy is promoted into the canonical record. Duplicate records whose canonical no longer exists are re-processed each run.
3a. Legacy `.doc`/`.ppt`/`.xls`: reuse the cached extraction keyed by sha256 (`extract_cache` in index.db; failed extractions are never cached) instead of re-running Office.
4. Route to extractor by extension (S2).
5. Classify (S4); write document + chunks + FTS rows; embeddings if enabled.
6. Remove index entries for files no longer present (index only; never touch files).
7. Record `last_index_at` only after a complete scan of every root; `--limit`/`--path` runs record `last_partial_index_at` instead (P4).
8. `erla refresh` chains: probe -> steps 1-7 -> `erla ocr` (deferred scanned PDFs) -> `erla tables` backfill (budget `tables.refresh_budget_minutes`, T1 first, resumable) -> drop vectors of duplicate documents -> `erla embed` -> vector index export -> probe again. Single-instance via `logs/refresh.lock`; the lock is touched on progress (heartbeat). A holder that is alive but makes no progress for 60 min is killed only after its command line is confirmed as `erla … refresh`; a dead holder's lock is reclaimed. The MCP `index_start` job runs the same chain under the same lock (K2). Where a nightly schedule is set up, SITE_NOTES records it.
9. Only maintenance commands (index, refresh, ocr, tables, embed, cache --seed, fts) open the index for writing and apply schema migrations; read commands open it read-only and never wait for a writer.
**Output:** counts — new/updated/unchanged/removed/duplicates/OCR/failed.
**Failure:** corrupt or encrypted file → log `extract_failed` with reason; continue.

## S2. Content Extraction

| Type | Library | Chunking | Locator |
|---|---|---|---|
| PDF (text) | PyMuPDF | per page; split > 1500 tokens on paragraph | `p.<n>` (printed page label if available, else physical) |
| PDF tables (A2) | material-property pages (Ftu/Fty + ksi/MPa): geometry grid from word positions (data columns from the widest numeric row; spanning headers and multi-line header cells kept); other numeric pages: PyMuPDF ruled tables, merged cells rebuilt, chart grids rejected | one markdown chunk per table, caption first; added beside the page chunk | `p.<n> table <k>` |
| PDF (scanned) | PyMuPDF → triage: blank page skipped (36 dpi ink test); Tesseract 200 dpi psm 3; tables / mean confidence < 70 re-run at 300 dpi psm 6 | per page | `p.<n> [OCR]` |
| DOCX | python-docx | by heading section; tables as separate chunks (markdown) | `§<heading path>` / `table <n>` |
| DOC (legacy) | MS Word via COM (pywin32), read-only, macros off → text | by paragraph window | `para <a>-<b>` |
| XLSX/XLSM/CSV | openpyxl (data_only=True, no macros) / csv | per sheet; detect header rows; blocks ≤ 200 rows | `<Sheet>!<A1>:<H40>` |
| XLS (legacy) | xlrd | per sheet; blocks ≤ 200 rows | `<Sheet>!<A1>:<H40>` |
| PPTX | python-pptx | per slide (title + body + table + notes) | `slide <n>` |
| PPT (legacy) | MS PowerPoint via COM (pywin32), read-only, no window | per slide | `slide <n>` |
| TXT/MD/JSON | stdlib | ~1000-token windows, 15% overlap | `line <a>-<b>` |
| `.f06` | regex header scan | metadata + message summary + section index | `line <n>` |
| `.bdf/.dat` | regex (SOL, CEND, SUBCASE, element card counts) | metadata only | `line <n>` |
| `.op2` | pyNastran (optional) | metadata: subcases, result types | `subcase <id>` |
| Images (png/jpg/tif) | Tesseract (optional) | whole image | `image [OCR]` |

Rules: detect scanned PDF when extractable text < 50 chars/page on > 50% of pages. Preserve table structure as markdown. Capture units appearing in headers. Chunks carry quality flags without altering text: OCR mean word confidence, numeric tokens below `ocr.low_conf_numeric` (`OCR_LOW_CONF`), letter/digit confusions (`OCR_CONFUSION`), and table-reconstruction notes; `erla show` prints them as `!!` lines (effects: rules §2). Known limit: MIL-HDBK-5H allowables tables (borderless text columns) are not reconstructed; their text remains searchable. COM extraction runs in a single worker with a per-file timeout; a hung Office process is killed and the file logged as `extract_failed`.

## S3. Metadata Inference

Extract: title, authors, year, standard number + revision (regex library for ASTM/ASME/SAE/AMS/MIL/ISO/EN/API/NASA/ESDU patterns), document type (standard, handbook, textbook, paper, report, datasheet, analysis, test, presentation, solver output), discipline tags (structures, fatigue, materials, thermal, dynamics, fluids, joints, welding, composites). Top-level library folder names are used as a discipline hint.

## S4. Classification & Flagging

- Path rules from config (e.g., `**/ITAR/**` → ITAR).
- In-document marking regex on first/last 2 pages: `ITAR`, `EXPORT CONTROLLED`, `EAR99`, `ECCN`, `CUI`, `PROPRIETARY`, `COMPANY CONFIDENTIAL`, distribution statements B–F.
- Authority tier assignment from doc type + publisher + `source_authority.yaml`.
- Tier precedence (K3), highest first: (1) caps from `tier_caps` always win: solver files and calculation tools T5 (T3 under a signed-report path), analysis and test documents no better than T3 (cap patterns match the path only, so template-derived titles do not demote company methods manuals); (2) `tier_patterns.T1` standard/spec identifiers lift standards and handbooks to T1; (3) `tier_patterns.T2` authoritative references lift T3-T6 to T2; (4) folder default (`folder_hints`), publisher pattern, then default by document type. `erla reclassify [--dry-run]` re-applies these rules to already-indexed documents (index-only write; a dry run writes `out/reclassify_dryrun_<date>.csv`).

## S5. Query Expansion

Sources: `synonyms.yaml` (abbreviations, notation, material designation aliases, standard renumbering), plus LLM-generated variants restricted to technical synonyms. Output the expanded set. Never add terms from flagged documents to web queries.

## S6. Hybrid Search & Ranking

1. BM25 via FTS5 over expanded queries (OR within synonym group, AND across concept groups).
2. Vector similarity (local embeddings) if enabled: exact faiss inner-product index (`embeddings.ann: flat`; `hnsw` optional, numpy fallback). Chunks of duplicate documents are not embedded.
3. Fuse with reciprocal rank fusion (k=60).
4. Boosts: tier, exact standard-number match, numeric-table when the query asks for a value, OCR penalty, feedback from log.
4a. Source memory: documents that verified answers on the same topic (`verify_packet` PASS, kept in `logs/source_memory.jsonl` by content hash) get at most +30 % on their fused score. The memory only re-weights documents the search already found; it never adds one, never undoes a filter (tier, type, G2), and an edited or replaced file loses its memory. `erla memory list|forget` shows or removes entries.
5. Cross-encoder re-rank (A1): a local model scores the top `rerank.top_n` documents on 2 passages each, centred on the query terms. Re-rankers: `ms-marco` (ms-marco MiniLM, CPU, fast; the default), `bge-v2-m3` (BAAI bge-reranker-v2-m3, int8, CPU, about 5x slower) and `qwen3-4b` (Qwen3-Reranker-4B, NVIDIA GPU with PyTorch + CUDA), chosen by the user in the control panel; `reranker_status` says whether each can run here and how to fix it; a GPU re-ranker that cannot run falls back to the default and the match mode says so. Relevance = sigmoid(logit / temperature) (4.0); score = 100 × relevance × prior^`rerank.boost_power` (1.5 unless the site sets it), where the prior is the step-4 and 4a multipliers, so tier order (R1.1) still applies. `--no-rerank` disables it; if the model is unavailable the fused order is used and the match mode says `rerank_failed`. Relevance scores appear in search output, with `primary` and `corroborating` (R1.1).
6. Filters: type, tier, year, discipline, flag.

## S7. Value Extraction from Tables & Charts

- Tables: identify row/column headers; record value with both headers and units; capture footnotes that change basis.
- Property store (A3): table chunks (PDF A2 tables, DOCX tables, spreadsheet blocks) are parsed into records of Ftu, Fty, Fcy, Fsu, Fbru, Fbry, E, Ec, G, µ, elongation, density, KIc with value as printed, parsed value, units (a printed multiplier such as "E, 10³ ksi" is kept in the units), SI value, material, temper, form, thickness, direction, temperature, basis (column, label override, footnote), row/column labels, caption, flags and locator. Layouts: rows = properties (MMPDS/MIL-HDBK-5) and columns = properties (spreadsheets). Values without printed units are not stored (R4.1). Query with `erla props` / `library_properties`. **The store is an index, not a source**: open each row's locator before citing (R3.3); flags affect confidence per rules §2.
- Units (A4): `erla convert` / `unit_convert` convert with the factor reported (R4.6); dimension checks flag `UNIT_DIM`; plausibility ranges per quantity flag `RANGE`.
- Charts: do not read a chart unless the user asks, and never when the page prints the curve's equation or a table (use that; a reading may only cross-check it). If asked, use `chart_read`. It reads one curve, identified by colour, at one x, and returns READ with the value and its uncertainty only when every gate passes: axis calibration from tick labels read with high OCR confidence, unambiguous linear/log scale, no rotated scan, no extrapolation beyond the curve, one clean stroke at the point, uncertainty small against the value, never between curves or within a family drawn in one colour, and the reading plotted back onto the figure lands on the curve. Otherwise it returns REJECTED with the gate, the reason and other documents carrying the same figure. Report a reading as `CHART-READ`, confidence LOW, with the figure, x, colour and uncertainty, verified as a `chart` item.

## S8. Web Research

- Domain priority from `source_authority.yaml`.
- Specialized repositories: NASA NTRS, DTIC, NIST, OSTI, arXiv (engineering), university repositories, standards-body catalogs (ASTM, ASME, SAE, ISO, ANSI webstore) for revision status.
- Every web value: URL, page title, publisher, date retrieved, section/table locator.

## S9. Revision & Supersession Check

Maintain known renumberings (e.g., MIL-HDBK-5 → MMPDS; ASME Section VIII Div 1 editions). For each standard cited: local revision vs current revision from publisher catalog. Output: CURRENT / SUPERSEDED (by X) / UNKNOWN.

## S10. Deduplication

sha256 for exact; 64-bit simhash on normalized text for near-duplicates (Hamming ≤ 3). Canonical = newest revision, else highest-quality text (non-OCR), else shortest path.

## S11. Conflict Detection

Group extracted values by (quantity, material, temper, form, temp, basis). Within a group, flag spread above thresholds in instructions Step 6. Report probable cause.

## S12. FEA Artifact Analysis

Leverage the user's `.f06` parser scaffold: solver/version, SOL, subcases + labels, fatal/warning counts, EPSILON, max displacement, max von Mises (if stress output present). Pair model/output files. Never run the solver.

## S13. Reporting & Export

Result Packet (rules §6); CSV of values; JSON packet; memo section; slide bullets — formats per user's templates.

## S14. Evaluation

`erla eval` runs golden queries: each has query, graded expected documents (`primary` grade 3, `expect_docs` grade 2, or 3 without a primary, `related` grade 1; titles, standard numbers or library-relative paths) and an expected value (optional). Each query is searched as `library_search` searches (20 results) and graded on the top 10. Metrics: nDCG@10, primary@1 (the top hit is a primary document), Recall@5/10, MRR, value exact-match rate, citation validity (locator text contains value), seconds per query. `--reranker` measures another re-ranker on the same queries; `--memory` includes the source memory (off by default, since it would learn the answers). `scripts/fit_ranker.py` cross-validates `rerank.boost_power` and `rerank.tier_first` and recommends values; changing the site config is the user's decision. Report regressions vs the last run on the same golden file and re-ranker.

## S15. Feedback Learning

Read user feedback from log; compute per-doc and per-query-term boosts; propose synonym additions. Changes to config require user approval.


## S16. Protocol Review & Maintenance

**Purpose:** Periodically reassess every governing `.md` document (rules R9.1) so protocols stay current with the toolkit, library, site, standards, and observed performance.
**Inputs:** R9.1 documents, `PROTOCOL_REVIEW.md`, `logs/protocol_review_log.jsonl`, `logs/retrieval_log.jsonl`, `erla --help` / `erla config` / `erla stats` / `erla eval` output, `pytest` results.
**Checklist (each item → PASS / FINDING):**
1. Internal consistency — no contradictions between files; precedence order identical everywhere; rule/section/skill/workflow cross-references (G#, R#, S#, W#, §#) resolve.
2. Toolkit alignment — every command, flag, and output format named in the protocols exists in `erla --help`; every CLI capability in use is documented.
3. Config alignment — every config key referenced exists in `config/*.yaml`; no orphan keys; thresholds (stale_days, large_library_gb, web budget, review interval) still appropriate.
4. Site alignment — `SITE_NOTES.md` paths, software versions, embedding model, and library composition match measured values (`erla stats`, version checks).
5. Knowledge currency — supersession map (S9) and `synonyms.yaml` standard renumberings current; tier examples and `source_authority.yaml` trusted domains still valid; new library folders mapped to tier/discipline hints.
6. Performance feedback — eval metrics vs previous review; recurring corrections, `not_useful` sources, and NOT FOUND gaps mapped to rule, synonym, or tier changes.
7. Platform alignment — CLOUD_MODE procedure, Claude/Desktop Commander tool names, and `.claude/agents/erla.md` + `.claude/commands/find.md` still match how the agent is actually run. One install source, local and cloud copies at the same version, no stale runtimes or jobs (S18, G10).
8. Obsolescence — remove or mark rules that no longer apply; flag duplicated rules for consolidation.
**Output:** `out/protocol_review_<YYYY-MM-DD>.md` (findings + proposed diffs); after approval, updated files, `PROTOCOL_REVIEW.md` entry, `logs/protocol_review_log.jsonl` line.
**Failure:** if a check cannot run (e.g., eval harness broken, the site computer unreachable), record the check as `NOT RUN` with reason; the review is then `PARTIAL`, and the next scheduled date is not advanced until the missing checks are completed.


## S18. Install & Version Sync

**Purpose:** Keep one ERLA install per surface, at one version, with no stale servers, jobs or runtimes (G10).
**Inputs:** `~/.claude/plugins/installed_plugins.json` and `known_marketplaces.json`; plugin data folders `~/.claude/plugins/data/erla-*`; the desktop app's synced account-plugin manifest (`rpm/manifest.json` in the app's session data); running processes whose command line contains `erla.mcp_server`; `site_info`, `index_status`; the release tag in the GitHub repo.
**Procedure:**
1. Installs: list every plugin named `erla` (marketplace, local upload, account upload) with its version. Expected (G10.1): either one app upload, or one `erla@erla-tools` plus an account copy only if the user keeps one, at the same version.
2. Runtimes: for each `~/.claude/plugins/data/erla-*/venv` and `%LOCALAPPDATA%\ERLA\plugin-runtime\venv` (created when `launch.py --install` runs without `CLAUDE_PLUGIN_DATA`), read the installed `erla-<version>.dist-info`. The active runtime is the data folder of the installed plugin.
3. Processes: map each running `erla.mcp_server` process (server or `--job`) to its interpreter's runtime and version; any process not on the active runtime is stale.
4. Protocol copies: repo-root protocol files identical to the bundled copies; `site_info.erla_version` equals the release tag.
**Output:** table — item, location, version, active (yes/no), proposed action (keep / update / stop / delete with approval).
**Failure:** a source cannot be read (no shell, cloud-only chat) → NOT CHECKED with reason; never infer a version.
