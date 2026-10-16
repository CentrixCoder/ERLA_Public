// Pure helpers for the /erla control panel in register.tsx (the engine follows $ only within
// one file, so the panel's hooks live there).

import type { EvalSummary, FileHit, LastSearch, PanelSettings, PanelView, RankedHit, Source } from '../types'

export const DEFAULT_SETTINGS: PanelSettings = { units: 'config', semantic: true, excludeRestricted: false, scope: 'both', reranker: 'default' }

// the control panel's re-ranker choices; the GPU ones need PyTorch with CUDA (erla rerank.status)
export const RERANKER_OPTIONS = [
  { value: 'default', label: 'ms-marco MiniLM (default, fast)' },
  { value: 'bge-v2-m3', label: 'BGE reranker v2-m3 (CPU, slower)' },
  { value: 'qwen3-4b', label: 'Qwen3-Reranker 4B (GPU)' },
] as const

export function isReranker(v: unknown): v is PanelSettings['reranker'] {
  return RERANKER_OPTIONS.some(o => o.value === v)
}

// the library_search argument the panel's re-ranker choice adds (none for the default)
export function rerankerArgs(s: PanelSettings): { reranker?: string } {
  return s.reranker === 'default' ? {} : { reranker: s.reranker }
}

// a stored value from an older or hand-edited store falls back field by field
export function readSettings(stored: unknown): PanelSettings {
  const s = (stored && typeof stored === 'object' ? stored : {}) as Partial<PanelSettings>
  return {
    units: s.units === 'US' || s.units === 'SI' ? s.units : 'config',
    semantic: typeof s.semantic === 'boolean' ? s.semantic : DEFAULT_SETTINGS.semantic,
    excludeRestricted: typeof s.excludeRestricted === 'boolean' ? s.excludeRestricted : DEFAULT_SETTINGS.excludeRestricted,
    scope: s.scope === 'local' || s.scope === 'web' ? s.scope : 'both',
    reranker: isReranker(s.reranker) ? s.reranker : 'default',
  }
}

// tools the search scope holds back: the web ones under 'local', ERLA's library ones under 'web'
const WEB_TOOLS = /^(WebSearch|WebFetch)$/
const LIBRARY_TOOLS = /erla__(library_search|library_files|library_show|library_properties)$/

// why a tool call is refused under the panel's search scope, or null when it may run
export function scopeBlock(tool: string, scope: PanelSettings['scope']): string | null {
  if (scope === 'local' && WEB_TOOLS.test(tool)) {
    return 'Blocked by the ERLA control panel: "Search in" is set to Library only, so web search and web fetch ' +
      'are off. Answer from the local library and name any gap; the user can change this in /erla → Settings.'
  }
  if (scope === 'web' && LIBRARY_TOOLS.test(tool)) {
    return 'Blocked by the ERLA control panel: "Search in" is set to Web only, so the local library is not ' +
      'searched. Use the web under ERLA\'s web rules; the user can change this in /erla → Settings.'
  }
  return null
}

// "p.41" / "p 41" / "page 41" -> 41; anything else (a sheet, a section) -> null
export function pageNumber(locator: unknown): number | null {
  if (typeof locator !== 'string') return null
  const m = /^\s*(?:p\.?|page)\s*(\d+)/i.exec(locator)
  return m ? Number(m[1]) : null
}

// the request the panel's Open button sends: the PDF viewer shows the file at its first cited page
export function openPrompt(doc: Pick<Source, 'path' | 'title' | 'pages'>): string {
  const page = doc.pages.map(pageNumber).find((n): n is number => n !== null) ?? 1
  return `Open "${doc.path}" in the PDF viewer at page ${page} (${doc.title}).`
}

// library_stats' text block: "index state: CURRENT", "documents: 3660   chunks: ...",
// "ocr used: 297   ocr pending: 136", "vectors: 0 embedded; ..."
export function parseStats(text: string): Pick<PanelView, 'indexState' | 'documents' | 'ocrPending' | 'vectors'> {
  const num = (re: RegExp) => {
    const m = re.exec(text)
    return m ? Number(m[1]) : null
  }
  return {
    indexState: /index state:\s*([A-Z]+)/.exec(text)?.[1] ?? null,
    documents: num(/documents:\s*(\d+)/),
    ocrPending: num(/ocr pending:\s*(\d+)/),
    vectors: num(/vectors:\s*(\d+)\s+embedded/),
  }
}

