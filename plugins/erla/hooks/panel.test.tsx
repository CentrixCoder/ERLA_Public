import { expect, mock, test } from 'claude-code/testing'

import type { RenderPropsOf } from 'claude-code'

import {
  acceptBlock, openerFrom, toolBadge, toolsFrom,artifactsFrom, askPrompt, buttonLook, CARD_COLOR, checkPolicyLines, FLASH_MS, pressedVariant, SAME_COLOR, dashboardLink, onlyDashboard, evalFrom, filesFrom, openPrompt, pageNumber, parseStats, preferencesSection, qualityLine, readSettings, referencesMarkdown,
  rerankerArgs, resultText, scopeBlock, searchFrom, sourceFromShow, tierColor,
} from './panel'

const PANE_PROPS = {} as unknown as RenderPropsOf['Pane']
const COMPOSE = {
  model: 'claude-opus-5-5', promptModel: 'claude-opus-5-5', surfaces: ['desktop'], tools: [],
  outputStyle: { name: 'default', isKeepingCodingInstructions: true }, traits: [], sections: [],
} as never

// library_stats' text as ERLA 0.2.8 prints it (shortened)
const STATS = [
  'last index: 2026-10-11T03:31:36+00:00 (0 days old)',
  'index state: CURRENT',
  'documents: 3660   chunks: 371363',
  'ocr used: 297   ocr pending: 136',
  'vectors: 0 embedded; search index none (0 rows, built -)',
].join('\n')

const SHOW = JSON.stringify({
  document: { doc_id: 812, title: 'Peterson\'s Stress Concentration Factors', path: 'Formulas and Charts/Peterson.pdf',
    authority_tier: 'T2', classification_flag: null, year: 2008, ext: 'pdf' },
  chunks: 640,
})
const SHOW_ITAR = JSON.stringify({
  document: { doc_id: 99, title: 'Fitting allowables', path: 'ITAR/fittings.pdf', authority_tier: 'T1', classification_flag: 'ITAR', year: null },
})

// one engtools_list row as ERLA's server answers it (erla/engtools.py status())
const ACCEPT_ARGV = (id: string) => ['C:/ERLA/runtime/python.exe', '-m', 'erla.cli', 'tools', 'accept', id, '--sha', 'abc123', '--by', 'panel']
const tool = (id: string, state: string, extra: Record<string, unknown> = {}) => ({
  id, state, hash: 'abc123', running: false, url: null, can_accept: state === 'tested',
  manifest: { id, name: id === 'beam' ? 'I-beam Timoshenko analyzer' : id, version: '1.0', summary: 'Propped cantilever, Timoshenko',
    limits: ['no lateral-torsional buckling'], run: ['app.py'], port: 8050 },
  last_test: { ok: state !== 'draft', at: '2026-10-19T20:00:00+00:00', exit: 0, seconds: 1.4, version: '1.0' },
  accepted: state === 'accepted' ? { hash: 'abc123' } : null, accept_argv: ACCEPT_ARGV(id), ...extra,
})

