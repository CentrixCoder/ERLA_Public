export type Progress = {
  phase: string
  done: number | null
  total: number | null
  ratePerSec: number | null
  etaSec: number | null
  unit: string
  // the last item the log reports finished (OCR prints a file once it is done)
  lastItem: string | null
  // when the count last moved, ms on $.clock: the current item has run since then
  sinceMs: number | null
  // ms on $.clock at the last redraw tick; drives the spinner and the elapsed time
  nowMs: number
}

// what the /erla-library dialog shows: ERLA's library folders and the last action's outcome
export type LibraryView = {
  roots: string[]
  // site_info's library_probe, when the index exists
  filesOnDisk: number | null
  documentsIndexed: number | null
  newFiles: number | null
  missing: number | null
  // the action running now ("Waiting for the folder picker…"), null when idle
  busy: string | null
  message: { text: string; isError: boolean } | null
  // the folder whose Remove was pressed once and waits for the confirming press
  confirmRemove: string | null
  // folders changed since the last index_start from this dialog
  needsIndex: boolean
}

// the /erla control panel's toggles, kept across sessions ($.store "settings")
export type PanelSettings = {
  // 'config': ERLA's own unit_system (library_config.yaml); 'US' / 'SI' override it for answers
  units: 'config' | 'US' | 'SI'
  // library_search hybrid (keyword + embeddings); off = keyword only
  semantic: boolean
  // library_search exclude_flagged: ITAR / EAR / CUI / proprietary documents left out
  excludeRestricted: boolean
  // where answers come from: 'local' the library only (web tools blocked), 'web' the web only
  // (ERLA's library tools blocked), 'both' the library first and the web for gaps (ERLA's default)
  scope: 'local' | 'web' | 'both'
  // library_search re-ranker: 'default' (BGE v2-m3 on the CPU) or a GPU LLM re-ranker (PyTorch + CUDA)
  reranker: 'default' | 'bge-v2-m3' | 'qwen3-4b'
  // the person's own KPI dashboard (an https link, e.g. a claude.ai artifact), opened from the panel's
  // KPI dashboard card; null until they paste one. Never shipped with the plugin: each user's is private
  dashboardUrl: string | null
  // whether the panel shows the KPI dashboard card (off by default; Settings or the card's Hide changes it)
  dashboardShown: boolean
}

// a document opened with library_show this session, for feedback and the reference list
export type Source = {
  docId: number
  title: string
  path: string
  tier: string | null
  flag: string | null
  year: number | null
  ext: string | null
  // pages Claude opened with library_show's locator ("p.41"), in the order first cited
  pages: string[]
  verdict: 'useful' | 'not_useful' | null
}

// one row of the panel's "List files" result (library_files)
export type FileHit = { docId: number; title: string; path: string; tier: string | null; flag: string | null }

// one bar of the panel's relevance chart: a library_search hit and its re-ranker relevance
export type RankedHit = {
  docId: number
  title: string
  tier: string | null
  flag: string | null
  // the re-ranker's 0-1 match of the passage to the question; null when the hit was not re-ranked
  relevance: number | null
}

// the last library_search Claude ran, top 10 in ERLA's order (tier first within the relevant set, R1.1)
export type LastSearch = {
  query: string
  // false when no hit carries a re-ranker score (keyword-only search, re-ranker off or failed)
  ranked: boolean
  hits: RankedHit[]
}

// library_stats' last_eval: the latest golden-set run, for the search-quality tile
export type EvalSummary = {
  ndcg10: number | null
  questions: number
  date: string | null
  reranker: string | null
  // golden questions needed before the score means anything (S14)
  minQuestions: number
}

// one of the person's claude.ai artifacts, offered by the KPI dashboard card's Find my dashboard
export type DashboardChoice = { title: string; url: string }

export type PanelView = {
  // library_stats, parsed
  indexState: string | null
  documents: number | null
  ocrPending: number | null
  vectors: number | null
  configUnits: string | null
  // site_info check_policy: when the Professor checks a Result Packet; shown, never set, by the panel
  checkPolicy: string | null
  // the running ERLA server's version (site_info erla_version), shown in the header
  version: string | null
  // this plugin copy's version (its plugin.json), shown when the server cannot say
  pluginVersion: string | null
  // set while the erla server is the install stub (only erla_setup_status): its report,
  // and whether the install has finished and only a reconnect is missing
  setup: { text: string; done: boolean } | null
  // library_stats last_eval, parsed; null when no eval has been recorded
  lastEval: EvalSummary | null
  query: string
  files: FileHit[] | null
  // Find my dashboard's artifacts to pick from, when it could not pick one by itself
  dashboardChoices: DashboardChoice[] | null
  busy: string | null
  message: { text: string; isError: boolean } | null
  // the Button press the panel is showing on that Button, null when none
  pressed: Pressed | null
}

// a press shown on its own Button: 'busy' while its work runs (⏳ and a short word), then 'done' (✓) or
// 'failed' (✗) with the outcome for a moment; `seq` tells one press from the next, so an older press's
// timer never clears a newer one
export type Pressed = { key: string; phase: 'busy' | 'done' | 'failed'; text: string; seq: number }

declare module 'claude-code' {
  interface PluginState {
    'erla': {
      live: Progress | null
      isHidden: boolean
      library: LibraryView | null
      panel: PanelView | null
      settings: PanelSettings
      sources: Source[]
      lastSearch: LastSearch | null
    }
  }
}
