import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, UiPressArgument } from 'claude-code'

import type { EngTool, FileHit, LibraryView, PanelSettings, PanelView, Progress, Source } from '../types'
import { explain, MISSING_TOOL, RELOAD_HINT, setupError, setupFrom, splitPaths } from './library'
import type { AddResult, RemoveResult, SiteInfo, StatusResult } from './library'
import {
  acceptBlock, toolBadge, toolsFrom, artifactsFrom, askPrompt, buttonLook, CARD_COLOR, CHART_HITS, DASHBOARD_CHOICES, dashboardLink, DEFAULT_SETTINGS, evalFrom, FLASH_MS, onlyDashboard, filesFrom, isReranker, openPrompt, parseStats,
  preferencesSection, pressedVariant, qualityLine, checkPolicyLines, readSettings, referencesMarkdown, RERANKER_OPTIONS, rerankerArgs, resultText, scopeBlock, searchFrom, sourceFromShow, tierColor,
} from './panel'

const live = atom({ plugin: 'erla', key: 'live' } as const, null)
const isHidden = atom({ plugin: 'erla', key: 'isHidden' } as const, false)

const PHASES = ['index', 'ocr', 'tables', 'embed']
const PHASE_LABEL: Record<string, string> = {
  index: 'Extracting text',
  ocr: 'OCR of scanned PDFs',
  tables: 'Table / property backfill',
  embed: 'Building search embeddings',
}
const UNIT: Record<string, string> = { index: 'files', ocr: 'PDFs', tables: 'PDFs', embed: 'chunks' }

const POLL_RUNNING_MS = 4000
const POLL_IDLE_MS = 20000
// redraw cadence while a job runs: OCR moves one PDF every few minutes, so the
// spinner and the per-item clock are what show the job is alive between counts
const TICK_MS = 1000
const SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

// Progress lines ERLA writes to its refresh log:
//   "  700/3654 extracted (2.8 files/s)"   index
//   "  ocr 5/120: path (n chunks)"         ocr
//   "  12/300 PDFs, 4 tables, ..."         tables
//   "  1200/50000 (85 chunks/s)"           embed
const LINE = /^\s*(?:ocr\s+)?(\d+)\/(\d+)\b/

export function lastCount(lines: readonly string[]): { done: number; total: number } | null {
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = LINE.exec(lines[i] ?? '')
    if (m) {
      const done = Number(m[1])
      const total = Number(m[2])
      if (total > 0 && done <= total) return { done, total }
    }
  }
  return null
}

// "  ocr 22/137: Joints/Fasteners/19900009424.pdf (100 chunks)" -> "19900009424.pdf"
const OCR_ITEM = /^\s*ocr\s+\d+\/\d+:\s+(.*?)(?:\s+\(\d+ chunks\))?\s*$/

export function lastItem(lines: readonly string[]): string | null {
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = OCR_ITEM.exec(lines[i] ?? '')
    if (m && m[1]) return m[1].split(/[\\/]/).pop() ?? m[1]
  }
  return null
}