// the text of a tool's result as a tool.call hook sees it: a string, MCP content blocks, or
// an object holding them
export function resultText(result: unknown): string | null {
  if (typeof result === 'string') return result
  const blocks = Array.isArray(result)
    ? result
    : result && typeof result === 'object' && Array.isArray((result as { content?: unknown }).content)
      ? (result as { content: unknown[] }).content
      : null
  if (!blocks) return null
  const text = blocks
    .map(b => (b && typeof b === 'object' && (b as { type?: unknown }).type === 'text' ? String((b as { text?: unknown }).text ?? '') : ''))
    .join('')
  return text || null
}

// library_show's answer -> the document it opened
export function sourceFromShow(text: string | null): Omit<Source, 'verdict' | 'pages'> | null {
  if (!text) return null
  let d: Record<string, unknown>
  try {
    d = JSON.parse(text) as Record<string, unknown>
  } catch {
    return null
  }
  // library_show answers {"document": {doc_id, title, path, ...}, ...}
  const meta = (d.document && typeof d.document === 'object' ? d.document : d) as Record<string, unknown>
  if (typeof meta.doc_id !== 'number') return null
  const path = typeof meta.path === 'string' ? meta.path : ''
  return {
    docId: meta.doc_id,
    title: typeof meta.title === 'string' && meta.title ? meta.title : path.split(/[\\/]/).pop() || `doc ${meta.doc_id}`,
    path,
    tier: typeof meta.authority_tier === 'string' ? meta.authority_tier : null,
    flag: typeof meta.classification_flag === 'string' && meta.classification_flag ? meta.classification_flag : null,
    year: typeof meta.year === 'number' ? meta.year : null,
    ext: typeof meta.ext === 'string' && meta.ext ? meta.ext.toLowerCase().replace(/^\./, '') : null,
  }
}

// library_files' inventory ({doc_type: [hits]}) -> one list, restricted ones dropped when asked
export function filesFrom(text: string, excludeRestricted: boolean): FileHit[] {
  const d = JSON.parse(text) as { inventory?: Record<string, Array<Record<string, unknown>>> }
  const hits: FileHit[] = []
  for (const group of Object.values(d.inventory ?? {})) {
    for (const h of group) {
      const flag = typeof h.flag === 'string' && h.flag ? h.flag : null
      if (excludeRestricted && flag) continue
      hits.push({
        docId: Number(h.doc_id),
        title: String(h.title ?? h.path ?? ''),
        path: String(h.path ?? ''),
        tier: typeof h.tier === 'string' ? h.tier : null,
        flag,
      })
    }
  }
  return hits
}

// the session's sources as a reference list, for export_packet and the clipboard
export function referencesMarkdown(sources: readonly Source[], date: string): string {
  const lines = [`# ERLA references (${date})`, '']
  sources.forEach((s, i) => {
    const bits = [s.year ? String(s.year) : null, s.tier, s.flag ? `**${s.flag}**` : null].filter(Boolean)
    lines.push(`${i + 1}. ${s.title}${bits.length ? ` (${bits.join(', ')})` : ''}  `, `   \`${s.path}\` · ERLA doc ${s.docId}`)
  })
  if (sources.some(s => s.flag)) lines.push('', '> Contains restricted documents: check marking and distribution before sharing.')
  return lines.join('\n') + '\n'
}

// what the panel's settings tell the model, as one system-prompt section; null when all default
export function preferencesSection(s: PanelSettings): string | null {
  const lines: string[] = []
  if (s.units !== 'config') {
    lines.push(`- Use ${s.units} as the project unit system: report values in the source's own units and add ` +
      `${s.units} conversions in parentheses where the source differs (ERLA unit_convert).`)
  }
  if (s.excludeRestricted) {
    lines.push('- Restricted documents (ITAR, EAR, CUI, proprietary) are excluded from library searches; say so ' +
      'when that may leave a gap in the answer.')
  }
  if (!s.semantic) lines.push('- Semantic (embedding) search is off: library_search runs keyword search only.')
  if (s.scope === 'local') {
    lines.push('- Search in: library only. Answer from the local library; web search and web fetch are blocked. ' +
      'Report what the library does not cover instead of looking it up.')
  }
  if (s.scope === 'web') {
    lines.push('- Search in: web only. Answer from public web sources under ERLA\'s web rules (check each query ' +
      'with web_query_check first, cite sources with their authority tiers); the library tools are blocked.')
  }
  if (!lines.length) return null
  return ['ERLA preferences the user set in the /erla control panel (they apply within ERLA\'s guardrails and rules):', ...lines].join('\n')
}

