/** Analytics tabs for attach runs — Overview/CPU/Memory/Timeline/Flamegraph/Files + absent strace tabs. */
const H = require('./_helpers')

// --- local helpers -----------------------------------------------------------
// Find the backend run id for an attach pid (its label carries `(pid <PID>)`).
async function findRunId(ctx, pid) {
  const runs = await ctx.api.get('/runs?limit=500')
  const r = runs.find(
    (x) => (x.label || '').includes(`pid ${pid}`) || (x.display_name || '').includes(`pid ${pid}`),
  )
  return r ? r.id : null
}

// Poll GET /runs/{id} until status 'completed' (attach analysis flips it). ~8s.
async function waitCompleted(ctx, pid, ms = 8000) {
  const t0 = Date.now()
  let id = null
  while (Date.now() - t0 < ms) {
    id = id || (await findRunId(ctx, pid))
    if (id) {
      const r = await ctx.api.get(`/runs/${id}`)
      if (r.status === 'completed') return r
    }
    await ctx.sleep(400)
  }
  return id ? await ctx.api.get(`/runs/${id}`) : null
}

// Spawn a cpu target, attach (short window), open its run, wait for the tab bar.
async function openAttach(ctx, kind = 'cpu') {
  const pid = await ctx.spawnTarget(kind)
  await H.attachPid(ctx, pid, { window: 4 })
  await H.openRunByPid(ctx, pid)
  await ctx.waitFor('.secondary-tabs', 8000)
  return pid
}

// Click a secondary tab and wait for its content testid.
async function tab(ctx, label, testid) {
  await ctx.page.locator('.secondary-tab', { hasText: label }).first().click()
  await ctx.waitFor(`[data-testid="${testid}"]`, 8000)
}