export function clock(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

export function per(ratePerSec: number, unit: string): string {
  if (ratePerSec >= 1) return `${ratePerSec >= 10 ? Math.round(ratePerSec) : ratePerSec.toFixed(1)} ${unit}/s`
  return `${clock(1 / ratePerSec)} per ${unit.replace(/s$/, '')}`
}

export function bar(fraction: number, width: number): string {
  const w = Math.max(5, width)
  const filled = Math.round(Math.min(1, Math.max(0, fraction)) * w)
  return '█'.repeat(filled) + '░'.repeat(w - filled)
}

export function eta(sec: number | null): string {
  if (sec === null || !isFinite(sec)) return 'ETA …'
  if (sec < 90) return `ETA ${Math.max(1, Math.round(sec))} s`
  if (sec < 5400) return `ETA ${Math.round(sec / 60)} min`
  return `ETA ${(sec / 3600).toFixed(1)} h`
}

type StatusPayload = {
  status?: { state?: string; phase?: string | null }
  log_tail?: string[]
}

let target: { server: string; tool: string } | null = null
let wasRunning = false
// rate anchor for the current phase: the first time the count was seen to move, so
// a bar opened part-way through a slow item does not count that partial item
let sample: { phase: string; t: number; done: number } | null = null
// last count seen and when it moved
let seen: { phase: string; done: number; t: number } | null = null
// why the last poll could not read the job, for /erla-progress
let lastError: string | null = null

async function findTool($: EngineInterface): Promise<{ server: string; tool: string } | null> {
  // our own manifest's server: the engine answers with the exact name $.mcp.call takes,
  // whether or not its tools are deferred or proxied
  try {
    const own = await $.mcp.connect('erla')
    if (own.isConnected) return { server: own.server, tool: 'index_status' }
    lastError = `erla server not connected: ${own.message}`
  } catch (err) {
    lastError = `erla server lookup failed: ${String(err)}`
  }
  // fallback: a proxied copy (e.g. mcp__remote-devices__plugin_erla_erla__index_status)
  const tools = await $.tool.list()
  const hit = tools.find(t => t.mcp && /(^|_)erla__index_status$/.test(t.name))
  if (!hit) return null
  // mcp__<server>__<tool>; the tool part may itself contain "__" when proxied
  const parts = hit.name.split('__')
  if (parts.length < 3) return null
  return { server: parts[1] ?? '', tool: parts.slice(2).join('__') }
}

async function poll($: EngineInterface): Promise<boolean> {
  if (!target) target = await findTool($)
  if (!target) {
    await update($, live, () => null)
    return false
  }

  let payload: StatusPayload
  try {
    const res = await $.mcp.call(target.server, target.tool, { tail: 40 })
    const block = res.content.find(c => c.type === 'text')
    if (res.isError) throw new Error(block && 'text' in block ? String(block.text) : 'index_status returned an error')
    payload = JSON.parse(block && 'text' in block ? String(block.text) : '{}') as StatusPayload
    lastError = null
  } catch (err) {
    lastError = `index_status on ${target.server} failed: ${String(err)}`
    target = null // server restarted or went away; look it up again next time
    return wasRunning
  }

  const state = payload.status?.state
  const running = state === 'running'

  if (!running) {
    await update($, live, () => null)
    if (wasRunning) {
      $.ui.toast(state === 'done' ? 'ERLA: indexing finished' : `ERLA: index job ${state ?? 'stopped'}`)
    }
    wasRunning = false
    sample = null
    seen = null
    return false
  }

  const phase = payload.status?.phase ?? 'index'
  // ERLA's log can be a burst of PDF warnings with no count line: keep the last count of this phase
  const prev = await read($, live)
  const count = lastCount(payload.log_tail ?? []) ??
    (prev && prev.phase === phase && prev.done !== null && prev.total !== null ? { done: prev.done, total: prev.total } : null)
  const now = await $.clock.now()

  let ratePerSec: number | null = null
  let etaSec: number | null = null
  let sinceMs: number | null = null
  if (count) {
    const moved = seen !== null && seen.phase === phase && count.done > seen.done
    if (!seen || seen.phase !== phase || count.done < seen.done) {
      // first sight (or a new phase): the current item started at an unknown time
      seen = { phase, done: count.done, t: now }
      sample = null
    } else if (moved) {
      seen = { phase, done: count.done, t: now }
      if (!sample) sample = { phase, t: now, done: count.done }
    }
    sinceMs = seen.t
    if (sample && seen.done > sample.done && seen.t - sample.t > 3000) {
      // finished items only: from the first move to the latest one
      ratePerSec = (seen.done - sample.done) / ((seen.t - sample.t) / 1000)
      etaSec = (count.total - count.done) / ratePerSec
    }
  }

  const p: Progress = {
    phase,
    done: count?.done ?? null,
    total: count?.total ?? null,
    ratePerSec,
    etaSec,
    unit: UNIT[phase] ?? 'items',
    lastItem: phase === 'ocr' ? lastItem(payload.log_tail ?? []) ?? prev?.lastItem ?? null : null,
    sinceMs,
    nowMs: now,
  }
  await update($, live, () => p)
  if (!wasRunning) {
    // a new job: show the bar even if it was hidden during an earlier job
    await update($, isHidden, () => false)
  }
  wasRunning = true
  return true
}

// set in session.start: poll now, then keep the fast cadence while a job runs
let kick: (() => void) | null = null

// ERLA tools that start (or may start) an index job
const STARTS_JOB = /erla__(index_start|add_library_folders|site_resurvey)$/
const SEARCH_TOOL = /erla__library_search$/
const SHOW_TOOL = /erla__library_show$/


export const register: Register = on => {
  // one hooks module per plugin: the /erla panel's hooks (below) register from here
  registerPanel(on)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'erla-progress',
      description: 'Show or hide the ERLA indexing progress bar',
    })
    await $.command.register({
      name: 'erla',
      description: 'Open the ERLA control panel: status, search, settings, sources',
    })
    // the panel's toggles outlive the session
    try {
      const stored = await $.store.get('settings')
      await update($, settings, () => readSettings(stored))
    } catch {
      // no store (a test without one, a locked store): the defaults stand
    }

    let timer: { cancel: () => void } | null = null
    const schedule = (ms: number) => {
      timer?.cancel()
      timer = $.clock.after(ms, () => {
        void poll($).then(
          running => schedule(running ? POLL_RUNNING_MS : POLL_IDLE_MS),
          () => schedule(POLL_IDLE_MS),
        )
      })
    }
    kick = () => schedule(1500)
    schedule(1000)

    // between polls, advance the bar's clock once a second so the spinner turns and the
    // current item's elapsed time counts up; idle (no job) costs one state read a second
    const tick = () => {
      $.clock.after(TICK_MS, () => {
        void (async () => {
          if (await read($, live)) {
            const now = await $.clock.now()
            await update($, live, p => (p ? { ...p, nowMs: now } : p))
          }
        })().finally(tick)
      })
    }
    tick()

    return next(e)
  })

  // four jobs, every other call passed straight through untouched:
  // - the panel's "Search in" scope holds back web tools (library only) or ERLA's library tools (web only)
  // - the bar appears as soon as an index job is started from Claude, not at the next idle poll
  // - library_search runs with the panel's search toggles, and its top 10 feed the panel's relevance chart
  // - documents opened with library_show join the panel's session sources, with the pages cited
  on('tool.call', async ($, e, next) => {
    const s = await read($, settings)
    const blocked = scopeBlock(e.tool, s.scope) ?? acceptBlock(e.tool, e)
    if (blocked) return { deny: blocked }
    let input = e
    if (SEARCH_TOOL.test(e.tool)) {
      input = { ...e, ...(s.excludeRestricted ? { exclude_flagged: true } : {}), ...(s.semantic ? {} : { hybrid: false }), ...rerankerArgs(s) }
    }
    const result = next(input)
    if (STARTS_JOB.test(e.tool)) {
      void result.then(() => kick?.()).catch(() => undefined)
    }
    if (SEARCH_TOOL.test(e.tool)) {
      const query = (e as { query?: unknown }).query
      void result.then(async r => {
        const found = 'result' in r && !r.isError ? searchFrom(typeof query === 'string' ? query : '', resultText(r.result)) : null
        if (found) await update($, lastSearch, () => found)
      }).catch(() => undefined)
    }
    if (SHOW_TOOL.test(e.tool)) {
      const locator = (e as { locator?: unknown }).locator
      const page = typeof locator === 'string' && locator.trim() ? [locator.trim()] : []
      void result.then(async r => {
        const doc = 'result' in r && !r.isError ? sourceFromShow(resultText(r.result)) : null
        if (!doc) return
        await update($, sources, list => {
          const seen = list.find(x => x.docId === doc.docId)
          if (!seen) return [...list, { ...doc, pages: page, verdict: null }]
          const pages = page.length && !seen.pages.includes(page[0] as string) ? [...seen.pages, ...page] : seen.pages
          return list.map(x => (x.docId === doc.docId ? { ...x, pages } : x))
        })
      }).catch(() => undefined)
    }
    return result
  }).catch(($, e, next) => next(e)) // never block a tool call by failing: on any error here, pass it through unchanged

  // the panel's settings reach the model as one system-prompt section, only when not default
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    const text = preferencesSection(await read($, settings))
    return text ? { ...composed, sections: [...composed.sections, { id: 'erla:preferences', text, scope: 'session' as const }] } : composed
  })

  on('command.run', { command: 'erla-progress' }, async $ => {
    const hidden = await update($, isHidden, h => !h)
    const p = await read($, live)
    const state = p
      ? `ERLA is indexing (${PHASE_LABEL[p.phase] ?? p.phase}${p.done !== null ? `, ${p.done}/${p.total}` : ''}).`
      : lastError
        ? `Cannot read ERLA's index job (${lastError}).`
        : 'No ERLA index job is running right now.'
    return { text: `${state} Progress bar ${hidden ? 'hidden' : 'shown'}.` }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const p = await read($, live)
    if (e.props.hasSurvey || p === null || (await read($, isHidden))) return next(e)

    const { Box, Text } = $.ui.resolve(e)
    const step = PHASES.indexOf(p.phase)
    const stepText = step >= 0 ? `step ${step + 1}/${PHASES.length}` : p.phase
    const label = PHASE_LABEL[p.phase] ?? p.phase
    const spin = SPINNER[Math.floor(p.nowMs / TICK_MS) % SPINNER.length]

    if (p.done === null || p.total === null) {
      return (
        <Box>
          <Text color="cyan">ERLA indexing {spin} </Text>
          <Text>{label} ({stepText}) · starting…</Text>
        </Box>
      )
    }

    const elapsed = p.sinceMs !== null ? (p.nowMs - p.sinceMs) / 1000 : null
    // with a measured rate, the bar creeps through the current item instead of jumping per item
    const partial = p.ratePerSec !== null && elapsed !== null ? Math.min(0.95, elapsed * p.ratePerSec) : 0
    const frac = Math.min(1, (p.done + (p.done < p.total ? partial : 0)) / p.total)
    const pct = `${(frac * 100).toFixed(frac < 0.1 ? 1 : 0)}%`
    const etaLive = p.etaSec !== null && p.ratePerSec !== null && elapsed !== null ? p.etaSec - Math.min(elapsed, 0.95 / p.ratePerSec) : null
    const tail = [
      `${p.done.toLocaleString()}/${p.total.toLocaleString()} ${p.unit}`,
      p.ratePerSec !== null ? per(p.ratePerSec, p.unit) : '',
      p.ratePerSec !== null ? eta(etaLive) : 'ETA after the next item',
    ].filter(Boolean).join(' · ')
    const width = Math.min(40, Math.max(10, (e.props.bodyColumns ?? 80) - 20))
    const slow = p.phase === 'ocr' || (p.ratePerSec !== null && p.ratePerSec < 1)
    const current = slow && elapsed !== null
      ? `${p.unit.replace(/s$/, '')} ${Math.min(p.done + 1, p.total)} running ${clock(elapsed)}${p.lastItem ? ` · last done: ${p.lastItem}` : ''}`
      : null

    return (
      <Box flexDirection="column">
        <Box>
          <Text color="cyan">ERLA indexing {spin} </Text>
          <Text>{label} ({stepText})</Text>
        </Box>
        <Box>
          <Text color="green">{bar(frac, width)}</Text>
          <Text bold> {pct}</Text>
          <Text dimColor>  {tail}</Text>
        </Box>
        {current ? <Text dimColor>{current}</Text> : null}
      </Box>
    )
  })
}

// ------------------------------------------------------------------ library folders
// The /erla panel's Library card: ERLA's library folders from site_info, added through ERLA's
// own folder picker (add_library_folders with no paths) or a typed path, removed after a
// confirming second press, then brought into the index with index_start. ERLA only reads the
// library itself.

const library = atom({ plugin: 'erla', key: 'library' } as const, null)

const EMPTY: LibraryView = {
  roots: [],
  filesOnDisk: null,
  documentsIndexed: null,
  newFiles: null,
  missing: null,
  busy: null,
  message: null,
  confirmRemove: null,
  needsIndex: false,
}

const textOf = (res: { content: { type: string; text?: unknown }[] }): string | null => {
  const block = res.content.find(c => c.type === 'text')
  return block && 'text' in block ? String(block.text) : null
}

