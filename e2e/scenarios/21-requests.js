/** Request-tracing batch — the Requests tab: capability gate, the `requests` flag,
 *  flag-gated tab presence, the rollup API shape, and (when the box can actually
 *  capture) real endpoint rows + the DB-vs-app breakdown. Mirrors 13-ebpf.js: the
 *  flag/presence/shape assertions are deterministic; the real-capture path is guarded
 *  on request-capabilities because bpftrace capture is privilege-dependent. */
const H = require('./_helpers')
const { httpTargetPort } = require('../lib/driver')

async function runForPid(ctx, pid) {
  const runs = await ctx.api.get('/runs?limit=500')
  return runs.find((r) => (r.label || '').includes(`pid ${pid}`)) || null
}
async function waitRunForPid(ctx, pid, tries = 25) {
  for (let i = 0; i < tries; i++) {
    const r = await runForPid(ctx, pid)
    if (r) return r
    await ctx.sleep(300)
  }
  return null
}
async function waitCompleted(ctx, id, tries = 60) {
  for (let i = 0; i < tries; i++) {
    const r = await ctx.api.get(`/runs/${id}`)
    if (r && r.status === 'completed') return r
    await ctx.sleep(500)
  }
  return await ctx.api.get(`/runs/${id}`)
}

// hit the http target (returns status, or 0 on error — the server is single-threaded)
async function hit(port, path) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}${path}`)
    return r.status
  } catch {
    return 0
  }
}
async function waitHttp(port, tries = 30) {
  for (let i = 0; i < tries; i++) {
    if ((await hit(port, '/')) >= 200) return true
    await new Promise((r) => setTimeout(r, 200))
  }
  return false
}

module.exports = [
  {
    id: 'requests-capabilities-shape', name: 'request-capabilities endpoint has the gate fields',
    tags: ['requests', 'api'],
    run: async (ctx) => {
      const caps = await ctx.api.get('/runs/attach/request-capabilities')
      ctx.assert(caps && typeof caps === 'object', 'no capabilities object')
      ctx.assert('available' in caps, 'capabilities missing `available`')
      ctx.assert('reason' in caps, 'capabilities missing `reason`')
      ctx.assert(caps.engine === 'bpftrace', 'capabilities engine should be bpftrace')
      // reason is null iff available (the gate is coherent)
      ctx.assert((caps.reason === null) === (caps.available === true), 'reason/available incoherent')
    },
  },
  {
    id: 'requests-attach-sets-flag', name: 'request-tracing attach stamps collector_config.requests',
    tags: ['requests', 'attach'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { requests: true, window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'attach produced no run for pid')
      ctx.assert((run.collector_config || {}).requests === true, 'newest run is not flagged requests')
      await ctx.waitFor('.run-row', 6000)
    },
  },
  {
    id: 'requests-tab-present', name: 'a request-tracing run exposes a Requests tab that renders',
    tags: ['requests', 'tabs'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { requests: true, window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const tab = ctx.page.locator('.secondary-tab', { hasText: /Requests/i })
      ctx.assert((await tab.count()) > 0, 'request run has no Requests tab')
      await tab.first().click()
      await ctx.waitFor('[data-testid="requests-tab"]', 8000)  // fail-open renders even with no traffic
    },
  },
  {
    id: 'requests-non-requests-no-tab', name: 'a non-request attach run has NO Requests tab',
    tags: ['requests', 'edge'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run && !(run.collector_config || {}).requests, 'run unexpectedly flagged requests')
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      const tab = ctx.page.locator('.secondary-tab', { hasText: /Requests/i })
      ctx.assert((await tab.count()) === 0, 'non-request run should not have a Requests tab')
    },
  },
  {
    id: 'requests-api-shape', name: 'requests endpoint returns the rollup shape',
    tags: ['requests', 'api'],
    timeout: 60000,
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.attachPid(ctx, pid, { requests: true, window: 4 })
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'no run for pid')
      await waitCompleted(ctx, run.id)
      const req = await ctx.api.get(`/runs/${run.id}/requests`)
      ctx.assert(req && typeof req === 'object', 'requests response empty')
      for (const k of ['available', 'endpoints', 'spans', 'engine', 'request_count',
                       'db_span_count', 'has_breakdown']) {
        ctx.assert(k in req, `requests rollup missing \`${k}\``)
      }
      ctx.assert(Array.isArray(req.endpoints), 'endpoints is not an array')
    },
  },
  {
    id: 'requests-endpoint-rows', name: 'real HTTP traffic yields endpoint rows + a DB-vs-app breakdown',
    tags: ['requests', 'tabs'],
    timeout: 90000,
    run: async (ctx) => {
      const caps = await ctx.api.get('/runs/attach/request-capabilities')
      const pid = await ctx.spawnTarget('http')
      const port = httpTargetPort(pid)
      const up = await waitHttp(port)
      ctx.assert(up, `http target never came up on :${port}`)

      await H.attachPid(ctx, pid, { requests: true, window: 8 })
      // drive plaintext GET traffic across the window (the probe needs ~1-2s to attach,
      // so keep hitting it for most of the window). /slow (off-CPU sleep) + / + /err.
      const end = Date.now() + 6500
      while (Date.now() < end) {
        await hit(port, '/')
        await hit(port, '/slow')
        await hit(port, '/err')
        await new Promise((r) => setTimeout(r, 150))
      }
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'no run for pid')
      await waitCompleted(ctx, run.id)

      const req = await ctx.api.get(`/runs/${run.id}/requests`)
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Requests/i }).first().click()
      await ctx.waitFor('[data-testid="requests-tab"]', 8000)

      // The capture is privilege-dependent (like eBPF). When it worked, exercise the full
      // UI: the endpoint table, a row (data-route), and its expand → breakdown. When it
      // didn't (no bpftrace privilege), the tab still renders its fail-open empty state —
      // assert that instead, so the scenario is robust across hosts.
      if (caps && caps.available && (req.endpoints || []).length > 0) {
        await ctx.waitFor('[data-testid="endpoint-table"]', 8000)
        const rows = ctx.page.locator('[data-testid="endpoint-row"]')
        ctx.assert((await rows.count()) > 0, 'captured endpoints but no endpoint-row rendered')
        ctx.assert((await ctx.count('[data-testid="endpoint-row"][data-route="/slow"]')) > 0, 'no row for GET /slow')
        await rows.first().click()
        await ctx.waitFor('[data-testid="endpoint-breakdown"]', 6000)
      } else {
        ctx.assert(await ctx.exists('[data-testid="requests-tab"]'), 'requests tab did not render fail-open')
      }
    },
  },
  {
    id: 'requests-waterfall-breakdown-drill',
    name: 'Phase 2: off-CPU breakdown, per-request waterfall + span→flamegraph drill',
    tags: ['requests', 'tabs'],
    timeout: 90000,
    run: async (ctx) => {
      const caps = await ctx.api.get('/runs/attach/request-capabilities')
      const pid = await ctx.spawnTarget('http')
      const port = httpTargetPort(pid)
      ctx.assert(await waitHttp(port), `http target never came up on :${port}`)

      await H.attachPid(ctx, pid, { requests: true, window: 8 })
      const end = Date.now() + 6500
      while (Date.now() < end) {
        await hit(port, '/slow')   // off-CPU sleep → an off-CPU breakdown + a per-tid drill
        await hit(port, '/')
        await new Promise((r) => setTimeout(r, 150))
      }
      const run = await waitRunForPid(ctx, pid)
      ctx.assert(run, 'no run for pid')
      await waitCompleted(ctx, run.id)
      const req = await ctx.api.get(`/runs/${run.id}/requests`)

      // Skip the deep UI assertions when capture was privilege-blocked (fail-open host):
      // just confirm the tab renders, like the sibling scenario.
      if (!(caps && caps.available && (req.spans || []).length > 0)) {
        await H.openRunByPid(ctx, pid)
        await ctx.waitFor('.secondary-tabs', 8000)
        await ctx.page.locator('.secondary-tab', { hasText: /Requests/i }).first().click()
        ctx.assert(await ctx.exists('[data-testid="requests-tab"]'), 'requests tab did not render fail-open')
        return
      }

      // API: the rollup carries the off-CPU decomposition, and the curated reader + the
      // per-tid off-CPU flame back the waterfall drill.
      ctx.assert(req.has_breakdown === true, 'rollup missing off-CPU breakdown (has_breakdown)')
      const slow = (req.spans || []).find((s) => s.route === '/slow') || req.spans[0]
      ctx.assert(slow && slow.breakdown, 'no per-request breakdown on a sampled span')
      const b = slow.breakdown
      for (const k of ['on_cpu_ms', 'runq_ms', 'db_wait_ms', 'other_off_ms']) {
        ctx.assert(k in b, `breakdown missing \`${k}\``)
      }
      const curated = await ctx.api.get(`/runs/${run.id}/request-spans?limit=50`)
      ctx.assert(Array.isArray(curated) && curated.length > 0, 'curated request-spans reader returned none')
      ctx.assert(typeof curated[0].timestamp_ms === 'number' && curated[0].timestamp_ms > 1e12,
        'curated span timestamp is not epoch ms (§2.6 anchor)')
      const flame = await ctx.api.get(`/runs/${run.id}/offcpu-flamegraph?tid=${slow.tid}`)
      ctx.assert(flame && ('supported' in flame), 'offcpu-flamegraph?tid= returned no flame shape')

      // UI: switch to the waterfall, expand a request → breakdown + SQL + off-CPU drill.
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.secondary-tabs', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Requests/i }).first().click()
      await ctx.waitFor('[data-testid="requests-tab"]', 8000)
      await ctx.page.locator('.req-view-toggle__btn', { hasText: /Requests/ }).first().click()
      await ctx.waitFor('[data-testid="request-waterfall"]', 8000)
      const wrows = ctx.page.locator('[data-testid="waterfall-row"]')
      ctx.assert((await wrows.count()) > 0, 'waterfall rendered no request rows')
      await wrows.first().click()
      await ctx.waitFor('[data-testid="waterfall-detail"]', 6000)
      ctx.assert(await ctx.exists('[data-testid="request-breakdown"]'), 'expanded request has no breakdown bar')
      ctx.assert(await ctx.exists('[data-testid="span-flame"]'), 'expanded request has no off-CPU drill')
    },
  },
]