test('helpers: stats, sources, file lists, references, preferences', () => {
  expect(parseStats(STATS)).toEqual({ indexState: 'CURRENT', documents: 3660, ocrPending: 136, vectors: 0 })
  expect(parseStats('nothing')).toEqual({ indexState: null, documents: null, ocrPending: null, vectors: null })

  expect(resultText({ content: [{ type: 'text', text: 'a' }, { type: 'image' }, { type: 'text', text: 'b' }] })).toBe('ab')
  expect(resultText('plain')).toBe('plain')
  expect(resultText(42)).toBe(null)

  expect(sourceFromShow(SHOW)).toEqual({ docId: 812, title: "Peterson's Stress Concentration Factors", path: 'Formulas and Charts/Peterson.pdf', tier: 'T2', flag: null, year: 2008, ext: 'pdf' })
  expect(sourceFromShow('{"error":"doc 5 not found"}')).toBe(null)
  expect(sourceFromShow('not json')).toBe(null)

  const inv = JSON.stringify({ inventory: { handbook: [{ doc_id: 1, title: 'Roark', path: 'R.pdf', tier: 'T2', flag: null }], spec: [{ doc_id: 2, title: 'X', path: 'ITAR/X.pdf', tier: 'T1', flag: 'ITAR' }] } })
  expect(filesFrom(inv, false).map(f => f.docId)).toEqual([1, 2])
  expect(filesFrom(inv, true).map(f => f.docId)).toEqual([1])

  const md = referencesMarkdown([{ ...sourceFromShow(SHOW)!, pages: [], verdict: null }, { ...sourceFromShow(SHOW_ITAR)!, pages: [], verdict: 'useful' }], '2026-10-11')
  expect(md).toMatch(/^# ERLA references \(2026-10-11\)/)
  expect(md).toMatch(/1\. Peterson's Stress Concentration Factors \(2008, T2\)/)
  expect(md).toMatch(/2\. Fitting allowables \(T1, \*\*ITAR\*\*\)/)
  expect(md).toMatch(/Contains restricted documents/)

  expect(readSettings(undefined)).toEqual({ units: 'config', semantic: true, excludeRestricted: false, scope: 'both', reranker: 'default', dashboardUrl: null, dashboardShown: false })
  expect(readSettings({ units: 'SI', semantic: 'yes', scope: 'local' })).toEqual({ units: 'SI', semantic: true, excludeRestricted: false, scope: 'local', reranker: 'default', dashboardUrl: null, dashboardShown: false })
  expect(readSettings({ reranker: 'qwen3-4b' }).reranker).toBe('qwen3-4b')
  expect(readSettings({ reranker: 'gpt-reranker' }).reranker).toBe('default')     // unknown value from an old or edited store
  expect(rerankerArgs(readSettings(undefined))).toEqual({})
  expect(rerankerArgs(readSettings({ reranker: 'qwen3-4b' }))).toEqual({ reranker: 'qwen3-4b' })
  expect(readSettings({ reranker: 'bge-gemma' }).reranker).toBe('default')       // dropped in 0.4.0
  expect(preferencesSection(readSettings(undefined))).toBe(null)
  const pref = preferencesSection({ units: 'SI', semantic: false, excludeRestricted: true }) ?? ''
  expect(pref).toMatch(/Use SI as the project unit system/)
  expect(pref).toMatch(/Restricted documents .* are excluded/)
  expect(pref).toMatch(/keyword search only/)
})

// a fake engine with ERLA behind it; records what the panel and Claude's tool calls did
function fake(on: Parameters<Parameters<typeof test>[1]>[1]) {
  const clock = mock.clock(on)
  const rec = { clock, toasts: [] as string[],
    // ms library_files takes to answer, so a test can look at the panel while the press runs
    slowFiles: 0, searchText: '{}', lastEval: null as unknown, mcp: [] as string[], searches: [] as Record<string, unknown>[], prompts: [] as string[], store: {} as Record<string, unknown>, refuse: '', reached: [] as string[], gpuReady: false,
    // the install stub: the server offers only erla_setup_status, as after a plugin update
    stub: null as string | null,
    // the erla server is not running (the app reports it disconnected)
    down: false,
    // the Artifact tool's listing text; 'deny' refuses the call, 'missing' rejects it (no such tool)
    artifacts: '' as string, artifactCalls: [] as Record<string, unknown>[],
    // engtools_list's answer; null = a server without Engineer Tools (the free core edition)
    tools: null as unknown,
    // commands the panel ran with $.process.run, what they answer, and whether this surface refuses them
    runs: [] as string[][], runOut: { exitCode: 0, stdout: '{"ok": true}', stderr: '' }, runFails: false }
  on('tool.list', () => ({ value: [] }))
  on('mcp.connect', () => ({ value: rec.down ? { isConnected: false, server: 'plugin:erla:erla', message: 'not running' } : { isConnected: true, server: 'plugin:erla:erla' } }))
  on('fs.read', ($, e) => {
    if (String(e.path).replace(/\\/g, '/').endsWith('/.claude-plugin/plugin.json')) return { value: '{"name": "erla", "version": "0.4.1"}' }
    throw new Error(`ENOENT: ${String(e.path)}`)
  })
  on('mcp.call', async ($, e) => {
    if (e.tool !== 'index_status') rec.mcp.push(`${e.tool} ${JSON.stringify(e.args ?? {})}`)
    if (e.tool === 'library_files' && rec.slowFiles) await clock.sleep(rec.slowFiles)
    if (rec.stub !== null) {
      if (e.tool === 'erla_setup_status') return { value: { content: [{ type: 'text', text: rec.stub }], isError: false } }
      // the rejection the panel showed on 2026-10-16 (engine 2.1.299), word for word
      return { deny: `$.mcp.call: no connected MCP tool "${e.tool}" on a server named "plugin:erla:erla"` }
    }
    if (e.tool === rec.refuse) return { deny: 'The server-side auto mode classifier gave no verdict' }
    const out: Record<string, unknown> = {
      library_stats: { text: STATS, last_eval: rec.lastEval },
      site_info: { unit_system: 'US', library_roots: ['D:\\Lib'], erla_version: '0.2.9', check_policy: 'always',
        ...(rec.tools !== null ? { engineer_tools: true } : {}) },
      ...(rec.tools !== null ? {
        engtools_list: rec.tools,
        engtools_start: { ok: true, url: 'http://127.0.0.1:8050/' },
        engtools_stop: { ok: true, stopped: true },
        engtools_test: { ok: true, test: { ok: true, exit: 0, seconds: 1.4, output: ['8 passed'] } },
      } : {}),
      index_status: { status: { state: 'done' }, log_tail: [] },
      index_start: { state: 'running' },
      library_files: { inventory: { handbook: [{ doc_id: 1, title: 'Roark', path: 'Roark.pdf', tier: 'T2', flag: null }], spec: [{ doc_id: 2, title: 'Fitting allowables', path: 'ITAR/X.pdf', tier: 'T1', flag: 'ITAR' }] } },
      record_feedback: { logged_to: 'feedback.jsonl' },
      reranker_status: { default: 'ms-marco', rerankers: [
        { key: 'ms-marco', label: 'ms-marco MiniLM (default, CPU, fast)', available: true },
        { key: 'bge-v2-m3', label: 'BGE reranker v2-m3 (CPU, slower)', available: true },
        rec.gpuReady
          ? { key: 'qwen3-4b', label: 'Qwen3-Reranker 4B (GPU)', available: true }
          : { key: 'qwen3-4b', label: 'Qwen3-Reranker 4B (GPU)', available: false,
              reason: 'PyTorch with CUDA needs to be installed for this re-ranker (torch and transformers not found).', fix: 'pip install torch' },
      ] },
      export_packet: { written: ['C:\\ERLA\\out\\references.md'] },
    }
    return { value: { content: [{ type: 'text', text: JSON.stringify(out[e.tool] ?? {}) }], isError: false } }
  })
  // Claude's own ERLA calls reach these, below the plugin's tool.call hook
  on('tool.call', ($, e) => {
    rec.reached.push(e.tool)
    if (e.tool === 'Artifact') {
      rec.artifactCalls.push({ ...e })
      if (rec.artifacts === 'missing') throw new Error('no tool named "Artifact"')
      if (rec.artifacts === 'deny') return { deny: 'Permission to use Artifact has been denied.' }
      return { result: rec.artifacts, text: rec.artifacts } as never
    }
    if (/library_search$/.test(e.tool)) rec.searches.push({ ...e })
    const text = /library_show$/.test(e.tool) ? (e as { doc_id?: number }).doc_id === 99 ? SHOW_ITAR : SHOW
      : /library_search$/.test(e.tool) ? rec.searchText : '{}'
    return { result: { content: [{ type: 'text', text }] } } as never
  })
  on('prompt.submit', ($, e) => {
    rec.prompts.push(e.text)
    return { text: e.text }
  })
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'You are Claude.', scope: 'shared' as const }] }))
  on('process.run', ($, e) => {
    rec.runs.push([...e.argv])
    if (rec.runFails) throw new Error('this surface runs no local commands')
    return { value: rec.runOut as never }
  })
  on('store.get', ($, e) => ({ value: rec.store[e.key] }))
  on('store.set', ($, e) => {
    rec.store[e.key] = e.value
    return { value: undefined }
  })
  on('command.register', () => ({ value: { command: 'x' } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('ui.toast', ($, e) => {
    rec.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  return rec
}

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: /erla shows status, lists files in place and sends Ask ERLA as the person's words`, async ($, on) => {
    const rec = fake(on)
    await $.session.start({ cwd: '/', surface, isInteractive: true })
    expect(JSON.stringify(await $.command.run({ command: 'erla', args: '' } as never))).toMatch(/control panel opened/)
    const ui = await $.ui.mount({ plugin: 'erla', surface, component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })

    for (const badge of [/^ CURRENT $/, /^3,660 docs$/, /^OCR 136 pending$/, /^semantic search not built yet$/]) {
      expect(await ui.find({ type: 'Text', text: badge })).toBeDefined()
    }
    expect(await ui.find({ type: 'Select', key: 'units' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^v0\.2\.9$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Independent check \(Professor\): always - every Result Packet$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /erla check-policy auto \| always \| off/ })).toBeDefined()

    await ui.input({ key: 'q', text: 'stress concentration shoulder fillet', kind: 'change' })
    await ui.press({ key: 'files' })
    expect(rec.mcp).toContain('library_files {"query":"stress concentration shoulder fillet","k":30}')
    expect(await ui.find({ type: 'Text', text: /^Roark$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Fitting allowables$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^⚠ ITAR$/ })).toBeDefined()
    expect(rec.prompts).toEqual([])

    await ui.press({ key: 'ask' })
    expect(rec.prompts).toEqual(['Search the ERLA engineering library, then the web for any gaps, and answer with a Result Packet: stress concentration shoulder fillet'])
    expect(await ui.find({ type: 'Text', text: /Sent to Claude/ })).toBeDefined()

    await ui.press({ key: 'reindex' })
    expect(rec.mcp).toContain('index_start {}')
    await ui.unmount()
  })
}

// 2026-10-16: after the update to 0.4.1 the server was still the install stub; the panel showed no
// version and one raw "no connected MCP tool" error per button. It must say what to do instead.
for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: /erla against the install stub shows Setup and the plugin version, then recovers`, async ($, on) => {
    const rec = fake(on)
    rec.stub = 'Install finished: ERLA 0.4.1. To load the library tools, quit and reopen the Claude app.'
    await $.session.start({ cwd: '/', surface, isInteractive: true })
    await $.command.run({ command: 'erla', args: '' } as never)
    const ui = await $.ui.mount({ plugin: 'erla', surface, component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })

    expect(await ui.find({ type: 'Text', text: /^v0\.4\.1$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Setup: reconnect ERLA$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /If they have not, quit and reopen the Claude app/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /no connected MCP tool/ })).toBe(undefined)

    await ui.press({ key: 'reindex' })
    expect(await ui.find({ type: 'Text', text: /ERLA is not ready yet: see Setup/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /no connected MCP tool/ })).toBe(undefined)

    // the person reconnects: Check again loads the real server's status and drops the Setup card
    rec.stub = null
    await ui.press({ key: 'setup-check' })
    expect(await ui.find({ type: 'Text', text: /^Setup:/ })).toBe(undefined)
    expect(await ui.find({ type: 'Text', text: /^v0\.2\.9$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ CURRENT $/ })).toBeDefined()
    await ui.unmount()
  })
}

test('toggles persist, reach library_search and the system prompt, and filter List files', async ($, on) => {
  const rec = fake(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })

  // defaults: the search runs as Claude asked, no preferences section
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches[0]).not.toHaveProperty('exclude_flagged')
  expect(rec.searches[0]).not.toHaveProperty('hybrid')
  expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['intro'])

  await ui.press({ key: 'restricted' })
  await ui.press({ key: 'semantic' })
  await ui.select({ key: 'units', value: 'SI' })
  expect(rec.store.settings).toEqual({ units: 'SI', semantic: false, excludeRestricted: true, scope: 'both', reranker: 'default', dashboardUrl: null, dashboardShown: false })
  expect(await ui.find({ type: 'Button', text: /☑ Leave out restricted documents/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', text: /☐ Semantic search/ })).toBeDefined()

  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches[1]).toMatchObject({ query: 'Kt', exclude_flagged: true, hybrid: false })
  const sections = (await $.prompt.compose(COMPOSE)).sections
  expect(sections.map(s => s.id)).toEqual(['intro', 'erla:preferences'])
  expect(sections[1]?.text).toMatch(/Use SI as the project unit system/)

  await ui.input({ key: 'q', text: 'fittings', kind: 'change' })
  await ui.press({ key: 'files' })
  expect(await ui.find({ type: 'Text', text: /Fitting allowables/ })).toBe(undefined)
  expect(await ui.find({ type: 'Text', text: /restricted documents left out/ })).toBeDefined()
  await ui.unmount()
})

test('re-ranker choice: GPU ones only when ERLA says they can run, then sent with every library_search', async ($, on) => {
  const rec = fake(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })

  await ui.select({ key: 'reranker', value: 'qwen3-4b' })
  expect(await ui.find({ type: 'Text', text: /PyTorch with CUDA needs to be installed/ })).toBeDefined()
  expect(rec.store.settings).toBe(undefined)                       // refused: nothing saved, default stays
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches[0]).not.toHaveProperty('reranker')

  rec.gpuReady = true                                              // PyTorch + CUDA installed, ERLA restarted
  await ui.select({ key: 'reranker', value: 'qwen3-4b' })
  expect(rec.store.settings).toMatchObject({ reranker: 'qwen3-4b' })
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches[1]).toMatchObject({ query: 'Kt', reranker: 'qwen3-4b' })

  await ui.select({ key: 'reranker', value: 'bge-v2-m3' })            // the slower CPU option needs no GPU
  expect(rec.store.settings).toMatchObject({ reranker: 'bge-v2-m3' })
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches[2]).toMatchObject({ query: 'Kt', reranker: 'bge-v2-m3' })

  await ui.select({ key: 'reranker', value: 'default' })
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches[3]).not.toHaveProperty('reranker')
  await ui.unmount()
})

test('settings saved in an earlier session come back', async ($, on) => {
  const rec = fake(on)
  rec.store.settings = { units: 'SI', semantic: true, excludeRestricted: true }
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches[0]).toMatchObject({ exclude_flagged: true })
})

test('documents Claude opens become sources with feedback and an exported reference list', async ($, on) => {
  const rec = fake(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  expect(await ui.find({ type: 'Text', text: /Sources this session \(0\)/ })).toBeDefined()

  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_show', doc_id: 812 } as never)
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_show', doc_id: 812, locator: 'p.41' } as never)
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_show', doc_id: 99 } as never)
  expect(await ui.find({ type: 'Text', text: /Sources this session \(2\)/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Peterson's Stress Concentration Factors$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Fitting allowables$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^⚠ ITAR$/ })).toBeDefined()

  await ui.press({ key: 'up:812' })
  expect(rec.mcp).toContain('record_feedback {"doc_id":812,"verdict":"useful"}')
  expect(await ui.find({ type: 'Text', text: /Feedback saved: Peterson's Stress Concentration Factors useful/ })).toBeDefined()

  await ui.press({ key: 'export' })
  const exported = rec.mcp.find(c => c.startsWith('export_packet')) ?? ''
  expect(exported).toMatch(/"name":"references_\d{14}"/)
  expect(exported).toMatch(/Peterson's Stress Concentration Factors/)
  expect(await ui.find({ type: 'Text', text: /References written to C:\\ERLA\\out\\references\.md/ })).toBeDefined()

  await ui.press({ key: 'clear' })
  expect(await ui.find({ type: 'Text', text: /Sources this session \(0\)/ })).toBeDefined()
  await ui.unmount()
})

test('a refused tool says which permission rule to add', async ($, on) => {
  const rec = fake(on)
  rec.refuse = 'library_files'
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'terminal', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await ui.input({ key: 'q', text: 'rivets', kind: 'change' })
  await ui.press({ key: 'files' })
  expect(await ui.find({ type: 'Text', text: /add the permission rule mcp__plugin_erla_erla__library_files/ })).toBeDefined()
  await ui.unmount()
})

test('check policy is shown with how to change it, never set from the panel', () => {
  expect(checkPolicyLines(null)).toEqual([])
  expect(checkPolicyLines('off')[0]).toBe('Independent check (Professor): off - nothing, unless you run /erla:check')
  expect(checkPolicyLines('sometimes')[0]).toMatch(/treated as auto$/)
})

test('scope, pages and Open helpers', () => {
  expect(scopeBlock('WebSearch', 'local')).toMatch(/Library only/)
  expect(scopeBlock('WebFetch', 'local')).toMatch(/Library only/)
  expect(scopeBlock('mcp__plugin_erla_erla__library_search', 'local')).toBe(null)
  expect(scopeBlock('mcp__plugin_erla_erla__library_show', 'web')).toMatch(/Web only/)
  expect(scopeBlock('mcp__plugin_erla_erla__index_status', 'web')).toBe(null)
  expect(scopeBlock('WebSearch', 'both')).toBe(null)
  expect(scopeBlock('Bash', 'local')).toBe(null)

  expect(askPrompt('Kt', 'local')).toBe('Search the ERLA engineering library (library only, no web) and answer with a Result Packet: Kt')
  expect(askPrompt('Kt', 'web')).toMatch(/^Research this on the web under ERLA's web rules \(web only, not the library\)/)
  expect(preferencesSection({ units: 'config', semantic: true, excludeRestricted: false, scope: 'local' })).toMatch(/Search in: library only/)
  expect(preferencesSection({ units: 'config', semantic: true, excludeRestricted: false, scope: 'web' })).toMatch(/web_query_check/)

  expect(pageNumber('p.41')).toBe(41)
  expect(pageNumber('page 7')).toBe(7)
  expect(pageNumber('Sheet1!A1')).toBe(null)
  expect(openPrompt({ path: 'D:\\Lib\\Peterson.pdf', title: 'Peterson', pages: ['Table 2', 'p.41'] }))
    .toBe('Open "D:\\Lib\\Peterson.pdf" in the PDF viewer at page 41 (Peterson).')
  expect(openPrompt({ path: 'D:\\Lib\\X.pdf', title: 'X', pages: [] })).toMatch(/at page 1 \(X\)\.$/)
})

test('Search in: library only blocks web tools, web only blocks library tools and List files', async ($, on) => {
  const rec = fake(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  expect(await ui.find({ type: 'Select', key: 'scope' })).toBeDefined()

  await ui.select({ key: 'scope', value: 'local' })
  expect(rec.store.settings).toMatchObject({ scope: 'local' })
  expect(await ui.find({ type: 'Text', text: /Web search and web fetch are off/ })).toBeDefined()
  const web = await $.tool.call({ tool: 'WebSearch', query: 'MMPDS 7075' } as never)
  expect(JSON.stringify(web)).toMatch(/Library only/)
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(rec.searches).toHaveLength(1)
  await ui.press({ key: 'ask' })
  await ui.input({ key: 'q', text: 'Kt shoulder', kind: 'change' })
  await ui.press({ key: 'ask' })
  expect(rec.prompts).toContain('Search the ERLA engineering library (library only, no web) and answer with a Result Packet: Kt shoulder')

  await ui.select({ key: 'scope', value: 'web' })
  const lib = await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(JSON.stringify(lib)).toMatch(/Web only/)
  expect(rec.searches).toHaveLength(1)
  await ui.press({ key: 'files' })
  expect(rec.mcp.some(c => c.startsWith('library_files'))).toBe(false)
  expect(await ui.find({ type: 'Text', text: /set to Web only/ })).toBeDefined()
  const sections = (await $.prompt.compose(COMPOSE)).sections
  expect(sections.find(x => x.id === 'erla:preferences')?.text).toMatch(/Search in: web only/)

  await ui.select({ key: 'scope', value: 'both' })
  await $.tool.call({ tool: 'WebSearch', query: 'MMPDS 7075' } as never)
  expect(rec.reached).toContain('WebSearch')
  await ui.unmount()
})

test('a cited PDF gets its pages listed and an Open button that asks for the viewer at that page', async ($, on) => {
  const rec = fake(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })

  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_show', doc_id: 812, locator: 'p.41' } as never)
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_show', doc_id: 812, locator: 'p.43' } as never)
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_show', doc_id: 812, locator: 'p.41' } as never)
  expect(await ui.find({ type: 'Text', text: /^p\.41, p\.43$/ })).toBeDefined()

  await ui.press({ key: 'open:812' })
  expect(rec.prompts).toEqual(['Open "Formulas and Charts/Peterson.pdf" in the PDF viewer at page 41 (Peterson\'s Stress Concentration Factors).'])
  expect(await ui.find({ type: 'Text', text: /Opening Peterson's Stress Concentration Factors in the PDF viewer/ })).toBeDefined()

  // a document without a PDF extension has no Open button
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_show', doc_id: 99 } as never)
  expect(await ui.find({ key: 'open:99' })).toBe(undefined)
  await ui.unmount()
})

// library_search as ERLA 0.4.0 answers it: ERLA's order is tier first within the relevant set (R1.1), so a
// T1 hit with lower relevance sits above a T3 one; `score` mixes in priors and is not a 0-1 relevance
const hit = (doc_id: number, title: string, tier: string, rerank: number | null, score: number, flag: string | null = null) =>
  ({ doc_id, title, path: `Lib/${title}.pdf`, tier, flag, rerank, score })
const SEARCH = JSON.stringify({
  query: '7075 Fty', mode: 'hybrid+rerank(bge-v2-m3)+tier_first',
  hits: [hit(1, 'MMPDS-18', 'T1', 0.66, 87.3), hit(2, 'Alcoa data sheet', 'T3', 0.72, 91.0), hit(3, 'Fitting allowables', 'T1', 0.4, 40.2, 'ITAR'),
    ...Array.from({ length: 9 }, (_, i) => hit(10 + i, `Extra ${i}`, 'T4', 0.1, 5))],
})

test('relevance chart helpers: re-ranker relevance only, ERLA order, top 10, honest about unranked searches', () => {
  const s = searchFrom('7075 Fty', SEARCH)!
  expect(s.ranked).toBe(true)
  expect(s.hits.length).toBe(10)                                            // 12 hits -> the top 10
  expect(s.hits.map(h => h.docId).slice(0, 3)).toEqual([1, 2, 3])           // ERLA's order kept, not re-sorted by relevance
  expect(s.hits.map(h => h.relevance).slice(0, 3)).toEqual([0.66, 0.72, 0.4])  // never ERLA's blended score (87.3)
  expect(s.hits[2]).toMatchObject({ flag: 'ITAR', tier: 'T1' })

  const keyword = searchFrom('Kt', JSON.stringify({ mode: 'keyword', hits: [{ ...hit(1, 'Peterson', 'T2', null, 12), rerank: null }] }))!
  expect(keyword).toMatchObject({ ranked: false, hits: [{ relevance: null }] })
  // a re-ranker that failed leaves stale or zero scores: never charted as relevance
  const failed = searchFrom('Kt', JSON.stringify({ mode: 'hybrid+rerank_failed(OSError)', hits: [hit(1, 'Peterson', 'T2', 0.0, 12)] }))!
  expect(failed.ranked).toBe(false)
  expect(searchFrom('Kt', JSON.stringify({ hits: [hit(1, 'X', 'T2', 1.7, 1), hit(2, 'Y', 'T2', -0.2, 1)] }))!.hits.map(h => h.relevance)).toEqual([1, 0])
  expect(searchFrom('Kt', 'not json')).toBe(null)
  expect(searchFrom('Kt', '{"error":"no index"}')).toBe(null)

  expect(new Set(['T1', 'T2', 'T3', 'T4'].map(tierColor)).size).toBe(4)
  expect(tierColor(null)).toBe('subtle')

  expect(qualityLine(evalFrom(null)).level).toBe('none')
  // a run recorded before nDCG@10 existed has no score: "not measured", never 0.00
  expect(qualityLine(evalFrom({ ts: '2026-09-01T00:00:00+00:00', n: 40, 'ndcg@10': null })).level).toBe('none')
  const weak = qualityLine(evalFrom({ ts: '2026-10-14T08:00:00+00:00', n: 3, 'ndcg@10': 0.912, reranker: 'ms-marco', min_questions: 30 }))
  expect(weak).toEqual({ level: 'weak', text: 'Search quality: nDCG@10 0.91 (3 questions · 2026-10-14 · ms-marco). Too few questions to rely on: needs 30+.' })
  expect(qualityLine(evalFrom({ ts: '2026-10-14T08:00:00+00:00', n: 42, 'ndcg@10': 0.81, min_questions: 30 })))
    .toEqual({ level: 'ok', text: 'Search quality: nDCG@10 0.81 (42 questions · 2026-10-14)' })
})

test('the panel charts the last library_search Claude ran and shows the search-quality tile', async ($, on) => {
  const rec = fake(on)
  rec.lastEval = { ts: '2026-10-14T08:00:00+00:00', n: 3, 'ndcg@10': 0.9, reranker: 'ms-marco', min_questions: 30 }
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })

  expect(await ui.find({ type: 'Text', text: /Too few questions to rely on: needs 30\+/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /top 10 sources appear here/ })).toBeDefined()

  rec.searchText = SEARCH
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: '7075 Fty' } as never)
  expect(await ui.find({ type: 'Text', text: /^Relevance: last search \(top 10\)$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^"7075 Fty"$/ })).toBeDefined()
  for (const t of [/^0\.66$/, /^0\.72$/, /^MMPDS-18$/, /^⚠ ITAR$/, /^Extra 6$/, /does not check that a value is right/]) {
    expect(await ui.find({ type: 'Text', text: t })).toBeDefined()
  }
  expect(await ui.find({ type: 'Text', text: /^87\.3$/ })).toBe(undefined)
  expect(await ui.find({ type: 'Text', text: /^Extra 7$/ })).toBe(undefined)    // the 11th and 12th hits are not drawn
  expect(await ui.find({ type: 'Text', text: /^█{13}░{7}$/ })).toBeDefined()     // 0.66 of a 20-cell bar

  rec.searchText = JSON.stringify({ mode: 'keyword', hits: [{ ...hit(1, 'Peterson', 'T2', null, 12), rerank: null }] })
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'Kt' } as never)
  expect(await ui.find({ type: 'Text', text: /^Not ranked by relevance/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Peterson$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^█/ })).toBe(undefined)            // no bars without re-ranker scores

  rec.searchText = 'not json'                                                    // an unreadable answer keeps the last chart
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__library_search', query: 'x' } as never)
  expect(await ui.find({ type: 'Text', text: /^Peterson$/ })).toBeDefined()
  await ui.unmount()
})

test('dashboard link: https only, so a pasted javascript:, file: or http: address never becomes a link', () => {
  expect(dashboardLink(' https://claude.ai/artifact/UazUqL7hwRrvwv2vqtrLnT ')).toBe('https://claude.ai/artifact/UazUqL7hwRrvwv2vqtrLnT')
  for (const bad of ['javascript:alert(1)', 'file:///C:/x.html', 'http://claude.ai/artifact/x', 'claude.ai/artifact/x', '', 'https://', `https://x.io/${'a'.repeat(2050)}`, 42, null]) {
    expect(dashboardLink(bad)).toBe(null)
  }
  // a stored link from an older or hand-edited store is checked again on load
  expect(readSettings({ dashboardUrl: 'javascript:alert(1)', dashboardShown: true })).toMatchObject({ dashboardUrl: null, dashboardShown: true })
  expect(readSettings({ dashboardShown: 'yes' }).dashboardShown).toBe(false)
  // the dashboard never reaches the model's prompt
  expect(preferencesSection(readSettings({ dashboardUrl: 'https://claude.ai/artifact/x', dashboardShown: true }))).toBe(null)
})