async function call<T>($: EngineInterface, tool: string, args: Record<string, unknown> = {}): Promise<T> {
  const own = await $.mcp.connect('erla')
  if (!own.isConnected) throw new Error(`the erla server is not connected (${own.message})`)
  let res: Awaited<ReturnType<EngineInterface['mcp']['call']>>
  try {
    res = await $.mcp.call(own.server, tool, args)
  } catch (err) {
    if (!MISSING_TOOL.test(String(err instanceof Error ? err.message : err))) throw err
    // the install stub, not a broken panel: show its report instead of one raw error per button
    let report: string | null = null
    try {
      report = textOf(await $.mcp.call(own.server, 'erla_setup_status', {}))
    } catch {
      // not the stub either: setupFrom(null) says the server offers no ERLA tools
    }
    await setPanel($, { setup: setupFrom(report) })
    throw setupError()
  }
  const text = textOf(res) ?? '{}'
  if (res.isError) throw new Error(text)
  return JSON.parse(text) as T
}

// this plugin copy's version, from its own manifest: the header shows it when the server cannot
async function readPluginVersion($: EngineInterface): Promise<string | null> {
  for (const path of [`${$.plugin.root}/.claude-plugin/plugin.json`, `${$.plugin.root}/plugin.json`]) {
    try {
      const v = (JSON.parse(await $.fs.read(path)) as { version?: unknown }).version
      if (typeof v === 'string' && /^\d+\.\d+\.\d+/.test(v)) return v
    } catch {
      // try the next place; a missing manifest only hides the version
    }
  }
  return null
}

const set = ($: EngineInterface, change: Partial<LibraryView>) =>
  update($, library, v => ({ ...(v ?? EMPTY), ...change }))

async function refresh($: EngineInterface): Promise<void> {
  try {
    const info = await call<SiteInfo>($, 'site_info')
    const probe = info.library_probe
    await setPanel($, { configUnits: info.unit_system ?? null, checkPolicy: info.check_policy ?? null, version: info.erla_version ?? null, setup: null,
      engineerTools: info.engineer_tools === true })
    await set($, {
      roots: info.library_roots ?? [],
      filesOnDisk: probe?.files_on_disk ?? null,
      documentsIndexed: probe?.documents_indexed ?? null,
      newFiles: probe?.new ?? null,
      missing: probe?.missing ?? null,
    })
  } catch (err) {
    await set($, { message: { text: explain('site_info', err), isError: true } })
  }
}

// one action at a time: a second press while the picker is up does nothing
async function act($: EngineInterface, busy: string, work: () => Promise<Partial<LibraryView>>): Promise<void> {
  if ((await read($, library))?.busy) return
  await set($, { busy, message: null })
  let change: Partial<LibraryView>
  try {
    change = await work()
  } catch (err) {
    change = { message: { text: String(err instanceof Error ? err.message : err), isError: true } }
  }
  await set($, { ...change, busy: null })
  await refresh($)
}

function add($: EngineInterface, paths: string[] | null): Promise<void> {
  const busy = paths ? `Adding ${paths.join(', ')}…` : 'Waiting for the folder picker (it may open behind other windows)…'
  return act($, busy, async () => {
    let res: AddResult
    try {
      res = await call<AddResult>($, 'add_library_folders', paths ? { paths } : {})
    } catch (err) {
      return { message: { text: explain('add_library_folders', err), isError: true } }
    }
    if (res.added) {
      // ERLA starts the index by itself only for a first library; otherwise the dialog offers it
      return { message: { text: `Added. ${res.summary ?? ''}`.trim(), isError: false }, needsIndex: !res.index_job, confirmRemove: null }
    }
    if (res.picker === 'open') return { message: { text: res.message ?? 'The folder picker is already open.', isError: false } }
    if (res.error) return { message: { text: res.error, isError: true } }
    return { message: { text: res.message ?? 'Nothing changed.', isError: false } }
  })
}

async function remove($: EngineInterface, root: string): Promise<void> {
  const view = await read($, library)
  if (view?.busy) return
  if (view?.confirmRemove !== root) {
    await set($, {
      confirmRemove: root,
      message: { text: `Press "Confirm remove" to stop using ${root}. Its files are not touched; ` +
        'the index drops its entries at the next index run.', isError: false },
    })
    return
  }
  await act($, `Removing ${root}…`, async () => {
    let res: RemoveResult
    try {
      res = await call<RemoveResult>($, 'remove_library_folder', { path: root })
    } catch (err) {
      return { message: { text: explain('remove_library_folder', err), isError: true }, confirmRemove: null }
    }
    if (!res.removed) return { message: { text: res.error ?? 'Not removed.', isError: true }, confirmRemove: null }
    return { message: { text: `Removed. ${res.summary ?? ''}`.trim(), isError: false }, needsIndex: true, confirmRemove: null }
  })
}

function indexNow($: EngineInterface): Promise<void> {
  return act($, 'Starting the index refresh…', async () => {
    try {
      const status = await call<StatusResult>($, 'index_status', { tail: 1 })
      if (status.status?.state === 'running') {
        return { message: { text: 'An index job is already running. Press "Index changes" again once it ' +
          'finishes (the progress bar above the prompt shows it); the change is picked up then.', isError: false } }
      }
      await call<unknown>($, 'index_start')
    } catch (err) {
      return { message: { text: explain('index_start', err), isError: true } }
    }
    return { message: { text: 'Index refresh started: the progress bar above the prompt shows it.', isError: false }, needsIndex: false }
  })
}

// ------------------------------------------------------------------ /erla control panel
// One pane for everyday ERLA use, every control also reachable by command or chat:
// status (library_stats) with Refresh index and Library folders…; a search box whose Ask ERLA
// sends the question to Claude as the person's own words and whose List files lists matches
// in place (library_files, no chat turn); the search toggles (units, semantic search,
// restricted documents) that the tool.call and prompt.compose hooks above apply; and the
// documents opened this session with 👍 / 👎 (record_feedback) and Export references
// (export_packet). An optional KPI dashboard card (off by default; Settings shows it, its Hide
// hides it) opens the person's own dashboard link in the browser; it calls no ERLA tool, so it
// works while the server is the install stub or disconnected.

const PANEL_PANE = 'erla-panel'
const PANEL_TITLE = 'ERLA'
const panel = atom({ plugin: 'erla', key: 'panel' } as const, null)
const settings = atom({ plugin: 'erla', key: 'settings' } as const, DEFAULT_SETTINGS)
const sources = atom({ plugin: 'erla', key: 'sources' } as const, [])
const lastSearch = atom({ plugin: 'erla', key: 'lastSearch' } as const, null)
const FILES_SHOWN = 8
const RELEVANCE_BAR = 20
const UNIT_VALUES = ['config', 'US', 'SI'] as const
const SCOPE_OPTIONS = [
  { value: 'both', label: 'Library + web (library first)' },
  { value: 'local', label: 'Local library only' },
  { value: 'web', label: 'Web only' },
] as const

const PANEL_EMPTY: PanelView = {
  indexState: null,
  documents: null,
  ocrPending: null,
  vectors: null,
  configUnits: null,
  checkPolicy: null,
  version: null,
  pluginVersion: null,
  setup: null,
  lastEval: null,
  query: '',
  files: null,
  dashboardChoices: null,
  engineerTools: false,
  tools: null,
  toolsError: null,
  confirmAccept: null,
  toolNote: null,
  busy: null,
  message: null,
  pressed: null,
}

const setPanel = ($: EngineInterface, change: Partial<PanelView>) =>
  update($, panel, v => ({ ...(v ?? PANEL_EMPTY), ...change }))

const say = ($: EngineInterface, text: string, isError = false) => setPanel($, { message: { text, isError } })

async function refreshStatus($: EngineInterface): Promise<void> {
  try {
    const stats = await call<{ text?: string; last_eval?: unknown }>($, 'library_stats')
    await setPanel($, { ...parseStats(stats.text ?? ''), lastEval: evalFrom(stats.last_eval) })
    await refresh($)
  } catch (err) {
    await say($, explain('library_stats', err), true)
  }
  if ((await read($, panel))?.engineerTools) await refreshTools($)
}

