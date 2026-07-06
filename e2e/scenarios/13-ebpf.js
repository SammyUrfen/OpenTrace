/** eBPF batch — latency histograms, off-CPU flamegraphs, capabilities + monitor combos. */
const H = require('./_helpers')

// Newest run for a pid (attach labels runs '... (pid <PID>)'; list is started_at DESC).
async function runForPid(ctx, pid) {
  const runs = await ctx.api.get('/runs?limit=500')
  return runs.find((r) => (r.label || '').includes(`pid ${pid}`)) || null
}

// Poll runForPid until it appears (attach publishes run_started immediately).
async function waitRunForPid(ctx, pid, tries = 25) {
  for (let i = 0; i < tries; i++) {
    const r = await runForPid(ctx, pid)
    if (r) return r
    await ctx.sleep(300)
  }
  return null
}

module.exports = [
  {
    id: 'ebpf-capabilities-shape', name: 'eBPF capabilities endpoint has the gate fields',
    tags: ['ebpf', 'api'],
    run: async (ctx) => {
      const caps = await ctx.api.get('/runs/attach/ebpf-capabilities')
      ctx.assert(caps && typeof caps === 'object', 'no capabilities object')
      ctx.assert('available' in caps, 'capabilities missing `available`')
      ctx.assert('bpftrace' in caps, 'capabilities missing `bpftrace`')
      ctx.assert(caps.tools && typeof caps.tools === 'object', 'capabilities missing `tools` map')
    },
  },
  {
    id: 'ebpf-checkbox-enables', name: 'Attach modal eBPF checkbox enables once caps load',
    tags: ['ebpf', 'attach'],
    timeout: 60000,
    run: async (ctx) => {
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      const box = ctx.page.locator('.attach__monitor input[type=checkbox]').nth(1)
      let enabled = false
      for (let i = 0; i < 20; i++) { if (await box.isEnabled().catch(() => false)) { enabled = true; break } await ctx.sleep(300) }
      ctx.assert(enabled, 'eBPF checkbox never became enabled (capabilities?)')
      await ctx.press('Escape')
      await ctx.gone('.attach__search', 4000).catch(() => {})
    },
  },
  {
    id: 'ebpf-attach-sets-flag', name: 'eBPF attach stamps collector_config.ebpf',
    tags: ['ebpf', 'attach'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'attach produced no run for pid')
      ctx.assert((run.collector_config || {}).ebpf === true, 'newest run is not flagged ebpf')
      await ctx.waitFor('.run-row', 6000)
    },
  },
  {
    id: 'ebpf-latency-tab-present', name: 'eBPF run exposes a Latency tab',
    tags: ['ebpf', 'tabs'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const lat = ctx.page.locator('.secondary-tab', { hasText: /Latency/i })
      ctx.assert((await lat.count()) > 0, 'eBPF run has no Latency tab')
      await lat.first().click()
      await ctx.waitFor('[data-testid="latency-tab"]', 8000)
    },
  },
  {
    id: 'ebpf-latency-api', name: 'Latency endpoint returns histogram fields',
    tags: ['ebpf', 'api'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'no run for pid')
      const lat = await ctx.api.get(`/runs/${run.id}/latency`)
      ctx.assert(lat && typeof lat === 'object', 'latency response empty')
      ctx.assert('runqueue' in lat, 'latency missing `runqueue`')
      ctx.assert('block_io' in lat, 'latency missing `block_io`')
      ctx.assert('engine' in lat || 'available' in lat, 'latency missing engine/available')
    },
  },
  {
    id: 'ebpf-offcpu-toggle', name: 'eBPF flamegraph offers an On-CPU/Off-CPU toggle',
    tags: ['ebpf', 'flamegraph'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Flamegraph/i }).first().click()
      await ctx.waitFor('[data-testid="flamegraph-tab"]', 8000)
      await ctx.waitFor('.flame-toggle', 8000)
      const off = ctx.page.locator('.flame-toggle__btn', { hasText: /Off-CPU/i }).first()
      ctx.assert((await off.count()) > 0, 'no Off-CPU toggle button')
      await off.click()
      const active = ctx.page.locator('.flame-toggle__btn--on', { hasText: /Off-CPU/i })
      await active.first().waitFor({ state: 'visible', timeout: 6000 })
    },
  },
  {
    id: 'ebpf-offcpu-toggle-roundtrip', name: 'Flamegraph toggles Off-CPU then back to On-CPU',
    tags: ['ebpf', 'flamegraph'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Flamegraph/i }).first().click()
      await ctx.waitFor('.flame-toggle', 8000)
      await ctx.page.locator('.flame-toggle__btn', { hasText: /Off-CPU/i }).first().click()
      await ctx.page.locator('.flame-toggle__btn--on', { hasText: /Off-CPU/i }).first().waitFor({ state: 'visible', timeout: 6000 })
      await ctx.page.locator('.flame-toggle__btn', { hasText: /On-CPU/i }).first().click()
      await ctx.page.locator('.flame-toggle__btn--on', { hasText: /On-CPU/i }).first().waitFor({ state: 'visible', timeout: 6000 })
    },
  },
  {
    id: 'ebpf-offcpu-flamegraph-api', name: 'Off-CPU flamegraph endpoint responds with a tree shape',
    tags: ['ebpf', 'api'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'no run for pid')
      const fg = await ctx.api.get(`/runs/${run.id}/offcpu-flamegraph`)
      ctx.assert(fg && typeof fg === 'object', 'off-cpu flamegraph response empty')
      ctx.assert('tree' in fg || 'supported' in fg, 'off-cpu flamegraph missing tree/supported')
      ctx.assert('samples' in fg || 'reason' in fg, 'off-cpu flamegraph missing samples/reason')
    },
  },
  {
    id: 'ebpf-non-ebpf-no-latency', name: 'A non-eBPF attach run has NO Latency tab',
    tags: ['ebpf', 'edge'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run && !(run.collector_config || {}).ebpf, 'run unexpectedly flagged ebpf')
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const lat = ctx.page.locator('.secondary-tab', { hasText: /Latency/i })
      ctx.assert((await lat.count()) === 0, 'non-eBPF run should not have a Latency tab')
    },
  },
  {
    id: 'ebpf-flamegraph-and-latency', name: 'eBPF run shows both Flamegraph and Latency tabs',
    tags: ['ebpf', 'tabs'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const flame = ctx.page.locator('.secondary-tab', { hasText: /Flamegraph/i })
      const lat = ctx.page.locator('.secondary-tab', { hasText: /Latency/i })
      ctx.assert((await flame.count()) > 0, 'eBPF run missing Flamegraph tab')
      ctx.assert((await lat.count()) > 0, 'eBPF run missing Latency tab')
    },
  },
  {
    id: 'ebpf-monitor-combo', name: 'eBPF + monitor attach is live and carries both flags',
    tags: ['ebpf', 'monitor'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { ebpf: true, monitor: true, window: 3 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'no run for pid')
      const c = run.collector_config || {}
      ctx.assert(c.ebpf === true, 'combo run not flagged ebpf')
      ctx.assert(c.monitor === true, 'combo run not flagged monitor')
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.monitor-bar', 10000)
      const lat = ctx.page.locator('.secondary-tab', { hasText: /Latency/i })
      ctx.assert((await lat.count()) > 0, 'ebpf+monitor run missing Latency tab')
      await ctx.page.locator('.monitor-bar__stop, .monitor-bar button', { hasText: /Stop/i }).first().click()
      await ctx.gone('.monitor-bar', 15000)
    },
  },
  {
    id: 'ebpf-idle-target-latency', name: 'eBPF attach on an idle target still exposes Latency',
    tags: ['ebpf', 'edge'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { ebpf: true, window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run && (run.collector_config || {}).ebpf === true, 'idle ebpf run missing flag')
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Latency/i }).first().click()
      await ctx.waitFor('[data-testid="latency-tab"]', 8000)
    },
  },
  {
    id: 'ebpf-two-runs-both-flagged', name: 'Two eBPF attaches yield two ebpf-flagged runs',
    tags: ['ebpf', 'edge'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      const n0 = await H.runCount(ctx)
      await H.attachPid(ctx, pid, { ebpf: true, window: 3 })
      await H.attachPid(ctx, pid, { ebpf: true, window: 3 })
      for (let i = 0; i < 25 && (await H.runCount(ctx)) < n0 + 2; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) >= n0 + 2, 'expected two runs from two eBPF attaches')
      const runs = await ctx.api.get('/runs?limit=500')
      const mine = runs.filter((r) => (r.label || '').includes(`pid ${pid}`))
      ctx.assert(mine.length >= 2, `expected >=2 runs for pid ${pid}, got ${mine.length}`)
      ctx.assert(mine.slice(0, 2).every((r) => (r.collector_config || {}).ebpf === true), 'both runs not flagged ebpf')
    },
  },
]