const DASH = 'https://claude.ai/artifact/UazUqL7hwRrvwv2vqtrLnT'

// the card calls no ERLA tool: it must work with the real server, the install stub and a disconnected one
for (const state of ['server', 'stub', 'disconnected'] as const) {
  test(`KPI dashboard card (${state}): off by default, shown from Settings, link saved, opened, copied, hidden`, async ($, on) => {
    const rec = fake(on)
    if (state === 'stub') rec.stub = 'Installing ERLA 0.4.3: this takes a few minutes.'
    rec.down = state === 'disconnected'
    const copied: string[] = []
    on('ui.copy', ($, e) => {
      copied.push(e.text)
      return { value: { isCopied: true } }
    })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'erla', args: '' } as never)
    const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
    const before = rec.mcp.length

    expect(await ui.find({ type: 'Text', text: /^KPI dashboard$/ })).toBe(undefined)
    await ui.press({ key: 'dashboard' })
    expect(await ui.find({ type: 'Text', text: /^KPI dashboard$/ })).toBeDefined()
    expect(rec.store.settings).toMatchObject({ dashboardShown: true, dashboardUrl: null })

    await ui.input({ key: 'dash-input', text: 'javascript:alert(1)', kind: 'submit' })
    expect(await ui.find({ type: 'Text', text: /Not saved: paste the dashboard's full https:\/\/ address/ })).toBeDefined()
    expect(await ui.find({ type: 'Link' })).toBe(undefined)
    expect(rec.store.settings).toMatchObject({ dashboardUrl: null })

    await ui.input({ key: 'dash-input', text: DASH, kind: 'submit' })
    expect(rec.store.settings).toMatchObject({ dashboardUrl: DASH, dashboardShown: true })
    const link = await ui.find({ type: 'Link' })
    expect(link?.props).toMatchObject({ href: DASH })
    expect(await ui.find({ key: 'dash-input' })).toBe(undefined)

    await ui.press({ key: 'dash-copy' })
    expect(copied).toEqual([DASH])
    expect(await ui.find({ type: 'Text', text: /Dashboard link copied/ })).toBeDefined()

    await ui.press({ key: 'dash-hide' })
    expect(await ui.find({ type: 'Text', text: /^KPI dashboard$/ })).toBe(undefined)
    expect(rec.store.settings).toMatchObject({ dashboardShown: false, dashboardUrl: DASH })   // hiding keeps the link
    await ui.press({ key: 'dashboard' })
    expect((await ui.find({ type: 'Link' }))?.props).toMatchObject({ href: DASH })

    await ui.press({ key: 'dash-change' })
    expect(await ui.find({ type: 'Link' })).toBe(undefined)
    expect(await ui.find({ key: 'dash-input' })).toBeDefined()
    expect(rec.mcp.length).toBe(before)                                                   // no ERLA tool was called
    await ui.unmount()
  })
}

// the Artifact tool's listing as it printed on 2026-10-17 (engine 2.1.299), titles shortened
const LISTING = [
  '4 artifacts, published by you or shared with you (your own first, ...):',
  `- (mine) ERLA KPI Dashboard — ${DASH} — updated 2026-10-17`,
  '- (mine) Lug Restraint Free Bodies — https://claude.ai/artifact/XXM6h1P6ToDGJF3F76c6Ws — updated 2026-09-02',
  '- (mine) ERLA operations — https://claude.ai/artifact/BWFTPwHtL8vdqSgEGieqw2 — updated 2026-10-11',
  '(Artifacts shared from another organization are not listed here yet; ask the user for the link.)',
].join('\n')

test('artifact listing: claude.ai links only, dashboards first, one dashboard picked by itself', () => {
  const all = artifactsFrom(LISTING)
  expect(all).toEqual([
    { title: 'ERLA KPI Dashboard', url: DASH },
    { title: 'Lug Restraint Free Bodies', url: 'https://claude.ai/artifact/XXM6h1P6ToDGJF3F76c6Ws' },
    { title: 'ERLA operations', url: 'https://claude.ai/artifact/BWFTPwHtL8vdqSgEGieqw2' },
  ])
  expect(onlyDashboard(all)).toEqual({ title: 'ERLA KPI Dashboard', url: DASH })
  // a shared title is data: a look-alike host, a javascript: link or a title alone never becomes a choice
  const hostile = [
    '- (shared) KPI dashboard — https://claude.ai.evil.example/artifact/abc — updated 2026-10-01',
    '- (shared) Dashboard — javascript:alert(1)',
    '- (shared) Ops dashboard — http://claude.ai/artifact/plainhttp',
    '- (shared) Team KPI Dashboard — https://claude.ai/code/artifact/0b7e-41aa — updated 2026-10-02',
  ].join('\n')
  expect(artifactsFrom(hostile)).toEqual([{ title: 'Team KPI Dashboard', url: 'https://claude.ai/code/artifact/0b7e-41aa' }])
  // two dashboards: the person picks; none named like one: the person picks
  const two = artifactsFrom(`${LISTING}\n- (mine) Fleet dashboard — https://claude.ai/artifact/Fleet123 — updated 2026-10-12`)
  expect(two.slice(0, 2).map(c => c.title)).toEqual(['ERLA KPI Dashboard', 'Fleet dashboard'])
  expect(onlyDashboard(two)).toBe(null)
  expect(onlyDashboard(artifactsFrom('- (mine) ERLA operations — https://claude.ai/artifact/BWFTPwHtL8vdqSgEGieqw2'))).toBe(null)
  expect(artifactsFrom(null)).toEqual([])
})

// Find my dashboard calls the Artifact tool, not ERLA: it works with the install stub and a disconnected server too
for (const state of ['server', 'stub', 'disconnected'] as const) {
  test(`Find my dashboard (${state}): one dashboard among the artifacts is saved without copying a link`, async ($, on) => {
    const rec = fake(on)
    if (state === 'stub') rec.stub = 'Installing ERLA 0.5.3: this takes a few minutes.'
    rec.down = state === 'disconnected'
    rec.artifacts = LISTING
    rec.store.settings = { dashboardShown: true }
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'erla', args: '' } as never)
    const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
    const before = rec.mcp.length

    expect(await ui.find({ type: 'Link' })).toBe(undefined)
    await ui.press({ key: 'dash-find' })
    expect(rec.artifactCalls).toEqual([expect.objectContaining({ tool: 'Artifact', action: 'list' })])
    expect(rec.store.settings).toMatchObject({ dashboardUrl: DASH, dashboardShown: true })
    expect((await ui.find({ type: 'Link' }))?.props).toMatchObject({ href: DASH })
    expect(await ui.find({ type: 'Text', text: /Found "ERLA KPI Dashboard" and saved its link/ })).toBeDefined()
    expect(await ui.find({ key: 'dash-find' })).toBe(undefined)
    expect(rec.mcp.length).toBe(before)
    await ui.unmount()
  })
}

