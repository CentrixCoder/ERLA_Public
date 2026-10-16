import { expect, mock, test } from 'claude-code/testing'

import type { RenderPropsOf } from 'claude-code'

import {
  askPrompt, filesFrom, openPrompt, pageNumber, parseStats, preferencesSection, readSettings, referencesMarkdown, rerankerArgs, resultText,
  scopeBlock, sourceFromShow,
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

  expect(readSettings(undefined)).toEqual({ units: 'config', semantic: true, excludeRestricted: false, scope: 'both', reranker: 'default' })
  expect(readSettings({ units: 'SI', semantic: 'yes', scope: 'local' })).toEqual({ units: 'SI', semantic: true, excludeRestricted: false, scope: 'local', reranker: 'default' })
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
  mock.clock(on)
  const rec = { mcp: [] as string[], searches: [] as Record<string, unknown>[], prompts: [] as string[], store: {} as Record<string, unknown>, refuse: '', reached: [] as string[], gpuReady: false }
  on('tool.list', () => ({ value: [] }))
  on('mcp.connect', () => ({ value: { isConnected: true, server: 'plugin:erla:erla' } }))
  on('mcp.call', ($, e) => {
    if (e.tool !== 'index_status') rec.mcp.push(`${e.tool} ${JSON.stringify(e.args ?? {})}`)
    if (e.tool === rec.refuse) return { deny: 'The server-side auto mode classifier gave no verdict' }
    const out: Record<string, unknown> = {
      library_stats: { text: STATS },
      site_info: { unit_system: 'US', library_roots: ['D:\\Lib'], erla_version: '0.2.9' },
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
    if (/library_search$/.test(e.tool)) rec.searches.push({ ...e })
    const text = /library_show$/.test(e.tool) ? (e as { doc_id?: number }).doc_id === 99 ? SHOW_ITAR : SHOW : '{}'
    return { result: { content: [{ type: 'text', text }] } } as never
  })
  on('prompt.submit', ($, e) => {
    rec.prompts.push(e.text)
    return { text: e.text }
  })
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'You are Claude.', scope: 'shared' as const }] }))
  on('store.get', ($, e) => ({ value: rec.store[e.key] }))
  on('store.set', ($, e) => {
    rec.store[e.key] = e.value
    return { value: undefined }
  })
  on('command.register', () => ({ value: { command: 'x' } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
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
  expect(rec.store.settings).toEqual({ units: 'SI', semantic: false, excludeRestricted: true, scope: 'both', reranker: 'default' })
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