// one action at a time, as in the library dialog; resolves to the work's outcome word, if it gives one
async function panelAct($: EngineInterface, busy: string, work: () => Promise<string | void>): Promise<string | void> {
  if ((await read($, panel))?.busy) return
  await setPanel($, { busy, message: null })
  let outcome: string | void = undefined
  try {
    outcome = await work()
  } catch (err) {
    await say($, err instanceof Error ? err.message : String(err), true)
  }
  await setPanel($, { busy: null })
  return outcome
}

let pressSeq = 0

// Every Button press shows on that Button, so a click never looks ignored: at once it is drawn pressed
// (the other variant) with ⏳ and a short word while the work runs, then ✓ or ✗ and the outcome for
// FLASH_MS. Buttons whose press would wait draw dim meanwhile, and a press on one says in a toast what
// runs instead of doing nothing. `waits: false` for presses that never wait (they call no ERLA action).
// The outcome is the work's own word, else "Done", or on an error message "Failed" ("Not ready" while
// the server is the install stub); `library` reads the Library card's message instead of the panel's.
async function press(
  $: EngineInterface, key: string, busyText: string, work: () => Promise<string | void>,
  opts: { waits?: boolean; library?: boolean } = {},
): Promise<void> {
  const waits = opts.waits ?? true
  const running = (await read($, panel))?.busy ?? (await read($, library))?.busy ?? null
  if (waits && running) {
    $.ui.toast(`ERLA is busy: ${running.replace(/…$/, '')}. Press again when it finishes.`)
    return
  }
  const seq = ++pressSeq
  await setPanel($, { pressed: { key, phase: 'busy', text: busyText, seq }, ...(opts.library ? {} : { message: null }) })
  let outcome: string | void = undefined
  try {
    outcome = await work()
  } catch (err) {
    await say($, err instanceof Error ? err.message : String(err), true)
  }
  const view = (await read($, panel)) ?? PANEL_EMPTY
  const failed = outcome === 'Failed' || Boolean((opts.library ? (await read($, library))?.message : view.message)?.isError)
  const text = outcome || (failed ? (view.setup ? 'Not ready' : 'Failed') : 'Done')
  await setPanel($, { pressed: { key, phase: failed ? 'failed' : 'done', text, seq } })
  $.clock.after(FLASH_MS, () => {
    void update($, panel, v => (v && v.pressed?.seq === seq ? { ...v, pressed: null } : v))
  })
}

function refreshIndex($: EngineInterface): Promise<string | void> {
  return panelAct($, 'Starting the index refresh…', async () => {
    try {
      const status = await call<StatusResult>($, 'index_status', { tail: 1 })
      if (status.status?.state === 'running') {
        await say($, 'An index job is already running: the progress bar above the prompt shows it.')
        return 'Already running'
      }
      await call<unknown>($, 'index_start')
      await say($, 'Index refresh started: the progress bar above the prompt shows it.')
      return 'Started'
    } catch (err) {
      await say($, explain('index_start', err), true)
    }
  })
}

async function ask($: EngineInterface, query: string): Promise<string> {
  const q = query.trim()
  if (!q) {
    await say($, 'Type a question in the search box first.', true)
    return 'Type a question'
  }
  const s = await read($, settings)
  await $.prompt.submit({ text: askPrompt(q, s.scope), asUser: true })
  await say($, 'Sent to Claude: the answer arrives in the conversation as a Result Packet.')
  return 'Sent'
}

// the PDF viewer is a tool Claude calls, so Open asks Claude to show the file at its cited page
async function openSource($: EngineInterface, doc: Source): Promise<string> {
  await $.prompt.submit({ text: openPrompt(doc), asUser: true })
  await say($, `Opening ${doc.title} in the PDF viewer in the conversation.`)
  return 'Sent'
}

async function listFiles($: EngineInterface, query: string): Promise<string | void> {
  const q = query.trim()
  if (!q) {
    await say($, 'Type a topic in the search box first.', true)
    return 'Type a topic'
  }
  if ((await read($, settings)).scope === 'web') {
    await say($, '"Search in" is set to Web only, so the library is not listed. Change it under Settings.', true)
    return 'Web only'
  }
  return panelAct($, `Listing files for "${q}"…`, async () => {
    let text: string
    try {
      text = JSON.stringify(await call<unknown>($, 'library_files', { query: q, k: 30 }))
    } catch (err) {
      await say($, explain('library_files', err), true)
      return
    }
    const s = await read($, settings)
    const files: FileHit[] = filesFrom(text, s.excludeRestricted)
    await setPanel($, { files })
    await say($, files.length
      ? `${files.length} file(s) for "${q}"${s.excludeRestricted ? ' (restricted documents left out)' : ''}.`
      : `No files for "${q}".`)
    return files.length ? `${files.length} found` : 'None found'
  })
}

type RerankerStatus = { key: string; label?: string; available: boolean; reason?: string; fix?: string }

// a GPU re-ranker is applied only when ERLA reports it can run here; otherwise the panel says why and how to fix it
function chooseReranker($: EngineInterface, value: string): Promise<void> {
  if (!isReranker(value)) return Promise.resolve()
  if (value === 'default') return changeSettings($, { reranker: 'default' })
  return panelAct($, 'Checking the re-ranker…', async () => {
    const st = await call<{ rerankers: RerankerStatus[] }>($, 'reranker_status')
    const r = st.rerankers.find(x => x.key === value)
    if (!r) throw new Error(`ERLA ${value} is not offered by this ERLA version; update ERLA.`)
    if (!r.available) throw new Error(`${r.label ?? value}: ${r.reason ?? 'cannot run here'}${r.fix ? `\n${r.fix}` : ''}`)
    await changeSettings($, { reranker: value })
    await say($, `${r.label ?? value} will re-rank library searches.`, false)
  })
}

async function changeSettings($: EngineInterface, change: Partial<PanelSettings>): Promise<void> {
  const next = await update($, settings, s => ({ ...s, ...change }))
  try {
    await $.store.set('settings', next)
  } catch {
    await say($, 'Setting applied for this session only: it could not be saved.', true)
  }
}

// the KPI dashboard card: the link is the person's own, checked before it is saved (https only)
async function saveDashboard($: EngineInterface, value: string): Promise<void> {
  const url = dashboardLink(value)
  if (!url) {
    await say($, 'Not saved: paste the dashboard\'s full https:// address.', true)
    return
  }
  await changeSettings($, { dashboardUrl: url })
  await setPanel($, { dashboardChoices: null })
  await say($, 'Dashboard link saved: Open KPI dashboard opens it in your browser.')
}

// Find my dashboard: the person's claude.ai artifacts through the Artifact tool (a read-only
// listing), so nobody has to copy an address; one artifact named like a dashboard is saved at
// once, otherwise the card offers the list to pick from. Pasting stays the fallback.
const NO_LISTING = 'Paste the dashboard\'s https:// link below instead (open the dashboard in your browser and copy its address).'

function findDashboard($: EngineInterface): Promise<string | void> {
  return panelAct($, 'Looking up your claude.ai artifacts…', async () => {
    let text: string | null
    try {
      const res = await $.tool.call({ tool: 'Artifact', action: 'list', scope: 'all', limit: 50 } as never)
      if (res.deny !== undefined) {
        await say($, `Could not list your claude.ai artifacts: ${res.deny} ${NO_LISTING}`, true)
        return
      }
      text = typeof res.text === 'string' ? res.text : resultText(res.result)
    } catch {
      await say($, `Your claude.ai artifacts cannot be listed in this session (no Artifact tool here). ${NO_LISTING}`, true)
      return
    }
    const choices = artifactsFrom(text)
    if (!choices.length) {
      await say($, `No claude.ai artifacts found for your account. Ask Claude to build your KPI dashboard, or ${NO_LISTING.charAt(0).toLowerCase()}${NO_LISTING.slice(1)}`, true)
      return 'None found'
    }
    const only = onlyDashboard(choices)
    if (only) {
      await changeSettings($, { dashboardUrl: only.url })
      await setPanel($, { dashboardChoices: null })
      await say($, `Found "${only.title}" and saved its link: Open KPI dashboard opens it in your browser.`)
      return 'Found'
    }
    await setPanel($, { dashboardChoices: choices.slice(0, DASHBOARD_CHOICES) })
    await say($, 'Pick your dashboard from your artifacts below.')
    return 'Pick one below'
  })
}

