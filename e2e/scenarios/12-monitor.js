/** Live-monitor batch — monitor-mode attach: monitor-bar, Incidents, Stop→completed, ebpf combo. */
const H = require('./_helpers')

// The attach run's label carries "(pid <PID>)"; the sidebar row shows "pid <PID>".
async function runForPid(ctx, pid) {
  const runs = await ctx.api.get('/runs?limit=500')
  return runs.find((r) => (r.label || '').includes(`pid ${pid}`) ||
                          (r.display_name || '').includes(`pid ${pid}`) ||
                          (r.command || '').includes(String(pid)))
}

async function waitRunForPid(ctx, pid, ms = 8000) {
  for (let i = 0; i < ms / 300; i++) {
    const r = await runForPid(ctx, pid)
    if (r) return r
    await ctx.sleep(300)
  }
  return runForPid(ctx, pid)
}

async function waitStatus(ctx, rid, re, ms = 30000) {
  for (let i = 0; i < ms / 500; i++) {
    const r = await ctx.api.get(`/runs/${rid}`).catch(() => null)
    if (r && re.test(r.status || '')) return r.status
    await ctx.sleep(500)
  }
  const r = await ctx.api.get(`/runs/${rid}`).catch(() => null)
  return r && r.status
}

module.exports = [
  {
    id: 'mon-monitor-bar-shows', name: 'Monitor attach shows the live monitor bar',
    tags: ['monitor', 'attach'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.monitor-bar', 10000)
      ctx.assert(await ctx.exists('.monitor-bar'), 'no monitor bar for a monitor run')
    },
  },
  {
    id: 'mon-incidents-tab-exists', name: 'Monitor run exposes an Incidents tab',
    tags: ['monitor', 'tabs'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const inc = ctx.page.locator('.secondary-tab', { hasText: /Incidents/i })
      ctx.assert((await inc.count()) > 0, 'no Incidents tab for a monitor run')
    },
  },
  {
    id: 'mon-stop-completes', name: 'Stop ends the monitor bar and completes the run',
    tags: ['monitor'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor run not found in backend')
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.monitor-bar', 10000)
      await ctx.page.locator('.monitor-bar__stop, .monitor-bar button', { hasText: /Stop/i }).first().click()
      await ctx.gone('.monitor-bar', 20000)
      const status = await waitStatus(ctx, run.id, /completed/i, 30000)
      ctx.assert(status === 'completed', `expected completed, got ${status}`)
    },
  },
  {
    id: 'mon-idle-no-incidents', name: 'Idle target monitor reports a healthy empty feed',
    tags: ['monitor', 'edge'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.monitor-bar', 10000)
      await ctx.page.locator('.secondary-tab', { hasText: /Incidents/i }).first().click()
      await ctx.waitFor('[data-testid="incident-feed"], .incidents', 8000).catch(() => {})
      await ctx.waitText(/No incidents/i, 8000)
    },
  },
  {
    id: 'mon-incidents-api-array', name: 'Incidents endpoint returns an array',
    tags: ['monitor', 'api'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor run not found in backend')
      const incidents = await ctx.api.get(`/runs/${run.id}/incidents`)
      ctx.assert(Array.isArray(incidents), 'incidents payload is not an array')
    },
  },
  {
    id: 'mon-collector-config-monitor', name: 'Monitor run carries collector_config.monitor=true',
    tags: ['monitor', 'api'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor run not found in backend')
      ctx.assert((run.collector_config || {}).monitor === true, 'collector_config.monitor is not true')
    },
  },
  {
    id: 'mon-monitor-not-ebpf', name: 'Plain monitor run does not set the ebpf flag',
    tags: ['monitor', 'api', 'edge'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor run not found in backend')
      const cc = run.collector_config || {}
      ctx.assert(cc.monitor === true && !cc.ebpf, `unexpected flags: ${JSON.stringify(cc)}`)
    },
  },
  {
    id: 'mon-ebpf-combo', name: 'Monitor + eBPF sets both flags and shows Incidents + Latency',
    tags: ['monitor', 'ebpf'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, ebpf: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor+ebpf run not found in backend')
      const cc = run.collector_config || {}
      ctx.assert(cc.monitor && cc.ebpf, `expected monitor+ebpf, got ${JSON.stringify(cc)}`)
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      ctx.assert((await ctx.page.locator('.secondary-tab', { hasText: /Incidents/i }).count()) > 0, 'no Incidents tab')
      ctx.assert((await ctx.page.locator('.secondary-tab', { hasText: /Latency/i }).count()) > 0, 'no Latency tab')
    },
  },
  {
    id: 'mon-live-status-running', name: 'A freshly attached monitor run is live (running)',
    tags: ['monitor', 'api'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor run not found in backend')
      const status = await ctx.api.get(`/runs/${run.id}`).then((r) => r.status)
      ctx.assert(/running|analyzing/i.test(status), `expected a live status, got ${status}`)
      await ctx.api.post(`/runs/${run.id}/stop`, {}).catch(() => {})
    },
  },
  {
    id: 'mon-stop-via-api', name: 'Stopping via the API finalizes and clears the monitor bar',
    tags: ['monitor', 'api'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor run not found in backend')
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.monitor-bar', 10000)
      await ctx.api.post(`/runs/${run.id}/stop`, {})
      const status = await waitStatus(ctx, run.id, /completed/i, 30000)
      ctx.assert(status === 'completed', `expected completed, got ${status}`)
      await ctx.gone('.monitor-bar', 15000)
    },
  },
  {
    id: 'mon-incidents-feed-renders', name: 'Incidents tab renders its feed container',
    tags: ['monitor', 'tabs'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Incidents/i }).first().click()
      await ctx.waitFor('[data-testid="incident-feed"], .incidents', 8000)
      ctx.assert(await ctx.exists('.incidents, [data-testid="incident-feed"]'), 'incidents feed did not render')
    },
  },
  {
    id: 'mon-reopen-after-stop-no-bar', name: 'A stopped monitor run reopens without a monitor bar',
    tags: ['monitor', 'edge'], timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'monitor run not found in backend')
      await ctx.api.post(`/runs/${run.id}/stop`, {})
      const status = await waitStatus(ctx, run.id, /completed/i, 30000)
      ctx.assert(status === 'completed', `expected completed, got ${status}`)
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      await ctx.sleep(500)
      ctx.assert(!(await ctx.exists('.monitor-bar')), 'completed run still shows a live monitor bar')
    },
  },
  {
    id: 'mon-two-monitors-accumulate', name: 'Two monitor attaches create two monitor runs',
    tags: ['monitor', 'edge'], timeout: 60000,
    run: async (ctx) => {
      const p1 = await ctx.spawnTarget('idle')
      const p2 = await ctx.spawnTarget('idle')
      const n0 = await H.runCount(ctx)
      await H.attachPid(ctx, p1, { monitor: true, window: 3 })
      await H.attachPid(ctx, p2, { monitor: true, window: 3 })
      for (let i = 0; i < 20 && (await H.runCount(ctx)) < n0 + 2; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) >= n0 + 2, 'expected two runs from two monitor attaches')
      const r1 = await waitRunForPid(ctx, p1)
      const r2 = await waitRunForPid(ctx, p2)
      ctx.assert(r1 && (r1.collector_config || {}).monitor, 'first run not a monitor run')
      ctx.assert(r2 && (r2.collector_config || {}).monitor, 'second run not a monitor run')
      await ctx.api.post(`/runs/${r1.id}/stop`, {}).catch(() => {})
      await ctx.api.post(`/runs/${r2.id}/stop`, {}).catch(() => {})
    },
  },
]
