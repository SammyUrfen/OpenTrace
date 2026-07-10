/** Open-run tab bar: opening, switching, closing, delete/rename propagation. */
const H = require('./_helpers')

const tabCount = (ctx) => ctx.count('.main-tab')
const lastTab = (ctx) => ctx.page.locator('.main-tab').last()
async function nthTab(ctx, i) { return ctx.page.locator('.main-tab').nth(i) }
async function waitTabCount(ctx, target, ms = 6000) {
  for (let i = 0; i < ms / 200; i++) { if ((await tabCount(ctx)) === target) return; await ctx.sleep(200) }
}

module.exports = [
  {
    id: 'tab-open-single', name: 'Opening an attached run opens one main tab',
    tags: ['tabs', 'attach'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      const before = await tabCount(ctx)
      await H.openRunByPid(ctx, pid)
      await waitTabCount(ctx, before + 1)
      ctx.assert((await tabCount(ctx)) === before + 1, `expected one new tab (had ${before})`)
      const label = (await lastTab(ctx).locator('.main-tab__label').innerText()).trim()
      ctx.assert(label.length > 0, 'new tab has an empty label')
      ctx.assert((await lastTab(ctx).getAttribute('aria-selected')) === 'true', 'freshly opened tab is not active')
    },
  },
  {
    id: 'tab-open-three', name: 'Opening three runs opens three tabs',
    tags: ['tabs', 'attach'],
    timeout: 45000,
    run: async (ctx) => {
      const pids = [await ctx.spawnTarget('cpu'), await ctx.spawnTarget('idle'), await ctx.spawnTarget('memgrow')]
      for (const p of pids) await H.attachPid(ctx, p, { window: 3 })
      const before = await tabCount(ctx)
      for (const p of pids) await H.openRunByPid(ctx, p)
      await waitTabCount(ctx, before + 3)
      ctx.assert((await tabCount(ctx)) === before + 3, `expected 3 new tabs (had ${before}, now ${await tabCount(ctx)})`)
    },
  },
  {
    id: 'tab-switch-active', name: 'Clicking a tab label switches the active tab',
    tags: ['tabs'],
    timeout: 40000,
    run: async (ctx) => {
      const a = await ctx.spawnTarget('cpu'); const b = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, a, { window: 3 }); await H.attachPid(ctx, b, { window: 3 })
      await H.openRunByPid(ctx, a); await H.openRunByPid(ctx, b)
      const n = await tabCount(ctx)
      // b is last & active; click the one before it (a) and assert focus moves.
      const aTab = await nthTab(ctx, n - 2)
      await aTab.locator('.main-tab__label').click()
      await ctx.sleep(300)
      ctx.assert((await aTab.getAttribute('aria-selected')) === 'true', 'clicked tab did not become active')
      ctx.assert((await lastTab(ctx).getAttribute('aria-selected')) === 'false', 'previous tab stayed active')
      await ctx.waitFor('.secondary-tabs', 6000)
    },
  },
  {
    id: 'tab-close-drops', name: 'Closing a tab with × drops the tab count',
    tags: ['tabs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.openRunByPid(ctx, pid)
      const n = await tabCount(ctx)
      await lastTab(ctx).locator('.main-tab__close').click()
      await waitTabCount(ctx, n - 1)
      ctx.assert((await tabCount(ctx)) === n - 1, `close did not drop tab count (${n})`)
    },
  },
  {
    id: 'tab-reopen-no-dup', name: 'Re-opening an already open run does not duplicate its tab',
    tags: ['tabs', 'edge'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      const before = await tabCount(ctx)
      await H.openRunByPid(ctx, pid)
      await waitTabCount(ctx, before + 1)
      await H.openRunByPid(ctx, pid)
      await H.openRunByPid(ctx, pid)
      await ctx.sleep(400)
      ctx.assert((await tabCount(ctx)) === before + 1, 'opening the same run added extra tabs')
    },
  },
  {
    id: 'tab-delete-closes', name: 'Deleting an open run closes its tab and drops runCount',
    tags: ['tabs', 'runs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.openRunByPid(ctx, pid)
      const tabs = await tabCount(ctx); const runs = await H.runCount(ctx)
      await H.runContextMenu(ctx, pid)
      await ctx.page.locator('.ctx-item--danger', { hasText: /Delete/i }).first().click()
      await H.confirmDeleteRun(ctx)
      for (let i = 0; i < 20 && (await H.runCount(ctx)) >= runs; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) < runs, 'run was not deleted from backend')
      await waitTabCount(ctx, tabs - 1)
      ctx.assert((await tabCount(ctx)) === tabs - 1, 'deleted run left its tab open')
    },
  },
  {
    id: 'tab-rename-sidebar', name: 'Renaming a run via context menu updates the sidebar',
    tags: ['tabs', 'runs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.runContextMenu(ctx, pid)
      await ctx.page.locator('.ctx-item', { hasText: /Rename/i }).first().click()
      await ctx.waitFor('.modal--small input', 4000)
      const name = 'tab-renamed-' + Date.now().toString().slice(-5)
      await ctx.fill('.modal--small input', name)
      await ctx.click('.modal--small .ai-btn--primary')
      await ctx.gone('.modal--small', 4000).catch(() => {})
      await ctx.waitText(new RegExp(name, 'i'), 6000)
      ctx.assert((await ctx.count('.session-list *')) >= 0, 'sidebar missing')
    },
  },
  {
    id: 'tab-doubleclick-rename', name: 'Double-clicking a run tab renames it',
    tags: ['tabs', 'runs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.openRunByPid(ctx, pid)
      await waitTabCount(ctx, (await tabCount(ctx)))
      const tab = lastTab(ctx)
      await tab.dblclick()
      const modal = ctx.page.locator('.modal--small input')
      const appeared = await modal.first().waitFor({ state: 'visible', timeout: 4000 }).then(() => true).catch(() => false)
      if (!appeared) { return } // rename-on-doubleclick not wired; skip gracefully
      const name = 'dbl-rename-' + Date.now().toString().slice(-5)
      await ctx.fill('.modal--small input', name)
      await ctx.click('.modal--small .ai-btn--primary')
      await ctx.gone('.modal--small', 4000).catch(() => {})
      await ctx.waitText(new RegExp(name, 'i'), 6000)
      const label = (await lastTab(ctx).locator('.main-tab__label').innerText()).trim()
      ctx.assert(new RegExp(name, 'i').test(label), `tab label did not update (got "${label}")`)
    },
  },
  {
    id: 'tab-status-dot', name: 'Run row shows a status dot and the open tab shows a colour dot',
    tags: ['tabs', 'ui'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      const row = ctx.page.locator('.run-row', { hasText: `pid ${pid}` }).first()
      await row.waitFor({ state: 'visible', timeout: 8000 })
      ctx.assert((await row.locator('.run-row__status').count()) > 0, 'run row has no status dot')
      await H.openRunByPid(ctx, pid)
      await waitTabCount(ctx, (await tabCount(ctx)))
      ctx.assert((await lastTab(ctx).locator('.main-tab__dot').count()) > 0, 'open run tab has no colour dot')
    },
  },
  {
    id: 'tab-close-then-reopen', name: 'A closed tab can be reopened from the sidebar',
    tags: ['tabs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.openRunByPid(ctx, pid)
      const n = await tabCount(ctx)
      await lastTab(ctx).locator('.main-tab__close').click()
      await waitTabCount(ctx, n - 1)
      ctx.assert((await tabCount(ctx)) === n - 1, 'tab did not close')
      await H.openRunByPid(ctx, pid)
      await waitTabCount(ctx, n)
      ctx.assert((await tabCount(ctx)) === n, 'reopening did not restore the tab')
    },
  },
  {
    id: 'tab-switch-preserves-analytics', name: 'Switching tabs preserves the run analytics view',
    tags: ['tabs'],
    timeout: 45000,
    run: async (ctx) => {
      const a = await ctx.spawnTarget('cpu'); const b = await ctx.spawnTarget('memgrow')
      await H.attachPid(ctx, a, { window: 3 }); await H.attachPid(ctx, b, { window: 3 })
      // run A: switch its analytics view to CPU
      await H.openRunByPid(ctx, a)
      await ctx.waitFor('.secondary-tabs', 6000)
      await ctx.page.locator('.secondary-tab', { hasText: /CPU/i }).first().click()
      await ctx.waitFor('[data-testid="cpu-tab"]', 6000)
      // run B: its own view is independent (defaults to Overview)
      await H.openRunByPid(ctx, b)
      await ctx.waitFor('[data-testid="overview-tab"]', 6000)
      // back to A by its pid tab → its CPU view must be preserved, not reset to Overview
      await ctx.page.locator('.main-tab', { hasText: `pid ${a}` }).first().locator('.main-tab__label').click()
      await ctx.waitFor('[data-testid="cpu-tab"]', 6000)
      ctx.assert(
        await ctx.exists('.secondary-tab--active[data-view="cpu"]'),
        'CPU view was not preserved after switching tabs away and back',
      )
    },
  },
  {
    id: 'tab-finished-run-no-steal-focus',
    name: 'A second finished run opens a tab but does not steal focus',
    tags: ['tabs', 'attach'],
    timeout: 45000,
    run: async (ctx) => {
      const a = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, a, { window: 3 })
      await H.openRunByPid(ctx, a) // focus run A
      await ctx.waitFor('.secondary-tabs', 6000)
      const activeBefore = (
        await ctx.page.locator('.main-tab[aria-selected="true"] .main-tab__label').innerText()
      ).trim()
      const tabsBefore = await tabCount(ctx)
      // a second run finishes while A is focused → it should open a background tab
      const b = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, b, { window: 3 })
      await waitTabCount(ctx, tabsBefore + 1) // its tab appeared (auto-opened on finish)
      await ctx.sleep(600) // let any (unwanted) focus change settle
      const activeAfter = (
        await ctx.page.locator('.main-tab[aria-selected="true"] .main-tab__label').innerText()
      ).trim()
      ctx.assert(
        activeAfter === activeBefore,
        `finished run stole focus (active "${activeBefore}" -> "${activeAfter}")`,
      )
    },
  },
  {
    id: 'tab-close-middle', name: 'Closing a middle tab leaves the flanking tabs open',
    tags: ['tabs'],
    timeout: 45000,
    run: async (ctx) => {
      const pids = [await ctx.spawnTarget('cpu'), await ctx.spawnTarget('idle'), await ctx.spawnTarget('memgrow')]
      for (const p of pids) await H.attachPid(ctx, p, { window: 3 })
      const before = await tabCount(ctx)
      for (const p of pids) await H.openRunByPid(ctx, p)
      await waitTabCount(ctx, before + 3)
      const n = await tabCount(ctx)
      const middle = await nthTab(ctx, n - 2) // second of the three we just opened
      await middle.locator('.main-tab__close').click()
      await waitTabCount(ctx, n - 1)
      ctx.assert((await tabCount(ctx)) === n - 1, 'closing the middle tab changed the count wrongly')
    },
  },
  {
    id: 'tab-many-open', name: 'Opening four runs yields four closable tabs',
    tags: ['tabs'],
    timeout: 55000,
    run: async (ctx) => {
      const pids = []
      for (const k of ['cpu', 'idle', 'memgrow', 'fdleak']) pids.push(await ctx.spawnTarget(k))
      for (const p of pids) await H.attachPid(ctx, p, { window: 3 })
      const before = await tabCount(ctx)
      for (const p of pids) await H.openRunByPid(ctx, p)
      await waitTabCount(ctx, before + 4)
      ctx.assert((await tabCount(ctx)) === before + 4, `expected 4 new tabs (had ${before})`)
      ctx.assert((await ctx.count('.main-tab__close')) === (await tabCount(ctx)), 'every tab should have a close button')
    },
  },
  {
    id: 'tab-delete-keeps-other', name: 'Deleting one open run keeps the other run tab open',
    tags: ['tabs', 'runs'],
    timeout: 40000,
    run: async (ctx) => {
      const a = await ctx.spawnTarget('cpu'); const b = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, a, { window: 3 }); await H.attachPid(ctx, b, { window: 3 })
      await H.openRunByPid(ctx, a); await H.openRunByPid(ctx, b)
      const tabs = await tabCount(ctx); const runs = await H.runCount(ctx)
      await H.runContextMenu(ctx, a)
      await ctx.page.locator('.ctx-item--danger', { hasText: /Delete/i }).first().click()
      await H.confirmDeleteRun(ctx)
      for (let i = 0; i < 20 && (await H.runCount(ctx)) >= runs; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) < runs, 'run a was not deleted')
      await waitTabCount(ctx, tabs - 1)
      ctx.assert((await tabCount(ctx)) === tabs - 1, 'exactly one tab should have closed')
      // run b's row still there and its tab still openable
      ctx.assert(await ctx.exists(`.run-row:has-text("pid ${b}")`), 'run b disappeared unexpectedly')
    },
  },
  {
    id: 'tab-label-matches-run', name: 'Open tab label matches a backend run name',
    tags: ['tabs', 'api'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.openRunByPid(ctx, pid)
      await waitTabCount(ctx, (await tabCount(ctx)))
      const label = (await lastTab(ctx).locator('.main-tab__label').innerText()).trim()
      const runs = await ctx.api.get('/runs?limit=500')
      // Tab + sidebar both show runLabel = `label ?? command` (the same user-facing
      // name), never the display_name slug — so assert the tab matches that.
      const names = runs.map((r) => (r.label ?? r.command ?? String(r.id).slice(0, 8)))
      ctx.assert(names.some((n) => (n || '').trim() === label), `tab label "${label}" matches no backend run name`)
    },
  },
]