test('Find my dashboard: with two dashboards the card lists them and a press saves the one chosen', async ($, on) => {
  const rec = fake(on)
  const FLEET = 'https://claude.ai/artifact/Fleet123'
  rec.artifacts = `${LISTING}\n- (mine) Fleet dashboard — ${FLEET} — updated 2026-10-12`
  rec.store.settings = { dashboardShown: true }
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await ui.press({ key: 'dash-find' })
  expect((rec.store.settings as { dashboardUrl?: unknown }).dashboardUrl ?? null).toBe(null)
  expect(await ui.find({ type: 'Text', text: /Pick your dashboard/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '→ Fleet dashboard' })).toBeDefined()
  await ui.press({ key: 'dash-pick-1' })
  expect(rec.store.settings).toMatchObject({ dashboardUrl: FLEET })
  expect((await ui.find({ type: 'Link' }))?.props).toMatchObject({ href: FLEET })
  expect(await ui.find({ key: 'dash-pick-0' })).toBe(undefined)
  // Change link brings Find my dashboard back, without the old list
  await ui.press({ key: 'dash-change' })
  expect(await ui.find({ key: 'dash-find' })).toBeDefined()
  expect(await ui.find({ key: 'dash-pick-0' })).toBe(undefined)
  await ui.unmount()
})