// the prompt the panel's "Ask ERLA" sends as the person's own words
export function askPrompt(query: string, scope: PanelSettings['scope'] = 'both'): string {
  const q = query.trim()
  if (scope === 'local') return `Search the ERLA engineering library (library only, no web) and answer with a Result Packet: ${q}`
  if (scope === 'web') return `Research this on the web under ERLA's web rules (web only, not the library) and answer with a Result Packet: ${q}`
  return `Search the ERLA engineering library, then the web for any gaps, and answer with a Result Packet: ${q}`
}

// ------------------------------------------------------------------ relevance chart and search quality

export const CHART_HITS = 10

// library_search's answer -> the panel's relevance chart: the first 10 hits in ERLA's order.
// `rerank` is the re-ranker's 0-1 match of the passage to the question; ERLA's own `score` mixes in
// tier and other priors and is not on a 0-1 scale, so it is never charted.
export function searchFrom(query: string, text: string | null): LastSearch | null {
  if (!text) return null
  let d: { hits?: unknown; mode?: unknown }
  try {
    d = JSON.parse(text) as { hits?: unknown; mode?: unknown }
  } catch {
    return null
  }
  if (!Array.isArray(d.hits)) return null
  const failed = typeof d.mode === 'string' && d.mode.includes('rerank_failed')
  const hits: RankedHit[] = (d.hits as Array<Record<string, unknown>>).slice(0, CHART_HITS).map(h => {
    const r = h.rerank
    const path = typeof h.path === 'string' ? h.path : ''
    return {
      docId: Number(h.doc_id),
      title: typeof h.title === 'string' && h.title ? h.title : path.split(/[\/]/).pop() || `doc ${String(h.doc_id)}`,
      tier: typeof h.tier === 'string' ? h.tier : null,
      flag: typeof h.flag === 'string' && h.flag ? h.flag : null,
      relevance: !failed && typeof r === 'number' && isFinite(r) ? Math.min(1, Math.max(0, r)) : null,
    }
  })
  return { query, ranked: hits.some(h => h.relevance !== null), hits }
}

// one theme color per authority tier, so a standard and a lecture note look different at a glance
export function tierColor(tier: string | null): string {
  switch (tier) {
    case 'T1': return 'success'
    case 'T2': return 'suggestion'
    case 'T3': return 'permission'
    case 'T4': return 'warning'
    default: return 'subtle'
  }
}

// library_stats' last_eval -> the search-quality tile's numbers
export function evalFrom(raw: unknown): EvalSummary | null {
  if (!raw || typeof raw !== 'object') return null
  const e = raw as Record<string, unknown>
  const nd = e['ndcg@10']
  return {
    ndcg10: typeof nd === 'number' && isFinite(nd) ? nd : null,
    questions: typeof e.n === 'number' ? e.n : 0,
    date: typeof e.ts === 'string' ? e.ts.slice(0, 10) : null,
    reranker: typeof e.reranker === 'string' && e.reranker ? e.reranker : null,
    minQuestions: typeof e.min_questions === 'number' ? e.min_questions : 30,
  }
}

// the tile's line and how much to trust it: no score is shown as a number until the golden set is big enough
export function qualityLine(e: EvalSummary | null): { text: string; level: 'ok' | 'weak' | 'none' } {
  if (!e || e.ndcg10 === null) {
    return { text: 'Search quality: not measured yet. Build a golden set (questions with known right answers) and run erla eval.', level: 'none' }
  }
  const tail = [`${e.questions} question${e.questions === 1 ? '' : 's'}`, e.date, e.reranker].filter(Boolean).join(' · ')
  if (e.questions < e.minQuestions) {
    return { text: `Search quality: nDCG@10 ${e.ndcg10.toFixed(2)} (${tail}). Too few questions to rely on: needs ${e.minQuestions}+.`, level: 'weak' }
  }
  return { text: `Search quality: nDCG@10 ${e.ndcg10.toFixed(2)} (${tail})`, level: 'ok' }
}
