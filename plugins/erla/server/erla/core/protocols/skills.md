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
**Failure:** corrupt, encrypted or DRM-protected file → log `extract_failed` with reason (for a DRM-protected EPUB, the scheme; ERLA never removes DRM); continue.

## S2. Content Extraction

| Type | Library | Chunking | Locator |
|---|---|---|---|
| PDF (text) | PyMuPDF | per page; split > 1500 tokens on paragraph | `p.<n>` (printed page label if available, else physical) |
| PDF tables (A2) | material-property pages (Ftu/Fty + ksi/MPa): geometry grid from word positions (data columns from the widest numeric row; spanning headers and multi-line header cells kept); other numeric pages: PyMuPDF ruled tables, merged cells rebuilt, chart grids rejected | one markdown chunk per table, caption first; added beside the page chunk | `p.<n> table <k>` |
| PDF (scanned) | PyMuPDF → triage: blank page skipped (36 dpi ink test); Tesseract 200 dpi psm 3; tables / mean confidence < 70 re-run at 300 dpi psm 6 | per page | `p.<n> [OCR]` |
| DOCX | python-docx package + WordprocessingML reader (`word.py`) | by heading section (heading lines first in the chunk; Word's automatic heading numbers included; a heading with no body is its own chunk); tables as separate chunks (markdown, caption first); footnotes, endnotes, headers and footers as their own chunks | `§<heading path>` / `table <n>` (`table <n>.<k>` nested) / `footnotes` / `endnotes` / `headers and footers` |
| DOC (legacy) | with MS Word (COM, read-only, macros off): a `.docx` copy in `logs/doc_copy/` (deleted after reading), read as DOCX; without Word, or when the copy fails: Office-free parser (olefile) → text | with Word: as DOCX; otherwise by paragraph window | with Word: as DOCX; otherwise `para <a>-<b>` |
| XLSX/XLSM/CSV | openpyxl (data_only=True, no macros) / csv | per sheet; detect header rows; blocks ≤ 200 rows | `<Sheet>!<A1>:<H40>` |
| XLS (legacy) | xlrd | per sheet; blocks ≤ 200 rows | `<Sheet>!<A1>:<H40>` |
| PPTX | python-pptx | per slide (title + body + table + notes) | `slide <n>` |
| PPT (legacy) | MS PowerPoint via COM (pywin32), read-only, no window | per slide | `slide <n>` |
| EPUB 2/3 | stdlib: zipfile, OPF spine, XHTML parser | spine reading order; per printed page where the book has a page-list (EPUB 3 nav page-list, EPUB 2 NCX pageList), else per heading section of each spine item; tables as markdown rows in the chunk; code blocks verbatim; split > 1500 tokens on paragraph | `p.<printed page>`; `item <n> §<heading>` / `item <n>` (n = spine position, 1-based); a repeated locator gets `(2)`, a split one `part <k>` |
| TXT/MD/JSON | stdlib | ~1000-token windows, 15% overlap | `line <a>-<b>` |
| `.f06` | regex header scan | metadata + message summary + section index | `line <n>` |
| `.bdf/.dat` | regex (SOL, CEND, SUBCASE, element card counts) | metadata only | `line <n>` |
| `.op2` | pyNastran (optional) | metadata: subcases, result types | `subcase <id>` |
| Images (png/jpg/tif) | Tesseract (optional) | whole image | `image [OCR]` |

Rules: detect scanned PDF when extractable text < 50 chars/page on > 50% of pages. Preserve table structure as markdown. Capture units appearing in headers. Chunks carry quality flags without altering text: OCR mean word confidence, numeric tokens below `ocr.low_conf_numeric` (`OCR_LOW_CONF`), letter/digit confusions (`OCR_CONFUSION`), and table-reconstruction notes; `erla show` prints them as `!!` lines (effects: rules §2). Known limit: MIL-HDBK-5H allowables tables (borderless text columns) are not reconstructed; their text remains searchable. COM extraction runs in a single worker with a per-file timeout; a hung Office process is killed and the file logged as `extract_failed`. EPUB: metadata (title, authors, publisher, date, ISBN) comes from the OPF package. A printed page is used only for spine items the page-list reaches. It is never carried into an item the page-list does not cover; that item is cited as `item <n>`. A book whose content documents are encrypted (DRM, META-INF/encryption.xml; font obfuscation does not count) is logged `extract_failed` with the scheme named and is never decrypted: ask the user for a DRM-free copy. A damaged archive or package fails closed. An unreadable spine item leaves a marker chunk at `item <n>` saying its text is not indexed, so a "not found in this book" answer must name the unread items. Text decoded with replacement characters carries a quality warning (`!!` line; `verify_packet` returns it). A locator opens the chunk with exactly that locator, else the chunks whose locator it begins at a word boundary (`p.6` opens `p.6 part 2` and `p.6 table 1`, never `p.60`; `item 1` never opens `item 12`). A blank locator opens nothing. `library_show`, `erla show` and `verify_packet` apply the same rule. DOCX: text is read as Word shows it with revisions accepted (tracked insertions kept, deletions dropped). Superscript exponents are written `^` (`E, 10^3 ksi`, `in^2`); a footnote mark in the body is `[n]` and its text is in the `footnotes` chunk. Symbol-font characters are mapped to Unicode; one that cannot be mapped is shown as the replacement character U+FFFD with a warning. Equations are linearised (`(a)/(b)`, `x^2`, `sqrt(x)`); old equation objects are shown as `[equation object]` and are not readable: open the file. Tracked changes, unmapped symbols and equation objects are `!!` lines on the chunk (effects: rules §2).

## S3. Metadata Inference

Extract: title, authors, year, standard number + revision (regex library for ASTM/ASME/SAE/AMS/MIL/ISO/EN/API/NASA/ESDU patterns), document type (standard, handbook, textbook, paper, report, datasheet, analysis, test, presentation, solver output), discipline tags (structures, fatigue, materials, thermal, dynamics, fluids, joints, welding, composites). Top-level library folder names are used as a discipline hint.

## S4. Classification & Flagging

- Path rules from config (e.g., `**/ITAR/**` → ITAR).
- In-document marking regex on first/last 2 pages: `ITAR`, `EXPORT CONTROLLED`, `EAR99`, `ECCN`, `CUI`, `PROPRIETARY`, `COMPANY CONFIDENTIAL`, distribution statements B–F.
- Authority tier assignment from doc type + publisher + `source_authority.yaml`.
- Tier precedence (K3), highest first: (1) caps from `tier_caps` always win: solver files and calculation tools T5 (T3 under a signed-report path), analysis and test documents no better than T3 (cap patterns match the path only, so template-derived titles do not demote company methods manuals); (2) `tier_patterns.T1` standard/spec identifiers lift standards and handbooks to T1; (3) `tier_patterns.T2` authoritative references lift T3-T6 to T2; (4) folder default (`folder_hints`), publisher pattern, then default by document type. `erla reclassify [--dry-run]` re-applies these rules to already-indexed documents (index-only write; a dry run writes `out/reclassify_dryrun_<date>.csv`).

## S5. Query Expansion

Sources: `synonyms.yaml` (abbreviations, notation, material designation aliases, standard renumbering), plus LLM-generated variants restricted to technical synonyms. Output the expanded set. Never add terms from flagged documents to web queries.

LLM-generated variants and the parts of a multi-part question go to `library_search` as `queries` (fused by document RRF, then re-ranked against the original question). Compound terms match their parts (`MMPDS` finds `MMPDS-17`; `high-strength` finds "high strength").

## S6. Hybrid Search & Ranking

1. Keyword scoring: exact BM25F (`erla.bm25`) over EVERY chunk that contains a query phrase, cut to `search.bm25.depth` (600) only after scoring. Per phrase q and chunk d: tf~ = Σ_f w_f · tf_f / (1 − b_f + b_f · len_f / avglen_f) over the fields text, title (title + file stem) and path (library path); score = IDF(q) · tf~ · (k1 + 1) / (k1 + tf~); IDF(q) = ln(1 + (N − n(q) + 0.5) / (n(q) + 0.5)) (Lucene); a chunk scores the sum over the query phrases. With one field this is IDF · TF · (k1 + 1) / (TF + k1 · (1 − b + b · len / avglen)). N, lengths and averages come from the live rows. Settings `search.bm25`: `k1` (1.2), `b` per field (0.75), `weights` (text 1, title 2.5, path 0.5), `idf` (lucene); fitted with `scripts/fit_ranker.py --bm25` (leave-one-out, per query class). Concept groups: OR within a synonym group (each variant a phrase), AND across groups; AND chunks first, OR-only chunks when fewer than k. The full-text index (version 2) strips sentence-final periods and edge hyphens and also indexes the parts of compound tokens. An index without the vocabulary table (before its next refresh) uses SQLite's bm25() with LIMIT 600 and says `+bm25_fts5` in the match mode.
2. Vector similarity (local embeddings) if enabled: each chunk is embedded as overlapping windows (`embeddings.window_chars` 1500, `window_overlap` 200) that start with the document title, standard number and locator (`embeddings.contextual`); a chunk scores by its best window. Exact faiss inner-product index (`embeddings.ann: flat`; `hnsw` optional, numpy fallback). Chunks of duplicate documents are not embedded. A vector index built with another model is not searched (`+vector_stale`). Optional third channel: learned sparse vectors (`sparse.enabled`, local BM42 by default).
3. Fuse with reciprocal rank fusion (k=60).
4. Boosts: tier, exact standard-number match, numeric-table when the query asks for a value, OCR penalty, feedback from log.
4a. Source memory: documents that verified answers on the same topic (`verify_packet` PASS, kept in `logs/source_memory.jsonl` by content hash) get at most +30 % on their fused score. The memory only re-weights documents the search already found; it never adds one, never undoes a filter (tier, type, G2), and an edited or replaced file loses its memory. `erla memory list|forget` shows or removes entries.
5. Cross-encoder re-rank (A1): a local model scores the top `rerank.top_n` documents on 2 passages each, centred on the query terms. Each passage starts with the document title (`rerank.title_in_passage`). Re-rankers: `ms-marco` (ms-marco MiniLM, CPU, fast; the default), `bge-v2-m3` (BAAI bge-reranker-v2-m3, int8, CPU, about 5x slower) and `qwen3-4b` (Qwen3-Reranker-4B, NVIDIA GPU with PyTorch + CUDA), chosen by the user in the control panel; `reranker_status` says whether each can run here and how to fix it; a GPU re-ranker that cannot run falls back to the default and the match mode says so. Relevance = sigmoid(logit / temperature) (4.0). The prior (the step-4 and 4a multipliers) enters as `rerank.prior_mode`: `power` (score = 100 × relevance × prior^`rerank.boost_power`, 1.5 unless the site sets it; the default), `bounded` (the prior can move a score by at most `exp(prior_clip_high)` up and `exp(prior_clip_low)` down) or `additive` (100 × (relevance + `prior_lambda` × ln prior)); `scripts/fit_ranker.py` chooses among them on the golden set, so tier order (R1.1) still applies. `--no-rerank` disables it; if the model is unavailable the fused order is used and the match mode says `rerank_failed`. Relevance scores appear in search output, with `primary` and `corroborating` (R1.1).
6. Filters: type, tier, year, discipline, flag.
7. Profiles (`intent`): `property` (tier_first), `research` (editions of one standard folded into one hit, `editions`), `locate` (no re-ranking); the site may override any setting per profile under `search.profiles.<intent>`, including the re-ranker. A profile never picks a re-ranker unless the site config says so.
8. Evidence: every search returns `evidence` (STRONG / PARTIAL / WEAK / NONE, next step) from the share of the question's concept units printed in the top hit and the top re-ranker relevance; thresholds `search.evidence` (unfitted defaults 0.6 / 0.35 relevance, 0.8 / 0.5 coverage). Hits list `cites`: library documents that are references the hit cites.

## S7. Value Extraction from Tables & Charts

- Tables: identify row/column headers; record value with both headers and units; capture footnotes that change basis.
- Property store (A3): table chunks (PDF A2 tables, DOCX tables, EPUB tables, spreadsheet blocks) are parsed into records of Ftu, Fty, Fcy, Fsu, Fbru, Fbry, E, Ec, G, µ, elongation, density, KIc with value as printed, parsed value, units (a printed multiplier such as "E, 10³ ksi" is kept in the units), SI value, material, temper, form, thickness, direction, temperature, basis (column, label override, footnote), row/column labels, caption, flags and locator. Layouts: rows = properties (MMPDS/MIL-HDBK-5) and columns = properties (spreadsheets). Values without printed units are not stored (R4.1). Query with `erla props` / `library_properties`. **The store is an index, not a source**: open each row's locator before citing (R3.3); flags affect confidence per rules §2.
- Units (A4): `erla convert` / `unit_convert` convert with the factor reported (R4.6); dimension checks flag `UNIT_DIM`; plausibility ranges per quantity flag `RANGE`.
- Charts: do not read a chart unless the user asks, and never when the page prints the curve's equation or a table (use that; a reading may only cross-check it). If asked, use `chart_read`. It reads one curve, identified by colour, at one x, and returns READ with the value and its uncertainty only when every gate passes: axis calibration from tick labels read with high OCR confidence, unambiguous linear/log scale, no rotated scan, no extrapolation beyond the curve, one clean stroke at the point, uncertainty small against the value, never between curves or within a family drawn in one colour, and the reading plotted back onto the figure lands on the curve. Otherwise it returns REJECTED with the gate, the reason and other documents carrying the same figure. Report a reading as `CHART-READ`, confidence LOW, with the figure, x, colour and uncertainty, verified as a `chart` item.

## S8. Web Research

- Domain priority from `source_authority.yaml`.
- Specialized repositories: NASA NTRS, DTIC, NIST, OSTI, arXiv (engineering), university repositories, standards-body catalogs (ASTM, ASME, SAE, ISO, ANSI webstore) for revision status.
- Every web value: URL, page title, publisher, date retrieved, section/table locator.

## S9. Revision & Supersession Check

Maintain known renumberings (e.g., MIL-HDBK-5 → MMPDS; ASME Section VIII Div 1 editions). For each standard cited: local revision vs current revision from publisher catalog. Output: CURRENT / SUPERSEDED (by X) / UNKNOWN.

`library_refs` (`erla refs <reference>`) lists the library documents that ARE a reference (every edition or revision held) and the documents that cite it, with the edition each cites. A reference is a standard, specification or handbook number (MMPDS, MIL-HDBK/STD/PRF/DTL, ASTM, AMS, SAE incl. ARP/AIR/AS, ISO, EN, DIN, VDI, BS, JIS, ANSI, IEEE, ECSS, NACE, API, ASME, NASA, ESDU, CMH-17, AWS, FED-STD, QQ, NAS/NASM/MS/AN hardware); a regulation (14 CFR / FAR, FAA AC, EASA CS, RTCA DO, DOT/FAA reports); a reference book by name with its edition ("Peterson, 3rd ed.", "Roark's Formulas …, 7th Edition"; names from synonyms.yaml `references:` and `refs.references`, and any "<Name>, <n>th ed." citation); a company, process, material or drawing specification ("Process Specification PS-10234", "XYZ-SPEC-0042", BMS, BAC, AIPS); and the site's own numbering schemes (`refs.patterns`). Use it to find every local edition before the publisher-catalog check, and to find what in the library depends on a superseded edition. A citation it does not recognise (a numbering scheme not configured) is found by `library_search` and checked by hand; add the scheme to `refs.patterns`.

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

Golden items may add `expect_locator` (the locator(s) that print the answer; scored as locator hit@5), `class` (property / research, for per-class nDCG) and `intent`. `citation_valid` checks that the expected value is printed at the first relevant hit's cited locators, not merely somewhere in the document. `--intent <profile>|item` searches every query with a profile, or each with its own. Each run reports how each evidence verdict did (`evidence_calibration`: queries per verdict and how many had a right document in the top 5), so the `search.evidence` thresholds can be set from data. Items with `status: unreviewed` are skipped; `erla memory golden` writes such candidates from verified answers (the logged question, the source and the locator) for the owner to grade. `scripts/fit_ranker.py` also fits `rerank.prior_mode` and `rerank.temperature`, and with `--bm25` the keyword stage's `search.bm25` k1, b per field and field weights against SQLite's bm25() as the baseline, all leave-one-out and per class. `scripts/embed_trial.py` compares embedding models on a copy of the index under `out/`.

## S15. Feedback Learning

Feedback (`record_feedback`, `erla feedback`) stores the document's content hash and, when the question is given (`query`), its topic terms. A vote follows the file (an edited file loses it; a re-read file keeps it) and counts only for queries covering 60 % of its topic; a vote without a question counts for every query. ±5 % per vote, clamped to [0.75, 1.25]. Votes logged before 0.7.0 (row id only) count only while that index row is unchanged since the vote. Propose synonym additions from the log. Changes to config require user approval.


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
9. Model currency — the Professor's model aliases (`opus`, `fable`) still name the newest model families on Anthropic's model list; a new family that replaces one is proposed for an ERLA release; the model the checks run on (`check_status` `model_id`) has a passing calibration in `logs/professor_calibration.jsonl`, or `scripts/professor_eval.py` is run on the seeded-error set (open `model_change` triggers).
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

## S19. Independent Check (Professor)

**Purpose:** a second, independent look at a draft Result Packet before the user sees it.
**Who:** the `professor` agent on Opus 5.5: read-only (library tools, `verify_packet`, `protocol_section`; in the full edition also the calculation tools), no web, no writers, no agents; re-run on Fable only when a check does not converge (W9 step 4a).
**When:** the user's check policy (`erla check-policy`; `site_info.check_policy`) or `/erla:check`.
**How:** W9: a two-part Check Brief, blind estimates before ERLA's values, sources re-opened, sanity checks, S0 checklist, verdict PASS / PASS WITH FINDINGS / FAIL, packet §G; for margins, design values and programs, the coverage audit (W9 step 3a) first.
**Limits:** an AI check, not a certification (G6.1). Phase 3 of ADR-0002 moves the blind estimate into code.
**Measured by:** the seeded-error set (catch rate on planted faults, false alarms on correct packets).

## S20. Coverage Inventory

**Purpose:** know, before the work, everything that can make a margin, check, design value or program incomplete, and show what is not checked.
**When:** at the start of any calculation, analysis, report or program that reports margins or design values (R4.7), and again whenever a configuration is added (a section shape, a load type, an option).
**How:** take the discipline's failure-mode catalogue (its skill file) and the governing sources; write one row per configuration and mode with its source and status (COMPUTED / NOT CHECKED / REQUIRED INPUT); for a program, keep it as a machine-readable file with a test and show the NOT CHECKED rows to the user with every result they affect; send it with the Check Brief (W9 step 3a). A lesson or a finding that names a missing row adds the row at once and is promoted at the next review (R9.3).
**Limits:** the inventory is as complete as the catalogue and the sources; the Professor's audit and the owner's review are the check on it.
**Measured by:** seeded omission cases in the Professor's calibration set (a draft that leaves out a credible mode must be caught).