for (const failure of ['deny', 'missing', 'empty'] as const) {
  test(`Find my dashboard (${failure}): says what happened and keeps the paste field`, async ($, on) => {
    const rec = fake(on)
    rec.artifacts = failure === 'empty' ? '0 artifacts, published by you or shared with you.' : failure
    rec.store.settings = { dashboardShown: true }
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'erla', args: '' } as never)
    const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
    await ui.press({ key: 'dash-find' })
    const why = { deny: /Could not list your claude\.ai artifacts: Permission/, missing: /cannot be listed in this session/, empty: /No claude\.ai artifacts found/ }[failure]
    expect(await ui.find({ type: 'Text', text: why })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /paste the dashboard's https:\/\/ link/i })).toBeDefined()
    expect((rec.store.settings as { dashboardUrl?: unknown }).dashboardUrl ?? null).toBe(null)
    expect(await ui.find({ key: 'dash-input' })).toBeDefined()
    expect(await ui.find({ type: 'Link' })).toBe(undefined)
    await ui.unmount()
  })
}

test('a dashboard link saved in an earlier session comes back with the card', async ($, on) => {
  const rec = fake(on)
  rec.store.settings = { dashboardUrl: DASH, dashboardShown: true }
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'terminal', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  expect((await ui.find({ type: 'Link' }))?.props).toMatchObject({ href: DASH })
  await ui.unmount()
})

