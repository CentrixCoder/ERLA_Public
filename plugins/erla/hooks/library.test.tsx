import { expect, mock, test } from 'claude-code/testing'

import type { RenderPropsOf } from 'claude-code'

import { explain, splitPaths } from './library'

const PANE_PROPS = {} as unknown as RenderPropsOf['Pane']
const ROOT = 'D:\\Local Files\\L\\Library\\Engineering Library'
const ADDED = 'E:\\Standards\\MIL-HDBK'

test('typed paths and auto-mode refusals', () => {
  expect(splitPaths(' "E:\\A b" ; F:\\C\n\n')).toEqual(['E:\\A b', 'F:\\C'])
  expect(splitPaths(' ; ')).toEqual([])
  expect(explain('remove_library_folder', new Error('HooksError: ... refused: The server-side auto mode classifier ...')))
    .toMatch(/add the permission rule mcp__plugin_erla_erla__remove_library_folder/)
  // auto mode also refuses Claude editing its own permissions, so the message never sends you to Claude in it
  expect(explain('remove_library_folder', new Error('refused'))).not.toMatch(/Ask Claude/)
  expect(explain('remove_library_folder', new Error('refused'))).toMatch(/\/erla:setup outside auto mode/)
  expect(explain('site_info', new Error('boom'))).toBe('site_info failed: boom')
})

// a fake ERLA: the folder list, which tools were called with what, and an index job state
function fakeErla(on: Parameters<Parameters<typeof test>[1]>[1]) {
  mock.clock(on)
  const erla = { roots: [ROOT], calls: [] as string[], job: 'done', refuse: '' }
  on('tool.list', () => ({ value: [] }))
  on('mcp.connect', () => ({ value: { isConnected: true, server: 'plugin:erla:erla' } }))
  on('mcp.call', ($, e) => {
    const args = (e.args ?? {}) as { paths?: string[]; path?: string }
    if (e.tool !== 'index_status' || erla.job !== 'done') erla.calls.push(`${e.tool} ${JSON.stringify(args)}`)
    if (e.tool === erla.refuse) return { deny: 'The server-side auto mode classifier gave no verdict' }
    let out: unknown = {}
    if (e.tool === 'site_info') {
      out = { configured: true, library_roots: erla.roots, library_probe: { files_on_disk: 3660, documents_indexed: 3660, new: 0, missing: 0 } }
    } else if (e.tool === 'add_library_folders') {
      const picked = args.paths ?? [ADDED] // no paths: the picker returns ADDED
      erla.roots = [...erla.roots, ...picked]
      out = { added: true, summary: `${picked.length} folder(s) added`, index_job: null }
    } else if (e.tool === 'remove_library_folder') {
      erla.roots = erla.roots.filter(r => r !== args.path)
      out = { removed: true, summary: '1 folder removed' }
    } else if (e.tool === 'index_status') {
      out = { status: { state: erla.job, phase: null }, log_tail: [] }
    } else if (e.tool === 'index_start') {
      erla.job = 'running'
      out = { started: 'now', state: 'running' }
    }
    return { value: { content: [{ type: 'text', text: JSON.stringify(out) }], isError: false } }
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
  return erla
}

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: /erla Library card lists the folders, adds by picker and by path, then indexes`, async ($, on) => {
    const erla = fakeErla(on)
    await $.session.start({ cwd: '/', surface, isInteractive: true })
    const opened = await $.command.run({ command: 'erla', args: '' } as never)
    expect(JSON.stringify(opened)).toMatch(/opened above the prompt/)

    const ui = await $.ui.mount({ plugin: 'erla', surface, component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
    expect(await ui.find({ type: 'Text', text: /Engineering Library/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^3,660 files on disk$/ })).toBeDefined()
    expect(await ui.find({ key: 'index' })).toBe(undefined)

    // Browse… asks ERLA's picker (no paths)
    await ui.press({ key: 'browse' })
    expect(erla.calls).toContain('add_library_folders {}')
    expect(await ui.find({ type: 'Text', text: /MIL-HDBK/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Added\. 1 folder\(s\) added/ })).toBeDefined()

    // a typed list of paths
    await ui.input({ key: 'add-path', text: '"F:\\Reports" ; G:\\Tests' })
    expect(erla.calls).toContain('add_library_folders {"paths":["F:\\\\Reports","G:\\\\Tests"]}')
    expect(await ui.find({ type: 'Text', text: /G:\\Tests/ })).toBeDefined()

    // the change waits for an index run until asked
    await ui.press({ key: 'index' })
    expect(erla.calls).toContain('index_start {}')
    expect(await ui.find({ type: 'Text', text: /Index refresh started/ })).toBeDefined()
    expect(await ui.find({ key: 'index' })).toBe(undefined)
    await ui.unmount()
  })
}

test('Remove takes a second press, and a running job defers the index run', async ($, on) => {
  const erla = fakeErla(on)
  erla.roots = [ROOT, ADDED]
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'desktop', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })

  await ui.press({ key: `rm:${ADDED}` })
  expect(erla.calls.some(c => c.startsWith('remove_library_folder'))).toBe(false)
  expect(await ui.find({ type: 'Button', text: /Confirm remove/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Its files are not touched/ })).toBeDefined()

  await ui.press({ key: `rm:${ADDED}` })
  expect(erla.calls).toContain(`remove_library_folder ${JSON.stringify({ path: ADDED })}`)
  expect(await ui.find({ type: 'Text', text: /MIL-HDBK/ })).toBe(undefined)

  erla.job = 'running'
  await ui.press({ key: 'index' })
  expect(erla.calls.some(c => c.startsWith('index_start'))).toBe(false)
  expect(await ui.find({ type: 'Text', text: /already running/ })).toBeDefined()
  await ui.unmount()
})

test('an auto-mode refusal says which permission rule to add', async ($, on) => {
  const erla = fakeErla(on)
  erla.refuse = 'add_library_folders'
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'erla', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'erla', surface: 'terminal', component: 'Pane', props: PANE_PROPS, requestId: 'erla-panel' })
  await ui.press({ key: 'browse' })
  expect(await ui.find({ type: 'Text', text: /add the permission rule mcp__plugin_erla_erla__add_library_folders/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Waiting for the folder picker/ })).toBe(undefined)
  await ui.unmount()
})
