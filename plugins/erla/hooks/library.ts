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

// an auto-mode refusal reads as a wall of text: say what to allow instead. The engine checks
// the panel's $.mcp.call against settings rules and auto mode, and no plugin hook can answer
// for it (tested 2026-10-14, engine 2.1.295), so the fix is the rule; /erla:setup offers to add
// the panel's rules. Not "ask Claude to add it": auto mode refuses Claude editing its own
// permissions too.
export function explain(tool: string, err: unknown): string {
  const text = err instanceof Error ? err.message : String(err)
  if (/auto mode classifier|refused/i.test(text)) {
    return `Claude Code's auto mode blocked ERLA's ${tool} tool. Run /erla:setup outside auto mode to add the ` +
      `panel's permission rules, or add the permission rule mcp__plugin_erla_erla__${tool} to permissions.allow ` +
      'in your Claude Code settings yourself, then try again.'
  }
  return `${tool} failed: ${text}`
}