// 2026-10-19, the owner on the released panel: "the card border colors are not all different". Status and
// Relevance were both `suggestion`, KPI dashboard and Search both `claude`, and Library's `permission` is
// the same colour as `suggestion` in Claude's themes. Two keys that draw one colour count as one here.
const hue = (key: string) => SAME_COLOR[key] ?? key

test('card colours: every card its own hue, only theme keys from the typed set', () => {
  const colours = Object.values(CARD_COLOR).map(hue)
  expect(new Set(colours).size).toBe(Object.keys(CARD_COLOR).length)
  // a key outside the typed theme set could be drawn however a surface likes
  for (const key of Object.values(CARD_COLOR)) {
    expect(['warning', 'ide', 'bashBorder', 'claude', 'planMode', 'suggestion', 'merged', 'success', 'promptBorder']).toContain(key)
  }
})

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: every card the panel draws has an outline colour no other card has`, async ($, on) => {
    const rec = fake(on)
    rec.stub = 'Installing ERLA 0.5.8: this takes a few minutes.'      // the Setup card is drawn too
    rec.store.settings = { dashboardShown: true }                         // and the KPI dashboard card
    rec.tools = { tools: [tool('beam', 'accepted')] }                   // and the Engineer Tools card
    await $.session.start({ cwd: '/', surface, isInteractive: true })
    await $.command.run({ command: 'erla', args: '' } as never)
    const ui = await $.ui.mount({ plugin: 'erla', surface, component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
    const cards = (await ui.findAll({ type: 'Box' })).filter(b => b.props.borderStyle === 'round')
    expect(cards.length).toBe(8)
    const colours = cards.map(b => hue(String(b.props.borderColor)))
    expect(new Set(colours).size).toBe(cards.length)
    await ui.unmount()
  })
}

test('button look: ⏳ while running, ✓ or ✗ after, dim only while it would wait', () => {
  const busy = { key: 'reindex', phase: 'busy' as const, text: 'Starting…', seq: 1 }
  expect(buttonLook('reindex', 'Refresh index', busy, true)).toEqual({ mark: '⏳', markColor: 'warning', text: 'Starting…', isPressed: true, dim: false })
  expect(buttonLook('status', 'Refresh status', busy, true)).toEqual({ mark: null, markColor: null, text: 'Refresh status', isPressed: false, dim: true })
  expect(buttonLook('reindex', 'Refresh index', { ...busy, phase: 'done', text: 'Started' }, false)).toMatchObject({ mark: '✓', markColor: 'success', text: 'Started' })
  expect(buttonLook('reindex', 'Refresh index', { ...busy, phase: 'failed', text: 'Failed' }, false)).toMatchObject({ mark: '✗', markColor: 'error' })
  expect(buttonLook('reindex', 'Refresh index', null, false)).toEqual({ mark: null, markColor: null, text: 'Refresh index', isPressed: false, dim: false })
  expect(pressedVariant('primary')).toBe('secondary')
  expect(pressedVariant('secondary')).toBe('primary')
})

// 2026-10-19, the owner: "when I click on them there is no response by the button that it has been clicked".
// Each press must show on the Button itself, in every server state a user meets, then return to rest.
const PRESS_OUTCOME = {
  server: /^✓ Started$/,
  stub: /^✗ Not ready$/,
  disconnected: /^✗ Failed$/,
  refused: /^✗ Failed$/,
} as const

for (const state of ['server', 'stub', 'disconnected', 'refused'] as const) {
  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface} (${state}): Refresh index shows its press on the Button, then returns to rest`, async ($, on) => {
      const rec = fake(on)
      if (state === 'stub') rec.stub = 'Installing ERLA 0.5.8: this takes a few minutes.'
      rec.down = state === 'disconnected'
      if (state === 'refused') rec.refuse = 'index_status'
      await $.session.start({ cwd: '/', surface, isInteractive: true })
      await $.command.run({ command: 'erla', args: '' } as never)
      const ui = await $.ui.mount({ plugin: 'erla', surface, component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
      await rec.clock.settle()
      // the engine waits a while for a server that is down, so the panel's opening status read (its
      // Buttons dim meanwhile) ends only then; the busy toast itself is tested with List files below
      if (state === 'disconnected') await rec.clock.advance(30000)

      expect((await ui.find({ type: 'Button', key: 'reindex' }))?.props).toMatchObject({ variant: 'primary', dimColor: false })
      expect(await ui.find({ type: 'Button', key: 'reindex', text: /^Refresh index$/ })).toBeDefined()

      await ui.press({ key: 'reindex' })
      const pressed = await ui.find({ type: 'Button', key: 'reindex', text: PRESS_OUTCOME[state] })
      expect(pressed).toBeDefined()
      expect(pressed?.props).toMatchObject({ variant: 'secondary' })              // drawn pressed
      expect(rec.mcp.includes('index_start {}')).toBe(state === 'server')

      await rec.clock.advance(FLASH_MS)
      expect(await ui.find({ type: 'Button', key: 'reindex', text: /^Refresh index$/ })).toBeDefined()
      expect((await ui.find({ type: 'Button', key: 'reindex' }))?.props).toMatchObject({ variant: 'primary' })
      await ui.unmount()
    })
  }
}

