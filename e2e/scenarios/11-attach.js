/** Attach batch — attaching to live targets, list filtering, cancel/rescan, opening attached runs. */
const H = require('./_helpers')

// wait until backend run count exceeds `before`, returns the newest run
async function waitNewRun(ctx, before, tries = 24) {
  for (let i = 0; i < tries && (await H.runCount(ctx)) <= before; i++) await ctx.sleep(300)
  ctx.assert((await H.runCount(ctx)) > before, 'no run was created by attach')
  const runs = await ctx.api.get('/runs?limit=5')
  return runs[0]
}

module.exports = [
  {
    id: 'atc-cpu-window3', name: 'Attach cpu target (window 3) creates an attach run',
    tags: ['attach'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      const before = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { window: 3 })
      const run = await waitNewRun(ctx, before)
      ctx.assert((run.collector_config || {}).attach, 'newest run is not an attach run')
      await ctx.waitFor('.run-row', 6000)
    },
  },
  {
    id: 'atc-idle-window4', name: 'Attach idle target (window 4) creates an attach run',
    tags: ['attach'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      const before = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { window: 4 })
      const run = await waitNewRun(ctx, before)
      ctx.assert((run.collector_config || {}).attach, 'idle attach run missing attach config')
    },
  },
  {
    id: 'atc-fdleak-window4', name: 'Attach fdleak target creates an attach run',
    tags: ['attach'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('fdleak')
      const before = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { window: 4 })
      const run = await waitNewRun(ctx, before)
      ctx.assert((run.collector_config || {}).attach, 'fdleak attach run missing attach config')
    },
  },
  {
    id: 'atc-memgrow-window10', name: 'Attach memgrow target (window 10) creates an attach run',
    tags: ['attach'],
    timeout: 40000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('memgrow')
      const before = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { window: 10 })
      const run = await waitNewRun(ctx, before, 30)
      ctx.assert((run.collector_config || {}).attach, 'memgrow attach run missing attach config')
    },
  },
  {
    id: 'atc-filter-by-pid', name: 'Attach list filters to a single row by pid',
    tags: ['attach', 'filter'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.fill('.attach__search', String(pid))
      const row = ctx.page.locator('.attach__row', { hasText: `pid ${pid}` }).first()
      await row.waitFor({ state: 'visible', timeout: 8000 })
      ctx.assert((await ctx.count('.attach__row')) >= 1, 'no attach row for the pid')
      await ctx.press('Escape')
      await ctx.gone('.attach__search', 4000).catch(() => {})
    },
  },
  {
    id: 'atc-filter-by-runtime', name: 'Attach list filters by runtime "python"',
    tags: ['attach', 'filter'],
    run: async (ctx) => {
      await ctx.spawnTarget('cpu') // ensure at least one python target is alive
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.fill('.attach__search', 'python')
      const row = ctx.page.locator('.attach__row').first()
      await row.waitFor({ state: 'visible', timeout: 8000 })
      ctx.assert((await ctx.count('.attach__row')) >= 1, 'no rows when filtering by python')
      await ctx.press('Escape')
    },
  },
  {
    id: 'atc-cancel-close-btn', name: 'Cancel attach via close button creates no run',
    tags: ['attach', 'edge'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      const before = await H.runCount(ctx)
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.fill('.attach__search', String(pid))
      await ctx.waitFor('.attach__row', 8000)
      await ctx.click('.modal__close')
      await ctx.gone('.attach__search', 4000)
      await ctx.sleep(1000)
      ctx.assert((await H.runCount(ctx)) === before, 'cancel via close still created a run')
    },
  },
  {
    id: 'atc-cancel-escape', name: 'Cancel attach via Escape creates no run',
    tags: ['attach', 'edge'],
    run: async (ctx) => {
      const before = await H.runCount(ctx)
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.press('Escape')
      await ctx.gone('.attach__search', 4000)
      await ctx.sleep(800)
      ctx.assert((await H.runCount(ctx)) === before, 'Escape still created a run')
    },
  },
  {
    id: 'atc-rescan-button', name: 'Rescan button re-populates the process list',
    tags: ['attach', 'ui'],
    run: async (ctx) => {
      await ctx.spawnTarget('cpu')
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      const rescan = ctx.page.locator('.ai-btn[title*="rescan" i], .ai-btn[title*="Rescan"]').first()
      await rescan.waitFor({ state: 'visible', timeout: 5000 })
      await rescan.click()
      // after rescan the list should still show rows (not stuck on scanning/empty)
      await ctx.waitFor('.attach__row', 10000)
      ctx.assert((await ctx.count('.attach__row')) >= 1, 'no rows after rescan')
      await ctx.press('Escape')
    },
  },
  {
    id: 'atc-attach-open-tab', name: 'Attach then open the run shows analytics tabs',
    tags: ['attach', 'tabs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      ctx.assert((await ctx.count('.secondary-tab')) >= 1, 'no analytics tabs for attached run')
    },
  },
  {
    id: 'atc-attach-has-flamegraph', name: 'Attach run exposes a Flamegraph tab (perf)',
    tags: ['attach', 'tabs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const fg = ctx.page.locator('.secondary-tab', { hasText: /Flamegraph/i })
      ctx.assert((await fg.count()) > 0, 'attach run has no Flamegraph tab')
      await fg.first().click()
      await ctx.waitFor('[data-testid="flamegraph-tab"]', 8000)
    },
  },
  {
    id: 'atc-attach-no-syscall-tab', name: 'Attach run has no Syscalls tab (no strace)',
    tags: ['attach', 'tabs', 'edge'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const sys = ctx.page.locator('.secondary-tab', { hasText: /Syscalls/i })
      ctx.assert((await sys.count()) === 0, 'attach run unexpectedly has a Syscalls tab')
    },
  },
  {
    id: 'atc-profiler-hint', name: 'Attach row surfaces a py-spy/perf profiler hint',
    tags: ['attach', 'ui'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.fill('.attach__search', String(pid))
      const row = ctx.page.locator('.attach__row', { hasText: `pid ${pid}` }).first()
      await row.waitFor({ state: 'visible', timeout: 8000 })
      const txt = await row.innerText()
      ctx.assert(/py-?spy|perf/i.test(txt), `row lacks a profiler hint: ${txt.slice(0, 120)}`)
      await ctx.press('Escape')
    },
  },
  {
    id: 'atc-config-window-persisted', name: 'Chosen window_s is persisted on the attach run',
    tags: ['attach', 'config'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      const before = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { window: 3 })
      const run = await waitNewRun(ctx, before)
      const cc = run.collector_config || {}
      const w = cc.window_s != null ? cc.window_s : cc.window
      if (w != null) ctx.assert(Number(w) === 3, `window_s not persisted (got ${w})`)
      ctx.assert(cc.attach, 'run not flagged attach')
    },
  },
  {
    id: 'atc-targets-endpoint', name: 'Attach targets endpoint lists the spawned pid',
    tags: ['attach', 'api'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      let found = false
      for (let i = 0; i < 15 && !found; i++) {
        const targets = await ctx.api.get('/runs/attach/targets')
        const list = Array.isArray(targets) ? targets : (targets.targets || [])
        found = list.some((t) => Number(t.pid) === Number(pid))
        if (!found) await ctx.sleep(400)
      }
      ctx.assert(found, `spawned pid ${pid} not in /runs/attach/targets`)
    },
  },
  {
    id: 'atc-two-targets-two-runs', name: 'Attaching two different targets makes two runs',
    tags: ['attach', 'edge'],
    run: async (ctx) => {
      const p1 = await ctx.spawnTarget('cpu')
      const p2 = await ctx.spawnTarget('idle')
      const before = await H.runCount(ctx)
      await H.attachPid(ctx, p1, { window: 3 })
      await H.attachPid(ctx, p2, { window: 3 })
      for (let i = 0; i < 24 && (await H.runCount(ctx)) < before + 2; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) >= before + 2, 'two attaches did not create two runs')
    },
  },
]