async function copyDashboard($: EngineInterface, url: string, surface: Parameters<EngineInterface['ui']['copy']>[0]['surface']): Promise<string> {
  const res = await $.ui.copy({ text: url, surface })
  await say($, res.isCopied ? 'Dashboard link copied.' : 'Could not copy the link here: select it above instead.', !res.isCopied)
  return res.isCopied ? 'Copied' : 'Not copied'
}

function feedback($: EngineInterface, doc: Source, verdict: 'useful' | 'not_useful'): Promise<void> {
  return panelAct($, 'Saving feedback…', async () => {
    try {
      await call<unknown>($, 'record_feedback', { doc_id: doc.docId, verdict })
    } catch (err) {
      await say($, explain('record_feedback', err), true)
      return
    }
    await update($, sources, list => list.map(x => (x.docId === doc.docId ? { ...x, verdict } : x)))
    await say($, `Feedback saved: ${doc.title} ${verdict === 'useful' ? 'useful' : 'not useful'}.`)
  })
}

function exportReferences($: EngineInterface): Promise<string | void> {
  return panelAct($, 'Exporting references…', async () => {
    const list = await read($, sources)
    if (!list.length) {
      await say($, 'No documents opened yet this session.', true)
      return 'Nothing to export'
    }
    const stamp = new Date(await $.clock.now()).toISOString()
    try {
      const res = await call<{ written?: string[]; error?: string }>($, 'export_packet', {
        name: `references_${stamp.slice(0, 19).replace(/[-:T]/g, '')}`,
        markdown: referencesMarkdown(list, stamp.slice(0, 10)),
      })
      // a refusal (the check policy, a bad name) comes back as a result, not a thrown error: never report a
      // file that was not written (correctness audit 2026-10-31: a green "References written to" with no path)
      if (res.error || !res.written?.length) {
        await say($, res.error ?? 'export_packet wrote no file', true)
        return 'Not exported'
      }
      await say($, `References written to ${res.written.join(', ')}`)
      return 'Exported'
    } catch (err) {
      await say($, explain('export_packet', err), true)
    }
  })
}

// ------------------------------------------------------------------ Engineer Tools (ADR-0003)
// Programs ERLA built for the user, kept in the site's tools/ store. Open starts the accepted version on
// 127.0.0.1 through engtools_start (the server refuses a version the user has not accepted, or changed
// files); Accept runs `erla tools accept` on this computer after a confirming second press, since no MCP
// tool may accept (ET3).

// a failed read is said in the card itself: on 2026-10-20 a refused engtools_list (no allow rule) left the
// card at "Reading your tools…" while the error sat in the panel's message line, out of sight
async function refreshTools($: EngineInterface): Promise<void> {
  try {
    await setPanel($, { tools: toolsFrom(await call<unknown>($, 'engtools_list')), toolsError: null })
  } catch (err) {
    await setPanel($, { toolsError: explain('engtools_list', err) })
  }
}

type ToolResult = { ok?: boolean; error?: string; url?: string | null; already_running?: boolean; stopped?: boolean;
  test?: { ok?: boolean; exit?: number | null; seconds?: number; output?: string[] }; output?: string[] }

const lastLines = (lines: string[] | undefined, n = 3) => (lines ?? []).filter(l => l.trim()).slice(-n).join(' | ')

// what a tool's button says goes in that tool's card as well as the message line, where it stays after the
// Button's outcome word fades
const toolSay = ($: EngineInterface, t: EngTool, text: string, isError = false) =>
  setPanel($, { message: { text, isError }, toolNote: { id: t.id, text, isError } })

function toolAction($: EngineInterface, t: EngTool, action: 'start' | 'stop' | 'test'): Promise<string | void> {
  const busy = { start: `Starting ${t.name}…`, stop: `Stopping ${t.name}…`, test: `Running ${t.name}'s tests…` }[action]
  return panelAct($, busy, async () => {
    await setPanel($, { toolNote: null })
    let res: ToolResult
    const args = { tool_id: t.id }
    try {
      // literal names: /erla:setup's permission rules must name every tool the panel calls (test_packaging)
      res = action === 'start' ? await call<ToolResult>($, 'engtools_start', args)
        : action === 'stop' ? await call<ToolResult>($, 'engtools_stop', args)
          : await call<ToolResult>($, 'engtools_test', args)
    } catch (err) {
      await toolSay($, t, explain(`engtools_${action}`, err), true)
      return
    }
    await refreshTools($)
    if (res.ok === false) {
      await toolSay($, t, `${t.name} did not ${action === 'test' ? 'run its tests' : action}: ${res.error ?? `${action} failed`}${res.test?.output || res.output ? ` — ${lastLines(res.test?.output ?? res.output)}` : ''}`, true)
      return
    }
    if (action === 'start') {
      // Open means open: once the tool answers, its page opens in the browser (the refreshed entry has the opener)
      const now = (await read($, panel))?.tools?.find(x => x.id === t.id)
      const url = res.url ?? now?.url ?? t.url
      const opened = await openInBrowser($, { ...t, ...now, url })
      return opened ?? 'Not opened'
    }
    if (action === 'stop') {
      await toolSay($, t, res.stopped ? `${t.name} stopped.` : `${t.name} was not running.`)
      return res.stopped ? 'Stopped' : 'Not running'
    }
    const passed = res.test?.ok === true
    await toolSay($, t, passed
      ? `${t.name}: tests passed in ${res.test?.seconds ?? '?'} s. Review its scope and limits, then press Accept to allow it to start.`
      : `${t.name}: tests failed (exit ${res.test?.exit ?? '?'}) — ${lastLines(res.test?.output)}`, !passed)
    return passed ? 'Passed' : 'Failed'
  })
}

async function acceptTool($: EngineInterface, t: EngTool): Promise<string | void> {
  const v = (await read($, panel)) ?? PANEL_EMPTY
  if (v.confirmAccept !== t.id) {
    await setPanel($, { confirmAccept: t.id })
    await toolSay($, t, `Accept ${t.name} ${t.version}? Its tests passed on these exact files. Accepting lets it start from this panel ` +
      'or when you ask ERLA; it is decision support, not a certified program. Press Accept again to confirm.')
    return 'Press again'
  }
  await setPanel($, { confirmAccept: null })
  if (!t.acceptArgv) {
    await toolSay($, t, 'This ERLA server did not say how to accept the tool; run `erla tools accept` in a terminal.', true)
    return
  }
  return panelAct($, `Accepting ${t.name}…`, async () => {
    let out: { exitCode: number | null; stdout: string; stderr: string }
    try {
      out = await $.process.run(t.acceptArgv ?? [], { timeoutMs: 120_000 })
    } catch (err) {
      // no local commands on this surface: say so and give the command (L10: never a silent fallback)
      await toolSay($, t, `This surface cannot run commands, so the panel cannot accept the tool here (${err instanceof Error ? err.message : String(err)}). ` +
        `In a terminal on this computer run: erla tools accept ${t.id} --sha <hash from erla tools list>`, true)
      return
    }
    let res: ToolResult = {}
    try {
      res = JSON.parse(out.stdout) as ToolResult
    } catch {
      res = { ok: false, error: (out.stderr || out.stdout).trim().split('\n').slice(-2).join(' ') || `exit code ${out.exitCode}` }
    }
    await refreshTools($)
    if (out.exitCode !== 0 || res.ok === false) {
      await toolSay($, t, `${t.name} was not accepted: ${res.error ?? `exit code ${out.exitCode}`}`, true)
      return
    }
    await toolSay($, t, `${t.name} ${t.version} accepted: press Open to start it.`)
    return 'Accepted'
  })
}

// 2026-10-20: the card's Link to http://127.0.0.1:8050/ opened nothing in the desktop app (its links open
// https addresses only), so the button runs the server's browser opener on this computer
async function openInBrowser($: EngineInterface, t: EngTool): Promise<string | void> {
  if (!t.openArgv || !t.url) {
    await toolSay($, t, `${t.name} is running, but this ERLA server did not say how to open a browser here: copy the link and open ${t.url ?? 'it'} in your browser.`, true)
    return
  }
  try {
    const out = await $.process.run(t.openArgv, { timeoutMs: 15_000 })
    if (out.exitCode !== 0) throw new Error(`${t.openArgv[0]} exit code ${out.exitCode}${out.stderr.trim() ? `: ${out.stderr.trim()}` : ''}`)
  } catch (err) {
    // L10: never a silent fallback; the address is in the card to copy
    await toolSay($, t, `${t.name} is running, but your browser did not open (${err instanceof Error ? err.message : String(err)}): copy the link and open ${t.url} yourself.`, true)
    return
  }
  await toolSay($, t, `${t.name} opened in your browser at ${t.url} (on this computer only).`)
  return 'Opened'
}

