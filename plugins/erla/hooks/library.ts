// Pure helpers for the /erla-library dialog in register.tsx (the engine follows $ only within
// one file, so the dialog's hooks live there).

export type SiteInfo = {
  unit_system?: string
  erla_version?: string
  configured?: boolean
  library_roots?: string[]
  error?: string
  library_probe?: { files_on_disk?: number; documents_indexed?: number; new?: number; missing?: number }
}
export type AddResult = { added?: boolean; picker?: string; summary?: string; message?: string; error?: string; index_job?: unknown }
export type RemoveResult = { removed?: boolean; summary?: string; error?: string }
export type StatusResult = { status?: { state?: string } }

// "a; b" or one path per line -> ["a", "b"]; surrounding quotes from a copied path dropped
export function splitPaths(text: string): string[] {
  return text
    .split(/[;\n]/)
    .map(p => p.trim().replace(/^"(.*)"$/, '$1').trim())
    .filter(Boolean)
}

// The erla server answers with only erla_setup_status while ERLA's runtime installs, and keeps
// doing so after the install until it is reconnected. The engine then rejects a call with
// '$.mcp.call: no connected MCP tool "site_info" on a server named "plugin:erla:erla"'.
export const SETUP_ERROR = 'ErlaSetup'
export const MISSING_TOOL = /no connected MCP tool/i
// Since 0.4.2 the stub hands over to the real server by itself. When that has not happened, only quitting
// and reopening the app restarted the server; /reload-plugins and /mcp Reconnect did not (tested 2026-10-16)
export const RELOAD_HINT = 'The library tools appear by themselves when the install finishes. If they have not, quit and ' +
  'reopen the Claude app (in a terminal, restart claude); /reload-plugins and /mcp Reconnect do not restart a running ' +
  'server. Then press Check again.'

export function setupError(): Error {
  const err = new Error('ERLA is not ready yet: see Setup at the top of this panel.')
  err.name = SETUP_ERROR
  return err
}

// erla_setup_status' report -> the panel's Setup card
export function setupFrom(text: string | null): { text: string; done: boolean } {
  if (text === null) {
    return { text: `The erla server is connected but offers no ERLA tools (it may be restarting). ${RELOAD_HINT}`, done: false }
  }
  return { text, done: /Install finished/i.test(text) }
}

// an auto-mode refusal reads as a wall of text: say what to allow instead. The engine checks
// the panel's $.mcp.call against settings rules and auto mode, and no plugin hook can answer
// for it (tested 2026-10-14, engine 2.1.295), so the fix is the rule; /erla:setup offers to add
// the panel's rules. Not "ask Claude to add it": auto mode refuses Claude editing its own
// permissions too.
export function explain(tool: string, err: unknown): string {
  const text = err instanceof Error ? err.message : String(err)
  if (err instanceof Error && err.name === SETUP_ERROR) return text
  if (/auto mode classifier|refused/i.test(text)) {
    return `Claude Code's auto mode blocked ERLA's ${tool} tool. Run /erla:setup outside auto mode to add the ` +
      `panel's permission rules, or add the permission rule mcp__plugin_erla_erla__${tool} to permissions.allow ` +
      'in your Claude Code settings yourself, then try again.'
  }
  return `${tool} failed: ${text}`
}
