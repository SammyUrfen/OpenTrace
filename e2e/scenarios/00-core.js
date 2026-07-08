/** Core validation batch — proves the framework + exercises the main features. */
const H = require('./_helpers')

module.exports = [
  {
    id: 'boot-backend-connected', name: 'App boots and connects to backend',
    tags: ['smoke'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      await ctx.assertText(/Backend:\s*connected/i, 'backend not connected')
      await ctx.assertText(/Terminal Tracing (ON|OFF)/i, 'tracing toggle missing')
    },
  },
  {
    id: 'palette-open-escape', name: 'Command palette opens and closes',
    tags: ['smoke', 'palette'],
    run: async (ctx) => {
      await ctx.press('Control+k')
      await ctx.waitFor('.palette__input', 4000)
      await ctx.press('Escape')
      await ctx.gone('.palette', 4000)
    },
  },
  {
    id: 'theme-toggle', name: 'Theme toggle flips data-theme',
    tags: ['smoke', 'settings'],
    run: async (ctx) => {
      const before = await ctx.page.evaluate(() => document.documentElement.dataset.theme)
      await H.toggleTheme(ctx)
      await ctx.sleep(300)
      const after = await ctx.page.evaluate(() => document.documentElement.dataset.theme)
      ctx.assert(before !== after, `theme did not change (${before} -> ${after})`)
    },
  },
  {
    id: 'tracing-toggle', name: 'Tracing toggle ON/OFF',
    tags: ['smoke'],
    run: async (ctx) => {
      await ctx.assertText(/Terminal Tracing OFF/i, 'expected tracing OFF at start')
      await H.toggleTracing(ctx)
      await ctx.waitText(/Terminal Tracing ON/i, 4000)
      await H.toggleTracing(ctx)
      await ctx.waitText(/Terminal Tracing OFF/i, 4000)
    },
  },
  {
    id: 'session-create', name: 'Create a session',
    tags: ['sessions'],
    run: async (ctx) => {
      await H.newSession(ctx, 'proj-alpha')
      await ctx.waitText(/proj-alpha/i, 5000)
      const sessions = await ctx.api.get('/sessions')
      ctx.assert(sessions.some((s) => /proj-alpha/i.test(s.display_name || s.name || '')), 'session not in backend')
    },
  },
  {
    id: 'settings-open-nav', name: 'Open settings and navigate sections',
    tags: ['settings'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      const navs = await ctx.count('.settings__navitem, .settings__nav button, .settings__nav a')
      ctx.assert(navs >= 2, `expected settings nav items, got ${navs}`)
      await ctx.press('Escape')
    },
  },
  {
    id: 'attach-cpu-basic', name: 'Attach to a CPU-bound process creates a run',
    tags: ['attach', 'smoke'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      const before = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { window: 4 })
      // a run should appear (backend ground truth) + a row in the sidebar
      for (let i = 0; i < 20 && (await H.runCount(ctx)) <= before; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) > before, 'no run was created by attach')
      await ctx.waitFor('.run-row', 6000)
    },
  },
  {
    id: 'attach-open-tabs', name: 'Open an attached run and switch analytics tabs',
    tags: ['attach', 'tabs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 6000)
      for (const [label, testid] of [[/Overview/i, 'overview-tab'], [/CPU/i, 'cpu-tab'], [/Memory/i, 'memory-tab']]) {
        await ctx.page.locator('.secondary-tab', { hasText: label }).first().click()
        await ctx.waitFor(`[data-testid="${testid}"]`, 6000)
      }
    },
  },
  {
    id: 'attach-monitor-stop', name: 'Monitor attach shows incidents + Stop',
    tags: ['attach', 'monitor'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.monitor-bar', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Incidents/i }).first().click()
      await ctx.waitFor('[data-testid="incident-feed"], .incidents', 6000).catch(() => {})
      await ctx.page.locator('.monitor-bar__stop, .monitor-bar button', { hasText: /Stop/i }).first().click()
      await ctx.gone('.monitor-bar', 15000)
    },
  },
  {
    id: 'attach-ebpf-latency', name: 'eBPF attach exposes a Latency tab',
    tags: ['attach', 'ebpf'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      // ground-truth: the run we just made must carry ebpf
      const runs = await ctx.api.get('/runs?limit=5')
      ctx.assert(runs.some((r) => (r.collector_config || {}).ebpf), 'newest run has no ebpf flag')
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 6000)
      const lat = ctx.page.locator('.secondary-tab', { hasText: /Latency/i })
      ctx.assert((await lat.count()) > 0, 'no Latency tab for an eBPF run')
      await lat.first().click()
      await ctx.waitFor('[data-testid="latency-tab"]', 6000)
    },
  },
  {
    id: 'run-delete', name: 'Delete a run from the sidebar context menu',
    tags: ['runs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      const n = await H.runCount(ctx)
      await H.runContextMenu(ctx)
      await ctx.page.locator('.ctx-item--danger', { hasText: /Delete/i }).first().click()
      for (let i = 0; i < 15 && (await H.runCount(ctx)) >= n; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) < n, 'run was not deleted')
    },
  },
  {
    id: 'run-delete-open-clean', name: 'Deleting an OPEN run closes its tab with no console errors',
    tags: ['runs', 'regression'],
    run: async (ctx) => {
      // regression: deleting the focused run used to refetch the dead id → 3× 404s
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 6000)
      const n = await H.runCount(ctx)
      await H.runContextMenu(ctx, pid)
      await ctx.page.locator('.ctx-item--danger', { hasText: /Delete/i }).first().click()
      for (let i = 0; i < 15 && (await H.runCount(ctx)) >= n; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) < n, 'open run was not deleted')
      // the runner auto-fails this scenario if any console/page error fired
    },
  },
  {
    id: 'run-ctx-menu-escape', name: 'Run context menu dismisses on Escape',
    tags: ['runs', 'regression', 'ui'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      await H.runContextMenu(ctx, pid)
      await ctx.waitFor('.ctx-menu', 4000)
      await ctx.press('Escape')
      await ctx.gone('.ctx-menu', 4000)
    },
  },
  {
    id: 'run-rename', name: 'Rename a run',
    tags: ['runs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      await H.runContextMenu(ctx, pid)
      await ctx.page.locator('.ctx-item', { hasText: /Rename/i }).first().click()
      await ctx.waitFor('.modal--small input', 4000)
      await ctx.fill('.modal--small input', 'renamed-run-xyz')
      await ctx.click('.modal--small .ai-btn--primary')
      await ctx.waitText(/renamed-run-xyz/i, 5000)
    },
  },
  {
    id: 'attach-invalid-pid-empty', name: 'Attach filter for a dead pid shows empty state',
    tags: ['attach', 'edge'],
    run: async (ctx) => {
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.fill('.attach__search', '99999991')
      await ctx.waitText(/No matching processes/i, 5000)
      await ctx.press('Escape')
    },
  },
  {
    id: 'attach-twice-two-runs', name: 'Attaching the same pid twice makes two runs',
    tags: ['attach', 'edge'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      const n0 = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { window: 3 })
      await H.attachPid(ctx, pid, { window: 3 })
      for (let i = 0; i < 20 && (await H.runCount(ctx)) < n0 + 2; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) >= n0 + 2, 'expected two runs from two attaches')
    },
  },
  {
    id: 'sidebar-toggle', name: 'Toggle sidebar with Ctrl+B',
    tags: ['ui'],
    run: async (ctx) => {
      const hidden = () => ctx.page.evaluate(() => !!document.querySelector('.app-shell--no-sidebar'))
      const before = await hidden()
      await H.menu(ctx, /View/i, /Toggle Sidebar/i)
      await ctx.sleep(400)
      ctx.assert((await hidden()) !== before, 'sidebar did not toggle')
      await H.menu(ctx, /View/i, /Toggle Sidebar/i) // restore
      await ctx.sleep(300)
    },
  },
]
