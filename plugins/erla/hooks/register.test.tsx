import { expect, mock, test } from 'claude-code/testing'

import type { RenderPropsOf } from 'claude-code'

import { bar, clock, eta, lastCount, lastItem, per } from './register'

// the layout fields the band reads; scroll/view are engine-owned and unused here
const BAND_PROPS = { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100 } as unknown as RenderPropsOf['AbovePrompt']

test('parses the latest count from each phase log format', () => {
  expect(lastCount(['  100/3654 extracted (0.7 files/s)', 'MuPDF error: x', '  700/3654 extracted (2.8 files/s)']))
    .toEqual({ done: 700, total: 3654 })
  expect(lastCount(['  ocr 5/120: Std/MIL/2024/x.pdf (3 chunks)'])).toEqual({ done: 5, total: 120 })
  expect(lastCount(['  12/300 PDFs, 4 tables, 9 property records'])).toEqual({ done: 12, total: 300 })
  expect(lastCount(['  1200/50000 (85 chunks/s)'])).toEqual({ done: 1200, total: 50000 })
  expect(lastCount(['MuPDF error: format error'])).toBe(null)
  expect(bar(0.5, 10)).toBe('█████░░░░░')
  expect(eta(1200)).toBe('ETA 20 min')
})

test('band shows while ERLA indexes, then hides with a toast when done', async ($, on) => {
  const clock = mock.clock(on)
  let state = 'running'
  let tail = ['  700/3654 extracted (2.8 files/s)']
  const toasts: string[] = []
  const calls: string[] = []

  on('tool.list', () => ({
    value: [{ name: 'mcp__remote-devices__plugin_erla_erla__index_status', description: '', mcp: true }],
  }))
  on('mcp.call', ($, e) => {
    calls.push(`${e.server}|${e.tool}`)
    return {
      value: {
        content: [{ type: 'text', text: JSON.stringify({ status: { state, phase: 'index' }, log_tail: tail }) }],
        isError: false,
      },
    }
  })
  on('command.register', () => ({ value: { command: 'erla-progress' } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine band</Text>
  })
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(1000)
  expect(calls[0]).toBe('remote-devices|plugin_erla_erla__index_status')

  tail = ['  1100/3654 extracted (3.0 files/s)']
  await clock.advance(4000)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'erla',
      surface,
      component: 'AbovePrompt',
      props: BAND_PROPS,
    })
    expect(await ui.find({ type: 'Text', text: /ERLA indexing/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /30%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1,100\/3,654 files/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /ETA/ })).toBeDefined()
    await ui.unmount()
  }

  tail = ['MuPDF error: format error: cmsOpenProfileFromMem failed', '']
  await clock.advance(4000)
  const keep = await $.ui.mount({ plugin: 'erla', surface: 'terminal', component: 'AbovePrompt', props: BAND_PROPS })
  expect(await keep.find({ type: 'Text', text: /1,100\/3,654 files/ })).toBeDefined()
  await keep.unmount()

  state = 'done'
  await clock.advance(4000)
  expect(toasts).toEqual(['ERLA: indexing finished'])

  const ui = await $.ui.mount({
    plugin: 'erla',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: BAND_PROPS,
  })
  expect(await ui.find({ type: 'Text', text: /ERLA indexing/ })).toBe(undefined)
  expect(await ui.find({ type: 'Text', text: /engine band/ })).toBeDefined()
})

test('polls the server $.mcp.connect names, even when tool.list has no erla tools (deferred)', async ($, on) => {
  const clock = mock.clock(on)
  const calls: string[] = []
  on('tool.list', () => ({ value: [] }))
  on('mcp.connect', () => ({ value: { isConnected: true, server: 'plugin:erla:erla' } }))
  on('mcp.call', ($, e) => {
    calls.push(`${e.server}|${e.tool}`)
    return {
      value: {
        content: [{ type: 'text', text: JSON.stringify({ status: { state: 'running', phase: 'ocr' }, log_tail: ['  ocr 1/137: a.pdf (41 chunks)'] }) }],
        isError: false,
      },
    }
  })
  on('command.register', () => ({ value: { command: 'erla-progress' } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine band</Text>
  })

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(1000)
  expect(calls[0]).toBe('plugin:erla:erla|index_status')

  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'AbovePrompt', props: BAND_PROPS })
  expect(await ui.find({ type: 'Text', text: /1\/137 PDFs/ })).toBeDefined()
})

