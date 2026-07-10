/**
 * Shared building blocks for scenarios. Prefer the command palette (stable action
 * labels) over brittle menu/button chains.
 */

// Open the palette (Ctrl+K) and click the action whose label matches. Blurs any
// focused input/terminal first (xterm swallows Ctrl+K), and waits for the palette
// to close after — so rapid successive palette actions don't race a reopen.
async function cmd(ctx, labelRe) {
  await ctx.page.evaluate(() => document.activeElement && document.activeElement.blur && document.activeElement.blur())
  await ctx.press('Control+k')
  await ctx.waitFor('.palette__input', 4000)
  const item = ctx.page.locator('.palette__item', { hasText: labelRe }).first()
  await item.waitFor({ state: 'visible', timeout: 4000 })
  await item.click()
  await ctx.page.locator('.palette').first().waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {})
}

// Create a session via the modal.
async function newSession(ctx, name) {
  await cmd(ctx, /New session/i)
  await ctx.waitFor('.modal--small input', 4000)
  await ctx.fill('.modal--small input', name)
  await ctx.click('.modal--small .ai-btn--primary')
  await ctx.gone('.modal--small', 4000).catch(() => {})
}

// Open the attach modal, filter to `pid`, optionally toggle monitor/ebpf, attach.
// Returns after the modal closes. Throws if the target row never appears.
async function attachPid(ctx, pid, opts = {}) {
  await cmd(ctx, /Attach to running process/i)
  await ctx.waitFor('.attach__search', 5000)
  await ctx.fill('.attach__search', String(pid))
  if (opts.window) await ctx.fill('.attach__window input', String(opts.window))
  if (opts.monitor) await ctx.page.locator('.attach__monitor input[type=checkbox]').first().check().catch(() => {})
  if (opts.ebpf) {
    const box = ctx.page.locator('.attach__monitor input[type=checkbox]').nth(1)
    // the ebpf checkbox is disabled until GET /runs/attach/ebpf-capabilities resolves
    for (let i = 0; i < 15; i++) { if (await box.isEnabled().catch(() => false)) break; await ctx.sleep(300) }
    ctx.assert(await box.isEnabled().catch(() => false), 'eBPF checkbox stayed disabled (capabilities?)')
    await box.check()
  }
  if (opts.requests) {
    const box = ctx.page.locator('.attach__monitor input[type=checkbox]').nth(2)
    // disabled until GET /runs/attach/request-capabilities resolves
    for (let i = 0; i < 15; i++) { if (await box.isEnabled().catch(() => false)) break; await ctx.sleep(300) }
    ctx.assert(await box.isEnabled().catch(() => false), 'Request-tracing checkbox stayed disabled (capabilities?)')
    await box.check()
  }
  const row = ctx.page.locator('.attach__row', { hasText: `pid ${pid}` }).first()
  await row.waitFor({ state: 'visible', timeout: 8000 })
  await row.click()
  await ctx.gone('.attach__list', 8000).catch(() => {})
}

// Open settings (optionally a section already routed by the palette 'guide' item).
const openSettings = (ctx) => cmd(ctx, /Open settings/i)
const toggleTheme = (ctx) => cmd(ctx, /Toggle theme/i)
const toggleTracing = (ctx) => cmd(ctx, /Turn terminal tracing (ON|OFF)/i)

// Click the sidebar run row for a specific pid (deterministic when many runs exist).
async function openRunByPid(ctx, pid) {
  const row = ctx.page.locator('.run-row', { hasText: `pid ${pid}` }).first()
  await row.waitFor({ state: 'visible', timeout: 8000 })
  await row.click()
}

// Right-click a run row (the pid's row, or the first) for its context menu.
async function runContextMenu(ctx, pid) {
  const row = pid
    ? ctx.page.locator('.run-row', { hasText: `pid ${pid}` }).first()
    : ctx.page.locator('.run-row').first()
  await row.waitFor({ state: 'visible', timeout: 8000 })
  await row.click({ button: 'right' })
}

// Confirm the styled delete dialog that follows clicking the sidebar's "Delete" item
// (window.confirm is gone — it's a real modal now, same as every other dialog).
async function confirmDeleteRun(ctx) {
  await ctx.waitFor('.modal--small .ai-btn--danger', 4000)
  await ctx.click('.modal--small .ai-btn--danger')
  await ctx.gone('.modal--small', 4000).catch(() => {})
}

// Trigger an in-app MenuBar item (for actions with no palette entry / renderer shortcut).
async function menu(ctx, topRe, itemRe) {
  await ctx.page.locator('.menubar__top', { hasText: topRe }).first().click()
  await ctx.page.locator('.menubar__menuitem', { hasText: itemRe }).first().click()
}

// How many runs exist per the backend (ground truth).
const runCount = async (ctx) => (await ctx.api.get('/runs?limit=500')).length

module.exports = { cmd, newSession, attachPid, openSettings, toggleTheme, toggleTracing,
                   runContextMenu, confirmDeleteRun, openRunByPid, menu, runCount }