test('while List files runs: its Button shows ⏳, waiting Buttons dim, a press on one says why in a toast', async ($, on) => {
  const rec = fake(on)
  rec.slowFiles = 5000
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  await ui.input({ key: 'q', text: 'rivets', kind: 'change' })

  await ui.press({ key: 'files' })
  await rec.clock.settle()
  const running = await ui.find({ type: 'Button', key: 'files', text: /^⏳ Listing…$/ })
  expect(running?.props).toMatchObject({ variant: 'primary', dimColor: false })        // a secondary Button lights up
  for (const key of ['reindex', 'status', 'browse', 'export']) {
    expect((await ui.find({ type: 'Button', key }))?.props).toMatchObject({ dimColor: true })
  }
  for (const key of ['ask', 'clear']) {                                               // these never wait
    expect((await ui.find({ type: 'Button', key }))?.props).toMatchObject({ dimColor: false })
  }

  await ui.press({ key: 'reindex' })
  expect(rec.mcp.includes('index_start {}')).toBe(false)
  expect(rec.toasts).toContain('ERLA is busy: Listing files for "rivets". Press again when it finishes.')

  await rec.clock.advance(5000)
  expect(await ui.find({ type: 'Button', key: 'files', text: /^✓ 2 found$/ })).toBeDefined()
  expect((await ui.find({ type: 'Button', key: 'reindex' }))?.props).toMatchObject({ dimColor: false })
  await ui.press({ key: 'reindex' })
  expect(rec.mcp).toContain('index_start {}')
  await ui.unmount()
})