module.exports = [
  {
    id: 'ana-overview-tab', name: 'Overview tab renders with Top findings section',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      await tab(ctx, /Overview/i, 'overview-tab')
      await ctx.waitText(/Top findings/i, 8000)
      await ctx.waitText(/Execution snapshot/i, 8000)
      await waitCompleted(ctx, pid)
    },
  },
  {
    id: 'ana-cpu-tab', name: 'CPU tab opens for an attach run',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      await openAttach(ctx)
      await tab(ctx, /^CPU$/i, 'cpu-tab')
    },
  },
  {
    id: 'ana-memory-tab', name: 'Memory tab opens for an attach run',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      await openAttach(ctx)
      await tab(ctx, /Memory/i, 'memory-tab')
    },
  },
  {
    id: 'ana-timeline-tab', name: 'Timeline tab opens for an attach run',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      await openAttach(ctx)
      await tab(ctx, /Timeline/i, 'timeline-tab')
    },
  },
  {
    id: 'ana-flamegraph-tab', name: 'Flamegraph tab present (perf) and opens',
    tags: ['analytics', 'attach', 'flamegraph'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      const id = await findRunId(ctx, pid)
      ctx.assert(id, 'could not resolve attach run id')
      const run = await ctx.api.get(`/runs/${id}`)
      ctx.assert((run.collector_config || {}).perf, 'attach run missing perf in collector_config')
      const fg = ctx.page.locator('.secondary-tab', { hasText: /Flamegraph/i })
      ctx.assert((await fg.count()) > 0, 'no Flamegraph tab for a perf attach run')
      await tab(ctx, /Flamegraph/i, 'flamegraph-tab')
    },
  },
  {
    id: 'ana-files-tab', name: 'Files tab lists captured run files',
    tags: ['analytics', 'attach', 'files'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      await waitCompleted(ctx, pid)
      const id = await findRunId(ctx, pid)
      ctx.assert(id, 'could not resolve attach run id')
      const files = await ctx.api.get(`/runs/${id}/files`)
      ctx.assert(Array.isArray(files) && files.length > 0, 'files API returned no files')
      await tab(ctx, /Files/i, 'files-tab')
      await ctx.waitText(/Captured files/i, 8000)
    },
  },
  {
    id: 'ana-files-api-shape', name: 'Files entries carry name + numeric size',
    tags: ['analytics', 'attach', 'files'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      await waitCompleted(ctx, pid)
      const id = await findRunId(ctx, pid)
      const files = await ctx.api.get(`/runs/${id}/files`)
      ctx.assert(files.length > 0, 'no files for run')
      for (const f of files) {
        ctx.assert(typeof f.name === 'string' && f.name.length > 0, 'file missing name')
        ctx.assert(typeof f.size === 'number' && f.size >= 0, `file ${f.name} bad size`)
      }
      await tab(ctx, /Files/i, 'files-tab')
      const items = await ctx.count('.files-item')
      ctx.assert(items === files.length, `UI files ${items} != API ${files.length}`)
    },
  },
  {
    id: 'ana-no-strace-tabs', name: 'Attach run hides I/O, Network, Syscalls, Logs tabs',
    tags: ['analytics', 'attach', 'edge'], timeout: 30000,
    run: async (ctx) => {
      await openAttach(ctx)
      for (const label of [/I\/O/i, /Network/i, /Syscalls/i, /Logs/i, /Processes/i]) {
        const n = await ctx.page.locator('.secondary-tab', { hasText: label }).count()
        ctx.assert(n === 0, `unexpected tab present for attach run: ${label}`)
      }
      // sanity: the psutil/perf tabs ARE there
      ctx.assert((await ctx.page.locator('.secondary-tab', { hasText: /Overview/i }).count()) > 0, 'no Overview tab')
      ctx.assert((await ctx.page.locator('.secondary-tab', { hasText: /Flamegraph/i }).count()) > 0, 'no Flamegraph tab')
    },
  },
  {
    id: 'ana-tabs-present-set', name: 'Attach run exposes exactly the psutil+perf tab set',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      await openAttach(ctx)
      for (const label of [/Overview/i, /Timeline/i, /Memory/i, /^CPU$/i, /Flamegraph/i, /Files/i]) {
        ctx.assert(
          (await ctx.page.locator('.secondary-tab', { hasText: label }).count()) > 0,
          `expected tab missing: ${label}`,
        )
      }
    },
  },
  {
    id: 'ana-overview-hot-function', name: 'Overview surfaces a hot_function finding for a CPU target',
    tags: ['analytics', 'attach', 'findings'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      const run = await waitCompleted(ctx, pid)
      ctx.assert(run && run.status === 'completed', 'attach run never completed')
      let anoms = []
      for (let i = 0; i < 8; i++) {
        anoms = await ctx.api.get(`/runs/${run.id}/anomalies`)
        if (Array.isArray(anoms) && anoms.length) break
        await ctx.sleep(400)
      }
      ctx.assert(Array.isArray(anoms), 'anomalies endpoint did not return an array')
      await tab(ctx, /Overview/i, 'overview-tab')
      await ctx.waitText(/Top findings/i, 8000)
      const hot = anoms.find((a) => a.rule_id === 'hot_function')
      if (hot) {
        ctx.assert(/hot function/i.test(hot.title || ''), 'hot_function title malformed')
        await ctx.waitFor('.anomaly-card', 8000)
      }
    },
  },
  {
    id: 'ana-tab-switch-back-forth', name: 'Switch Overview→CPU→Memory→Overview repeatedly',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      await openAttach(ctx)
      await tab(ctx, /Overview/i, 'overview-tab')
      await tab(ctx, /^CPU$/i, 'cpu-tab')
      await tab(ctx, /Memory/i, 'memory-tab')
      await tab(ctx, /Overview/i, 'overview-tab')
      await tab(ctx, /Memory/i, 'memory-tab')
      await tab(ctx, /^CPU$/i, 'cpu-tab')
    },
  },
  {
    id: 'ana-rapid-tab-cycle', name: 'Rapidly cycle every present tab and land on Files',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      await openAttach(ctx)
      const seq = [
        [/Overview/i, 'overview-tab'], [/Timeline/i, 'timeline-tab'], [/Memory/i, 'memory-tab'],
        [/^CPU$/i, 'cpu-tab'], [/Flamegraph/i, 'flamegraph-tab'], [/Files/i, 'files-tab'],
      ]
      for (const [label] of seq) {
        await ctx.page.locator('.secondary-tab', { hasText: label }).first().click()
        await ctx.sleep(120)
      }
      await ctx.waitFor('[data-testid="files-tab"]', 8000)
    },
  },
  {
    id: 'ana-timeline-then-flamegraph', name: 'Timeline then Flamegraph both render for one run',
    tags: ['analytics', 'attach', 'flamegraph'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      await waitCompleted(ctx, pid)
      await tab(ctx, /Timeline/i, 'timeline-tab')
      await tab(ctx, /Flamegraph/i, 'flamegraph-tab')
      await tab(ctx, /Overview/i, 'overview-tab')
    },
  },
  {
    id: 'ana-run-completes', name: 'Attach run flips to completed status',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      const run = await waitCompleted(ctx, pid, 9000)
      ctx.assert(run, 'attach run not found')
      ctx.assert(run.status === 'completed', `expected completed, got ${run.status}`)
      await tab(ctx, /Overview/i, 'overview-tab')
    },
  },
  {
    id: 'ana-overview-execution-snapshot', name: 'Overview shows execution snapshot stat grid',
    tags: ['analytics', 'attach'], timeout: 30000,
    run: async (ctx) => {
      const pid = await openAttach(ctx)
      await waitCompleted(ctx, pid)
      await tab(ctx, /Overview/i, 'overview-tab')
      await ctx.waitText(/Execution snapshot/i, 8000)
      const cells = await ctx.count('.stat-grid .stat-cell, .stat-grid > *')
      ctx.assert(cells > 0, 'no stat cells in execution snapshot')
    },
  },
]