async function copyToolLink($: EngineInterface, t: EngTool, surface: Parameters<EngineInterface['ui']['copy']>[0]['surface']): Promise<string> {
  const res = await $.ui.copy({ text: t.url ?? '', surface })
  await toolSay($, t, res.isCopied ? `${t.name} link copied: paste it in your browser.` : `Could not copy the link here: open ${t.url} in your browser.`, !res.isCopied)
  return res.isCopied ? 'Copied' : 'Not copied'
}

function registerPanel(on: Parameters<Register>[0]): void {
  on('command.run', { command: 'erla' }, async $ => {
    await setPanel($, { busy: 'Reading ERLA status…', message: null, pluginVersion: await readPluginVersion($) })
    const opened = await $.ui.open({ id: PANEL_PANE, title: PANEL_TITLE, focus: true, closeOnEscape: true, rows: 36 })
    void refreshStatus($).finally(() => setPanel($, { busy: null }))
    return {
      text: opened.isPlaced ? 'ERLA control panel opened above the prompt.' : 'ERLA control panel: widen the window to see it.',
    }
  })

  on('ui.render', { component: 'Pane', requestId: PANEL_PANE }, async ($, e) => {
    const { Box, Text, Button, Input, Select, Link } = $.ui.resolve(e)
    const v = (await read($, panel)) ?? PANEL_EMPTY
    const s = await read($, settings)
    const list = await read($, sources)
    const job = await read($, live)
    const lib = (await read($, library)) ?? EMPTY
    const ls = await read($, lastSearch)
    const quality = qualityLine(v.lastEval)

    // theme colors, so the panel reads in light and dark themes alike; each card has its own (CARD_COLOR)
    const ACCENT = CARD_COLOR
    // one section: a rounded card with a colored title
    const card = (key: string, title: string, color: string, ...body: unknown[]) => (
      <Box key={key} flexDirection="column" borderStyle="round" borderColor={color} paddingX={1} marginBottom={1}>
        <Text bold color={color}>{title}</Text>
        {body}
      </Box>
    )
    // one action Button with its press feedback (press() above): pressed variant and ⏳ / ✓ / ✗ while it
    // shows a press, dim while another action runs and a press would wait, bold and underlined under the
    // pointer. `scope` names its hover group (a short one where the key holds a path).
    const waiting = Boolean(v.busy ?? lib.busy)
    const button = (key: string, label: string, variant: 'primary' | 'secondary', onPress: (e: UiPressArgument) => void,
      opts: { waits?: boolean; scope?: string } = {}) => {
      const look = buttonLook(key, label, v.pressed, (opts.waits ?? true) && waiting)
      return (
        <Button
          key={key}
          variant={look.isPressed ? pressedVariant(variant) : variant}
          dimColor={look.dim}
          hover={{ scope: `erla-button:${opts.scope ?? key}`, bold: true, underline: true }}
          onPress={onPress}
        >
          {look.mark ? [<Text key="mark" color={look.markColor ?? undefined} bold>{look.mark}</Text>, ` ${look.text}`] : look.text}
        </Button>
      )
    }
    const stateColor = v.indexState === 'CURRENT' ? 'success' : v.indexState ? 'warning' : 'subtle'
    const unitLabel = (u: string) => (u === 'config' ? `Library default (${v.configUnits ?? 'US'})` : u === 'US' ? 'US customary' : 'SI')
    const toggle = (key: string, isOn: boolean, label: string, onPress: () => void) => (
      <Button key={key} plain hover={{ scope: `erla-button:${key}`, bold: true }} onPress={onPress}>
        <Text color={isOn ? 'success' : 'subtle'} bold>{isOn ? '☑' : '☐'}</Text>
        <Text dimColor={!isOn}>{` ${label}`}</Text>
      </Button>
    )
    const flagTag = (flag: string | null) => (flag ? <Text color="error" bold>{`⚠ ${flag}`}</Text> : null)

    return (
      <Box flexDirection="column" paddingX={1}>
        <Box gap={1} marginBottom={1}>
          <Text bold color="claude">ERLA</Text>
          {v.version ?? v.pluginVersion ? <Text color="claude">{`v${v.version ?? v.pluginVersion}`}</Text> : null}
          <Text color="subtle">Engineering Research Library Agent</Text>
        </Box>

        {v.setup
          ? card('setup', v.setup.done ? 'Setup: reconnect ERLA' : 'Setup: ERLA is installing', ACCENT.setup,
            <Text key="setup-head" color="warning">
              {v.setup.done
                ? `ERLA ${v.pluginVersion ?? ''} is installed, but this session still talks to the installer. ${RELOAD_HINT}`.replace(/\s+/g, ' ')
                : `The buttons below work once ERLA's runtime is installed. ${RELOAD_HINT}`}
            </Text>,
            <Text key="setup-report" color="subtle">{v.setup.text}</Text>,
            <Box key="setup-actions" gap={1} marginTop={1}>
              {button('setup-check', 'Check again', 'primary', () => void press($, 'setup-check', 'Checking…', async () => {
                await panelAct($, 'Reading ERLA status…', () => refreshStatus($))
                return (await read($, panel))?.message?.isError ? undefined : 'Ready'
              }))}
            </Box>,
          )
          : null}

        {card('status', 'Status', ACCENT.status,
          <Box key="badges" gap={2} flexWrap="wrap">
            <Text backgroundColor={stateColor} color="inverseText" bold>{` ${v.indexState ?? '—'} `}</Text>
            {v.documents !== null ? <Text>{`${v.documents.toLocaleString()} docs`}</Text> : null}
            {v.ocrPending ? <Text color="warning">{`OCR ${v.ocrPending} pending`}</Text> : null}
            {v.vectors !== null
              ? <Text color={v.vectors > 0 ? 'success' : 'warning'}>{v.vectors > 0 ? `${v.vectors.toLocaleString()} vectors` : 'semantic search not built yet'}</Text>
              : null}
          </Box>,
          <Text key="quality" color={quality.level === 'ok' ? 'success' : quality.level === 'weak' ? 'warning' : 'subtle'}>{quality.text}</Text>,
          job
            ? <Text key="job" color={ACCENT.status}>{`⟳ Indexing now: ${PHASE_LABEL[job.phase] ?? job.phase}${job.done !== null ? ` ${job.done}/${job.total}` : ''}`}</Text>
            : null,
          <Box key="actions" gap={1} marginTop={1}>
            {button('reindex', 'Refresh index', 'primary', () => void press($, 'reindex', 'Starting…', () => refreshIndex($)))}
            {button('status', 'Refresh status', 'secondary', () => void press($, 'status', 'Reading…', async () => {
              await panelAct($, 'Reading ERLA status…', () => refreshStatus($))
              return (await read($, panel))?.message?.isError ? undefined : 'Updated'
            }))}
          </Box>,
        )}

        {s.dashboardShown
          ? card('dashboard', 'KPI dashboard', ACCENT.dashboard,
            s.dashboardUrl
              ? <Text key="dash-open" bold><Link href={s.dashboardUrl}>Open KPI dashboard ↗</Link></Text>
              : <Text key="dash-none" color="subtle">Press Find my dashboard: ERLA looks through your claude.ai artifacts and saves your dashboard's link. It is kept with your panel settings and opens in your browser.</Text>,
            s.dashboardUrl ? <Text key="dash-url" color="subtle" wrap="truncate-middle">{s.dashboardUrl}</Text> : null,
            !s.dashboardUrl && v.dashboardChoices
              ? (
                <Box key="dash-choices" flexDirection="column" marginTop={1}>
                  <Text key="dash-choices-head" color="subtle">Your artifacts (press one to use it):</Text>
                  {v.dashboardChoices.map((c, i) => (
                    <Button key={`dash-pick-${i}`} plain hover={{ scope: `erla-button:dash-pick-${i}`, bold: true, underline: true }} onPress={() => void saveDashboard($, c.url)}>
                      <Text color="claude">{`→ ${c.title}`}</Text>
                    </Button>
                  ))}
                </Box>
              )
              : null,
            s.dashboardUrl
              ? null
              : (
                <Box key="dash-row" width="100%" flexGrow={1} marginTop={1}>
                  <Input
                    key="dash-input"
                    placeholder="or paste a link: https://claude.ai/artifact/…"
                    submitLabel="save"
                    onSubmit={value => void saveDashboard($, value)}
                  />
                </Box>
              ),
            <Box key="dash-actions" gap={1} marginTop={1}>
              {s.dashboardUrl
                ? null
                : button('dash-find', 'Find my dashboard', 'primary', () => void press($, 'dash-find', 'Looking…', () => findDashboard($)))}
              {s.dashboardUrl
                ? button('dash-copy', 'Copy link', 'secondary',
                  e => void press($, 'dash-copy', 'Copying…', () => copyDashboard($, s.dashboardUrl ?? '', e.surface), { waits: false }), { waits: false })
                : null}
              {s.dashboardUrl
                ? button('dash-change', 'Change link', 'secondary', () => void press($, 'dash-change', 'Changing…', async () => {
                  await changeSettings($, { dashboardUrl: null })
                  await say($, 'Link cleared: press Find my dashboard or paste the new link.')
                }, { waits: false }), { waits: false })
                : null}
              {button('dash-hide', 'Hide', 'secondary', () => void press($, 'dash-hide', 'Hiding…', async () => {
                await changeSettings($, { dashboardShown: false })
                await say($, 'KPI dashboard card hidden: Settings → Show the KPI dashboard card brings it back.')
              }, { waits: false }), { waits: false })}
            </Box>,
          )
          : null}

        {v.engineerTools
          ? card('tools', `Engineer Tools${v.tools ? ` (${v.tools.length})` : ''}`, ACCENT.tools,
            v.toolsError
              ? <Text key="tools-error" color="error">{v.toolsError}</Text>
              : null,
            !v.tools
              ? v.toolsError ? null : <Text key="tools-none" color="subtle">Reading your tools…</Text>
              : v.tools.length === 0
                ? <Text key="tools-none" color="subtle">No tools yet. Ask ERLA to build a program for an analysis; once it is registered (erla tools add {'<folder>'}) it is listed here.</Text>
                : null,
            ...(v.tools ?? []).map(t => {
              const badge = toolBadge(t)
              return (
                <Box key={`tool-${t.id}`} flexDirection="column" marginTop={1}>
                  <Box gap={1} flexWrap="wrap">
                    <Text bold>{t.name}</Text>
                    {t.version ? <Text color="subtle">{`v${t.version}`}</Text> : null}
                    <Text color={badge.color}>{`● ${badge.text}`}</Text>
                  </Box>
                  {t.summary ? <Text color="subtle">{t.summary}</Text> : null}
                  {t.limits.length ? <Text color="subtle" wrap="truncate-end">{`Limits: ${t.limits.join('; ')}`}</Text> : null}
                  {t.running && t.url ? <Text color="subtle"><Link href={t.url}>{t.url}</Link></Text> : null}
                  <Box gap={1}>
                    {t.state === 'accepted' && !t.running
                      ? button(`tool-open-${t.id}`, 'Open', 'primary', () => void press($, `tool-open-${t.id}`, 'Starting…', () => toolAction($, t, 'start')), { scope: 'tool-open' })
                      : null}
                    {t.running && t.url
                      ? button(`tool-browse-${t.id}`, 'Open in browser', 'primary', () => void press($, `tool-browse-${t.id}`, 'Opening…', () => openInBrowser($, t), { waits: false }), { scope: 'tool-browse', waits: false })
                      : null}
                    {t.running && t.url
                      ? button(`tool-copy-${t.id}`, 'Copy link', 'secondary', e => void press($, `tool-copy-${t.id}`, 'Copying…', () => copyToolLink($, t, e.surface), { waits: false }), { scope: 'tool-copy', waits: false })
                      : null}
                    {t.running
                      ? button(`tool-stop-${t.id}`, 'Stop', 'secondary', () => void press($, `tool-stop-${t.id}`, 'Stopping…', () => toolAction($, t, 'stop')), { scope: 'tool-stop' })
                      : null}
                    {t.canAccept
                      ? button(`tool-accept-${t.id}`, v.confirmAccept === t.id ? 'Confirm accept' : 'Accept', 'primary',
                        () => void press($, `tool-accept-${t.id}`, 'Accepting…', () => acceptTool($, t)), { scope: 'tool-accept' })
                      : null}
                    {t.running
                      ? null
                      : button(`tool-test-${t.id}`, 'Run tests', 'secondary', () => void press($, `tool-test-${t.id}`, 'Testing…', () => toolAction($, t, 'test')), { scope: 'tool-test' })}
                  </Box>
                  {v.toolNote?.id === t.id
                    ? <Text key={`tool-note-${t.id}`} color={v.toolNote.isError ? 'error' : 'subtle'}>{v.toolNote.text}</Text>
                    : null}
                </Box>
              )
            }),
            <Box key="tools-actions" gap={1} marginTop={1}>
              {button('tools-refresh', 'Refresh list', 'secondary', () => void press($, 'tools-refresh', 'Reading…', async () => {
                await panelAct($, 'Reading your tools…', () => refreshTools($))
                return (await read($, panel))?.toolsError ? 'Failed' : 'Updated'
              }))}
            </Box>,
          )
          : null}

        {card('search', 'Search', ACCENT.search,
          <Text key="q-label" color="subtle">Ask a question, or name a topic to list files</Text>,
          // the field on its own full-width row, so the desktop draws it as wide as the card
          <Box key="q-row" width="100%" flexGrow={1}>
            <Input
              key="q"
              placeholder="e.g. Kt for a stepped shaft in bending, or fatigue of 7075-T6 lugs"
              value={v.query}
              submitLabel="ask"
              autoFocus
              onInput={value => void setPanel($, { query: value })}
              onSubmit={value => {
                void setPanel($, { query: value }).then(() => ask($, value))
              }}
            />
          </Box>,
          <Box key="search-actions" gap={1} marginTop={1}>
            {button('ask', 'Ask ERLA', 'primary', () => void press($, 'ask', 'Sending…', () => ask($, v.query), { waits: false }), { waits: false })}
            {button('files', 'List files', 'secondary', () => void press($, 'files', 'Listing…', () => listFiles($, v.query)))}
          </Box>,
          v.files && v.files.length
            ? (
              <Box key="files-list" flexDirection="column" marginTop={1}>
                {v.files.slice(0, FILES_SHOWN).map(f => (
                  <Box key={`file:${f.docId}`} gap={1}>
                    {flagTag(f.flag)}
                    <Text color="suggestion">{f.tier ?? '--'}</Text>
                    <Text>{f.title}</Text>
                    {f.path && f.path !== f.title ? <Text color="subtle">{f.path}</Text> : null}
                  </Box>
                ))}
                {v.files.length > FILES_SHOWN ? <Text color="subtle">{`…and ${v.files.length - FILES_SHOWN} more`}</Text> : null}
              </Box>
            )
            : null,
        )}

        {card('relevance', `Relevance: last search${ls ? ` (top ${Math.min(CHART_HITS, ls.hits.length)})` : ''}`, ACCENT.relevance,
          !ls
            ? <Text key="none" color="subtle">After ERLA searches the library, its top 10 sources appear here with how well each matches the question.</Text>
            : null,
          ls ? <Text key="query" color="subtle" wrap="truncate-end">{`"${ls.query}"`}</Text> : null,
          ls && !ls.ranked
            ? <Text key="unranked" color="warning">Not ranked by relevance: this search ran without the re-ranker (keyword or semantic order only), so no bars are drawn.</Text>
            : null,
          ls && ls.hits.length === 0 ? <Text key="empty" color="subtle">No library hits for this question.</Text> : null,
          ...(ls ? ls.hits : []).map((r, i) => (
            <Box key={`rel:${i}:${r.docId}`} gap={1}>
              <Text color="subtle">{String(i + 1).padStart(2)}</Text>
              {r.relevance !== null
                ? <Text color={tierColor(r.tier)}>{bar(r.relevance, RELEVANCE_BAR)}</Text>
                : ls?.ranked ? <Text color="subtle">{'·'.repeat(RELEVANCE_BAR)}</Text> : null}
              {r.relevance !== null ? <Text bold>{r.relevance.toFixed(2)}</Text> : ls?.ranked ? <Text color="subtle"> -- </Text> : null}
              <Text color={tierColor(r.tier)}>{r.tier ?? '--'}</Text>
              {flagTag(r.flag)}
              <Text wrap="truncate-end">{r.title}</Text>
            </Box>
          )),
          ls && ls.ranked
            ? <Text key="legend" color="subtle">Bar: how well the passage matches the question (re-ranker, 0–1). It does not check that a value is right; ERLA verifies values before a Result Packet. Order puts higher tiers first among relevant hits (R1.1).</Text>
            : null,
        )}

        {card('library', `Library folders (${lib.roots.length})`, ACCENT.library,
          <Text key="ro" color="subtle">ERLA only reads these folders. Changes reach searches after an index run.</Text>,
          lib.roots.length === 0 && !lib.busy ? <Text key="none" color="subtle">No library folders yet. Add one below.</Text> : null,
          ...lib.roots.map((root, i) => (
            <Box key={`root:${root}`} justifyContent="space-between" alignItems="center" gap={2}>
              <Box gap={1} flexShrink={1}>
                <Text color={ACCENT.library}>▸</Text>
                <Text bold wrap="truncate-middle">{root}</Text>
              </Box>
              {button(`rm:${root}`, lib.confirmRemove === root ? 'Confirm remove' : 'Remove', lib.confirmRemove === root ? 'primary' : 'secondary',
                // the first press turns it into Confirm remove (its feedback); the second removes
                () => void (lib.confirmRemove === root
                  ? press($, `rm:${root}`, 'Removing…', () => remove($, root), { library: true })
                  : remove($, root)),
                { scope: `rm:${i}` })}
            </Box>
          )),
          lib.filesOnDisk !== null
            ? (
              <Box key="counts" gap={2} flexWrap="wrap">
                <Text>{`${lib.filesOnDisk.toLocaleString()} files on disk`}</Text>
                <Text color={lib.documentsIndexed === lib.filesOnDisk ? 'success' : 'warning'}>{`${(lib.documentsIndexed ?? 0).toLocaleString()} indexed`}</Text>
                {lib.newFiles ? <Text color="warning">{`${lib.newFiles} new`}</Text> : null}
                {lib.missing ? <Text color="error">{`${lib.missing} missing`}</Text> : null}
              </Box>
            )
            : null,
          <Text key="add-label" color="subtle">Add a folder: type its path, or Browse</Text>,
          <Box key="add-row" width="100%" flexGrow={1}>
            <Input
              key="add-path"
              placeholder="D:\Standards\MIL-HDBK  (several: separate with ;)"
              submitLabel="add"
              onSubmit={value => {
                const paths = splitPaths(value)
                if (paths.length) void add($, paths)
              }}
            />
          </Box>,
          <Box key="lib-actions" gap={1} marginTop={1} alignItems="center">
            {button('browse', 'Browse…', 'primary', () => void press($, 'browse', 'Picker open…', async () => {
              await add($, null)
              const m = (await read($, library))?.message
              return m?.isError ? undefined : m && /^Added/.test(m.text) ? 'Added' : 'No change'
            }, { library: true }))}
            {lib.needsIndex
              ? button('index', 'Index changes', 'primary', () => void press($, 'index', 'Starting…', () => indexNow($), { library: true }))
              : <Text color="subtle">Browse opens the Windows folder picker (it may open behind other windows).</Text>}
          </Box>,
          lib.needsIndex ? <Text key="needs" color="warning">Folders changed: press Index changes to bring them into searches.</Text> : null,
          lib.busy ? <Text key="lib-busy" color="warning">{`⏳ ${lib.busy}`}</Text> : null,
          lib.message ? <Text key="lib-msg" color={lib.message.isError ? 'error' : 'success'}>{`${lib.message.isError ? '✗' : '✓'} ${lib.message.text}`}</Text> : null,
        )}

        {card('settings', 'Settings', ACCENT.settings,
          <Select
            key="scope"
            label="Search in: "
            value={s.scope}
            options={SCOPE_OPTIONS.map(o => ({ value: o.value, label: o.label }))}
            onSelect={value => void changeSettings($, { scope: value === 'local' || value === 'web' ? value : 'both' })}
          />,
          s.scope !== 'both'
            ? <Text key="scope-note" color="warning">{s.scope === 'local' ? 'Web search and web fetch are off while this is set.' : 'ERLA\'s library tools are off while this is set.'}</Text>
            : null,
          <Select
            key="units"
            label="Units in answers: "
            value={s.units}
            options={UNIT_VALUES.map(u => ({ value: u, label: unitLabel(u) }))}
            onSelect={value => void changeSettings($, { units: value === 'US' || value === 'SI' ? value : 'config' })}
          />,
          <Select
            key="reranker"
            label="Re-ranker: "
            value={s.reranker}
            options={RERANKER_OPTIONS.map(o => ({ value: o.value, label: o.label }))}
            onSelect={value => void chooseReranker($, value)}
          />,
          ...checkPolicyLines(v.checkPolicy).map((line, i) =>
            <Text key={`check-policy-${i}`} color={i === 0 ? undefined : 'subtle'}>{line}</Text>),
          <Box key="toggles" flexDirection="column" marginTop={1}>
            {toggle('semantic', s.semantic, 'Semantic search', () => void changeSettings($, { semantic: !s.semantic }))}
            {toggle('restricted', s.excludeRestricted, 'Leave out restricted documents (ITAR / EAR / CUI / proprietary)',
              () => void changeSettings($, { excludeRestricted: !s.excludeRestricted }))}
            {toggle('dashboard', s.dashboardShown, 'Show the KPI dashboard card', () => void changeSettings($, { dashboardShown: !s.dashboardShown }))}
          </Box>,
        )}

        {card('sources', `Sources this session (${list.length})`, ACCENT.sources,
          list.length === 0 ? <Text key="none" color="subtle">Documents ERLA opens for an answer appear here.</Text> : null,
          ...list.map(doc => (
            <Box key={`src:${doc.docId}`} gap={1}>
              {flagTag(doc.flag)}
              {doc.tier ? <Text color="suggestion">{doc.tier}</Text> : null}
              <Text>{doc.title}</Text>
              {doc.pages.length ? <Text color="subtle">{doc.pages.slice(0, 4).join(', ')}{doc.pages.length > 4 ? '…' : ''}</Text> : null}
              {doc.ext === 'pdf'
                ? button(`open:${doc.docId}`, 'Open', 'secondary', () => void press($, `open:${doc.docId}`, 'Sending…', () => openSource($, doc), { waits: false }), { waits: false })
                : null}
              <Button key={`up:${doc.docId}`} plain dimColor={doc.verdict !== 'useful'} onPress={() => void feedback($, doc, 'useful')}>👍</Button>
              <Button key={`down:${doc.docId}`} plain dimColor={doc.verdict !== 'not_useful'} onPress={() => void feedback($, doc, 'not_useful')}>👎</Button>
            </Box>
          )),
          <Box key="source-actions" gap={1} marginTop={1}>
            {button('export', 'Export references', 'primary', () => void press($, 'export', 'Exporting…', () => exportReferences($)))}
            {button('clear', 'Clear list', 'secondary', () => void press($, 'clear', 'Clearing…', async () => {
              await update($, sources, () => [])
              return 'Cleared'
            }, { waits: false }), { waits: false })}
          </Box>,
        )}

        <Box justifyContent="space-between" alignItems="center">
          <Box flexDirection="column" flexShrink={1}>
            {v.busy ? <Text color="warning">{`⏳ ${v.busy}`}</Text> : null}
            {v.message ? <Text color={v.message.isError ? 'error' : 'success'}>{`${v.message.isError ? '✗' : '✓'} ${v.message.text}`}</Text> : null}
          </Box>
          <Button key="close" role="dismiss" onPress={() => void $.ui.close({ id: PANEL_PANE })}>Close</Button>
        </Box>
      </Box>
    )
  })
}
