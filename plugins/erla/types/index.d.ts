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

export type PanelView = {
  // library_stats, parsed
  indexState: string | null
  documents: number | null
  ocrPending: number | null
  vectors: number | null
  configUnits: string | null
  // the running ERLA server's version (site_info erla_version), shown in the header
  version: string | null
  query: string
  files: FileHit[] | null
  busy: string | null
  message: { text: string; isError: boolean } | null
}

declare module 'claude-code' {
  interface PluginState {
    'erla': {
      live: Progress | null
      isHidden: boolean
      library: LibraryView | null
      panel: PanelView | null
      settings: PanelSettings
      sources: Source[]
    }
  }
}