test('quick presses confirm on their Buttons too: Ask ERLA, Clear list, Export with nothing to export', async ($, on) => {
  const rec = fake(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()

  await ui.press({ key: 'ask' })
  expect(await ui.find({ type: 'Button', key: 'ask', text: /^✗ Type a question$/ })).toBeDefined()
  await ui.input({ key: 'q', text: 'Kt shoulder', kind: 'change' })
  await ui.press({ key: 'ask' })
  expect(await ui.find({ type: 'Button', key: 'ask', text: /^✓ Sent$/ })).toBeDefined()
  await ui.press({ key: 'export' })
  expect(await ui.find({ type: 'Button', key: 'export', text: /^✗ Nothing to export$/ })).toBeDefined()
  await ui.press({ key: 'clear' })
  expect(await ui.find({ type: 'Button', key: 'clear', text: /^✓ Cleared$/ })).toBeDefined()
  // a later press keeps its own confirmation: the earlier press's timer does not clear it
  await rec.clock.advance(FLASH_MS - 1)
  await ui.press({ key: 'clear' })
  await rec.clock.advance(1)
  expect(await ui.find({ type: 'Button', key: 'clear', text: /^✓ Cleared$/ })).toBeDefined()
  await rec.clock.advance(FLASH_MS)
  expect(await ui.find({ type: 'Button', key: 'clear', text: /^Clear list$/ })).toBeDefined()
  expect(rec.prompts).toHaveLength(1)
  await ui.unmount()
})

// ------------------------------------------------------------------ Engineer Tools (ADR-0003)
const BS = String.fromCharCode(92)

test('Engineer Tools helpers: rows read safely, badges, and only the user accepts (ET3)', () => {
  const rows = toolsFrom({ tools: [
    tool('beam', 'tested'),
    tool('lug', 'accepted', { running: true, url: 'http://127.0.0.1:8051/' }),
    tool('evil', 'accepted', { running: true, url: 'http://0.0.0.0:8052/' }),     // never a link off this computer
    { id: 'x', state: 'unknown' },                                                  // a row it cannot read
    tool('odd', 'draft', { accept_argv: [1, 2] }),
  ] })
  expect(rows.map(r => r.id)).toEqual(['beam', 'lug', 'evil', 'odd'])
  expect(rows[0]).toMatchObject({ name: 'I-beam Timoshenko analyzer', version: '1.0', canAccept: true, acceptArgv: ACCEPT_ARGV('beam'), limits: ['no lateral-torsional buckling'] })
  expect(rows[1]?.url).toBe('http://127.0.0.1:8051/')
  expect(rows[2]?.url).toBe(null)
  expect(rows[3]?.acceptArgv).toBe(null)
  expect(toolsFrom('nonsense')).toEqual([])

  // the browser opener: one of the three known commands, aimed at the tool's own address, else none
  const URL1 = 'http://127.0.0.1:8050/'
  const WIN = ['rundll32.exe', 'url.dll,FileProtocolHandler', URL1]
  expect(openerFrom(WIN, URL1)).toEqual(WIN)
  expect(openerFrom(['open', URL1], URL1)).toEqual(['open', URL1])
  expect(openerFrom(['xdg-open', URL1], URL1)).toEqual(['xdg-open', URL1])
  expect(openerFrom(WIN, null)).toBe(null)                                         // not running
  expect(openerFrom(['rundll32.exe', 'url.dll,FileProtocolHandler', 'http://evil.example/'], URL1)).toBe(null)
  expect(openerFrom(['cmd.exe', '/c', 'start', URL1], URL1)).toBe(null)            // a shell is never run
  expect(openerFrom(['open', '-a', 'Terminal', URL1], URL1)).toBe(null)
  expect(openerFrom([1, URL1], URL1)).toBe(null)
  expect(toolsFrom({ tools: [tool('beam', 'accepted', { running: true, url: URL1, open_argv: WIN })] })[0]?.openArgv).toEqual(WIN)
  expect(toolsFrom({ tools: [tool('beam', 'accepted', { running: true, url: 'http://0.0.0.0:8050/', open_argv: ['open', 'http://0.0.0.0:8050/'] })] })[0]?.openArgv).toBe(null)
  expect(toolBadge({ state: 'modified', running: false }).color).toBe('error')
  expect(toolBadge({ state: 'tested', running: false }).text).toMatch(/awaiting your acceptance/)

  // Claude's own shell calls that would accept a tool, in every spelling, are refused
  for (const command of [
    'erla tools accept beam --sha abc123',
    'erla.exe tools accept beam --sha abc123',
    `& "C:${BS}ERLA${BS}python.exe" -m erla.cli tools accept beam --sha abc`,
    'cd /tmp && python -m erla.cli tools accept beam --sha abc',
    'ERLA TOOLS ACCEPT beam',
  ]) {
    expect(acceptBlock('Bash', { command })).toMatch(/user's decision/)
    expect(acceptBlock('PowerShell', { command })).toMatch(/user's decision/)
    expect(acceptBlock('mcp__Windows-MCP__PowerShell', { command })).toMatch(/user's decision/)
  }
  // what stays allowed: listing, testing, other programs, other tools
  expect(acceptBlock('Bash', { command: 'erla tools list' })).toBe(null)
  expect(acceptBlock('Bash', { command: 'erla tools test beam' })).toBe(null)
  expect(acceptBlock('Bash', { command: 'git commit -m "tools accept docs"' })).toBe(null)
  expect(acceptBlock('Read', { command: 'erla tools accept beam' })).toBe(null)
})

test('a shell call from Claude that would accept a tool never reaches the shell', async ($, on) => {
  const rec = fake(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const res = await $.tool.call({ tool: 'Bash', command: 'erla tools accept beam --sha abc123' } as never)
  expect(JSON.stringify(res)).toMatch(/user's decision/)
  expect(rec.reached).not.toContain('Bash')
  await $.tool.call({ tool: 'Bash', command: 'erla tools list' } as never)
  expect(rec.reached).toContain('Bash')
})

for (const state of ['core', 'stub', 'disconnected'] as const) {
  test(`no Engineer Tools card and no engtools call when the server cannot offer them (${state})`, async ($, on) => {
    const rec = fake(on)
    if (state === 'stub') rec.stub = 'Installing ERLA 0.6.0: this takes a few minutes.'
    rec.down = state === 'disconnected'
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'erla', args: '' } as never)
    const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
    await rec.clock.settle()
    if (state === 'disconnected') await rec.clock.advance(30000)
    expect(await ui.find({ type: 'Text', text: /^Engineer Tools/ })).toBeUndefined()
    expect(rec.mcp.some(c => c.startsWith('engtools'))).toBe(false)
    await ui.unmount()
  })
}

test('Engineer Tools: Open starts an accepted tool; draft and changed tools cannot be opened', async ($, on) => {
  const rec = fake(on)
  rec.tools = { tools: [tool('beam', 'accepted'), tool('lug', 'draft'), tool('weld', 'modified')] }
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Text', text: /^Engineer Tools \(3\)$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /changed since accepted/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'tool-open-lug' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'tool-open-weld' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', key: 'tool-accept-beam' })).toBeUndefined()   // already accepted

  await ui.press({ key: 'tool-open-beam' })
  await rec.clock.settle()
  expect(rec.mcp).toContain('engtools_start {"tool_id":"beam"}')
  expect(await ui.find({ type: 'Text', text: /started: open http:\/\/127\.0\.0\.1:8050\// })).toBeDefined()
  expect(rec.runs).toEqual([])                                   // starting never runs a local command
  await ui.unmount()
})

test('Engineer Tools: Accept needs a second press, then runs the CLI the server named (ET3)', async ($, on) => {
  const rec = fake(on)
  rec.tools = { tools: [tool('beam', 'tested')] }
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Button', key: 'tool-open-beam' })).toBeUndefined()   // not accepted yet

  await ui.press({ key: 'tool-accept-beam' })
  await rec.clock.settle()
  expect(rec.runs).toEqual([])
  expect(await ui.find({ type: 'Text', text: /Press Accept again to confirm/ })).toBeDefined()

  await rec.clock.advance(FLASH_MS)
  expect(await ui.find({ type: 'Button', key: 'tool-accept-beam', text: /^Confirm accept$/ })).toBeDefined()
  await ui.press({ key: 'tool-accept-beam' })
  await rec.clock.settle()
  expect(rec.runs).toEqual([ACCEPT_ARGV('beam')])
  expect(await ui.find({ type: 'Text', text: /accepted: press Open/ })).toBeDefined()
  expect(rec.mcp.some(c => c.startsWith('engtools_accept'))).toBe(false)
  await ui.unmount()
})

test('Engineer Tools: a refused accept and a surface without commands are both said, never silent (L10)', async ($, on) => {
  const rec = fake(on)
  rec.tools = { tools: [tool('beam', 'tested')] }
  rec.runOut = { exitCode: 1, stdout: '{"ok": false, "error": "refused: the tests have not passed on these files"}', stderr: '' }
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  await ui.press({ key: 'tool-accept-beam' })
  await rec.clock.advance(FLASH_MS)
  await ui.press({ key: 'tool-accept-beam' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Text', text: /was not accepted: refused: the tests have not passed/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'tool-accept-beam', text: /Failed/ })).toBeDefined()

  rec.runFails = true
  await rec.clock.advance(FLASH_MS)
  await ui.press({ key: 'tool-accept-beam' })
  await rec.clock.advance(FLASH_MS)
  await ui.press({ key: 'tool-accept-beam' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Text', text: /cannot run commands.*erla tools accept beam/ })).toBeDefined()
  await ui.unmount()
})

// 2026-10-20: settings had no allow rule for engtools_list; the card said "Reading your tools…" for good
// while the refusal sat in the panel's message line. The card must say it and name the rule.
test('Engineer Tools: a refused list is said in the card with the rule to add, then Refresh list recovers', async ($, on) => {
  const rec = fake(on)
  rec.tools = { tools: [tool('beam', 'tested')] }
  rec.refuse = 'engtools_list'
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Text', text: /add the permission rule mcp__plugin_erla_erla__engtools_list/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Reading your tools…$/ })).toBeUndefined()   // the old card's endless state

  await ui.press({ key: 'tools-refresh' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Button', key: 'tools-refresh', text: /Failed/ })).toBeDefined()

  rec.refuse = ''                                                // the user added the rule
  await rec.clock.advance(FLASH_MS)
  await ui.press({ key: 'tools-refresh' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Text', text: /^I-beam Timoshenko analyzer$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /blocked ERLA's engtools_list/ })).toBeUndefined()
  await ui.unmount()
})

// 2026-10-20: the running tool's card had only a Link to http://127.0.0.1:8050/, which the desktop app does
// not open (its links open https addresses), so the user could not reach the tool's UI at all
test('Engineer Tools: a running tool opens in the browser through the server\'s opener, and says a failure', async ($, on) => {
  const rec = fake(on)
  const url = 'http://127.0.0.1:8050/'
  const opener = ['rundll32.exe', 'url.dll,FileProtocolHandler', url]
  rec.tools = { tools: [tool('beam', 'accepted', { running: true, url, open_argv: opener })] }
  rec.runOut = { exitCode: 0, stdout: '', stderr: '' }
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Button', key: 'tool-open-beam' })).toBeUndefined()      // already running
  expect(await ui.find({ type: 'Button', key: 'tool-copy-beam' })).toBeDefined()

  await ui.press({ key: 'tool-browse-beam' })
  await rec.clock.settle()
  expect(rec.runs).toEqual([opener])
  expect(await ui.find({ type: 'Text', text: /opened in your browser at http:\/\/127\.0\.0\.1:8050\// })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'tool-browse-beam', text: /Opened/ })).toBeDefined()

  // a surface that runs no commands: said, with the address to open by hand (L10)
  rec.runFails = true
  await rec.clock.advance(FLASH_MS)
  await ui.press({ key: 'tool-browse-beam' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Text', text: /Could not open your browser .*open http:\/\/127\.0\.0\.1:8050\/ yourself/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'tool-browse-beam', text: /Failed/ })).toBeDefined()
  await ui.unmount()
})

test('Engineer Tools: an opener the panel does not know is never run', async ($, on) => {
  const rec = fake(on)
  const url = 'http://127.0.0.1:8050/'
  rec.tools = { tools: [tool('beam', 'accepted', { running: true, url, open_argv: ['cmd.exe', '/c', 'start', url] })] }
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  await ui.press({ key: 'tool-browse-beam' })
  await rec.clock.settle()
  expect(rec.runs).toEqual([])
  expect(await ui.find({ type: 'Text', text: /did not say how to open a browser here/ })).toBeDefined()
  await ui.unmount()
})

test('Engineer Tools: an auto-mode refusal of Open shows on the Button', async ($, on) => {
  const rec = fake(on)
  rec.tools = { tools: [tool('beam', 'accepted')] }
  rec.refuse = 'engtools_start'
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await rec.clock.settle()
  await ui.press({ key: 'tool-open-beam' })
  await rec.clock.settle()
  expect(await ui.find({ type: 'Button', key: 'tool-open-beam', text: /Failed/ })).toBeDefined()
  await ui.unmount()
})