test('helpers for the slow OCR phase', () => {
  expect(lastItem(['  ocr 22/137: Joints - Bolted/Fasteners and Joints/19900009424.pdf (100 chunks)', 'MuPDF error: x']))
    .toBe('19900009424.pdf')
  expect(lastItem(['  700/3654 extracted (2.8 files/s)'])).toBe(null)
  expect(clock(80)).toBe('1m 20s')
  expect(clock(3 * 3600 + 5 * 60)).toBe('3h 05m')
  expect(per(1 / 180, 'PDFs')).toBe('3m 00s per PDF')
  expect(per(2.8, 'files')).toBe('2.8 files/s')
})

test('during slow OCR the bar ticks every second, then shows a rate and ETA once items finish', async ($, on) => {
  const clk = mock.clock(on)
  let tail = ['  ocr 22/137: a/b/first.pdf (100 chunks)']
  on('tool.list', () => ({ value: [] }))
  on('mcp.connect', () => ({ value: { isConnected: true, server: 'plugin:erla:erla' } }))
  on('mcp.call', () => ({
    value: {
      content: [{ type: 'text', text: JSON.stringify({ status: { state: 'running', phase: 'ocr' }, log_tail: tail }) }],
      isError: false,
    },
  }))
  on('command.register', () => ({ value: { command: 'erla-progress' } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine band</Text>
  })
  const mount = () => $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'AbovePrompt', props: BAND_PROPS })

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clk.advance(1000)
  let ui = await mount()
  expect(await ui.find({ type: 'Text', text: /22\/137 PDFs · ETA after the next item/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /PDF 23 running \ds · last done: first\.pdf/ })).toBeDefined()
  await ui.unmount()

  // no new log line, yet the item clock counts up between polls
  await clk.advance(5000)
  ui = await mount()
  expect(await ui.find({ type: 'Text', text: /PDF 23 running [5-6]s/ })).toBeDefined()
  await ui.unmount()

  // two PDFs finish ~3 minutes apart: a rate and an ETA appear; the item still running does not count
  tail = ['  ocr 23/137: a/b/second.pdf (40 chunks)']
  await clk.advance(175000)
  tail = ['  ocr 24/137: a/b/third.pdf (40 chunks)']
  await clk.advance(180000)
  ui = await mount()
  expect(await ui.find({ type: 'Text', text: /24\/137 PDFs · 2m 5\ds per PDF · ETA 5\.\d h/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /PDF 25 running 2m 5\ds · last done: third\.pdf/ })).toBeDefined()
  await ui.unmount()
})

test('starting an index job from Claude shows the bar within seconds, even if it was hidden', async ($, on) => {
  const clock = mock.clock(on)
  let state = 'done'
  on('tool.list', () => ({
    value: [{ name: 'mcp__plugin_erla_erla__index_status', description: '', mcp: true }],
  }))
  on('mcp.call', ($, e) => ({
    value: {
      content: [{ type: 'text', text: JSON.stringify({ status: { state, phase: 'ocr' }, log_tail: ['  ocr 3/120: a/b.pdf (2 chunks)'] }) }],
      isError: false,
    },
  }))
  on('command.register', () => ({ value: { command: 'erla-progress' } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine band</Text>
  })
  on('tool.call', () => ({ result: { content: [{ type: 'text', text: '{"started": true}' }] } as never }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(1000)
  await $.command.run({ command: 'erla-progress', args: '' } as never) // person hid the bar earlier

  state = 'running'
  await $.tool.call({ tool: 'mcp__plugin_erla_erla__index_start', ocr: true } as never)
  await clock.advance(2000) // well before the 20 s idle poll

  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'AbovePrompt', props: BAND_PROPS })
  expect(await ui.find({ type: 'Text', text: /OCR of scanned PDFs \(step 2\/4\)/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /3\/120 PDFs/ })).toBeDefined()
})
