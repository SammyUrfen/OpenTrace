/** Diff/Compare batch — comparing two attach runs via the sidebar submenu. */
const H = require('./_helpers')

// Attach one cpu target as a fresh run; returns its pid.
async function attachOne(ctx) {
  const pid = await ctx.spawnTarget('cpu')
  await H.attachPid(ctx, pid, { window: 3 })
  return pid
}

// Guarantee >=2 runs exist (diff needs a partner); return one controlled pid to act on.
async function ensureTwo(ctx) {
  const pid = await attachOne(ctx)
  if ((await H.runCount(ctx)) < 2) await attachOne(ctx)
  return pid
}

// Right-click `pid`'s row, open "Compare with… ▸", click the first other run → diff tab.
async function openDiffFor(ctx, pid) {
  await H.runContextMenu(ctx, pid)
  await ctx.page.locator('.ctx-item', { hasText: /Compare with/i }).first().click()
  await ctx.waitFor('.ctx-submenu', 4000)
  await ctx.page.locator('.ctx-submenu .ctx-item').first().click()
  await ctx.waitFor('.main-tab__diff', 8000)
}

const diffTabCount = (ctx) => ctx.count('.main-tab__diff')

module.exports = [
  {
    id: 'diff-compare-opens-tab', name: 'Compare-with opens a diff main-tab',
    tags: ['diff'], timeout: 45000,
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const pid = await ensureTwo(ctx)
      const before = await diffTabCount(ctx)
      await openDiffFor(ctx, pid)
      ctx.assert((await diffTabCount(ctx)) > before, 'no diff tab appeared')
      await ctx.waitFor('[data-testid="overview-diff"]', 10000)
    },
  },
  {
    id: 'diff-overview-header-ab', name: 'Diff overview shows A ↔ B header',
    tags: ['diff'], timeout: 45000,
    run: async (ctx) => {
      const pid = await ensureTwo(ctx)
      await openDiffFor(ctx, pid)
      await ctx.waitFor('[data-testid="overview-diff"]', 10000)
      await ctx.waitFor('.diff-header', 6000)
      ctx.assert(await ctx.exists('.diff-header__tag--a'), 'no A tag in diff header')
      ctx.assert(await ctx.exists('.diff-header__tag--b'), 'no B tag in diff header')
      await ctx.assertText(/What changed/i, 'diff metric grid missing')
    },
  },
  {
    id: 'diff-syscall-subtab', name: 'Diff Syscalls Δ sub-tab renders',
    tags: ['diff'], timeout: 45000,
    run: async (ctx) => {
      const pid = await ensureTwo(ctx)
      await openDiffFor(ctx, pid)
      await ctx.waitFor('[data-testid="overview-diff"]', 10000)
      await ctx.page.locator('.secondary-tab', { hasText: /Syscalls/i }).first().click()
      await ctx.waitFor('[data-testid="syscall-diff"]', 8000)
    },
  },
  {
    id: 'diff-anomaly-subtab', name: 'Diff Anomalies Δ sub-tab renders',
    tags: ['diff'], timeout: 45000,
    run: async (ctx) => {
      const pid = await ensureTwo(ctx)
      await openDiffFor(ctx, pid)
      await ctx.waitFor('[data-testid="overview-diff"]', 10000)
      await ctx.page.locator('.secondary-tab', { hasText: /Anomalies/i }).first().click()
      await ctx.waitFor('[data-testid="anomaly-diff"]', 8000)
    },
  },
  {
    id: 'diff-cycle-subtabs', name: 'Cycle through all five diff sub-tabs',
    tags: ['diff'], timeout: 45000,
    run: async (ctx) => {
      const pid = await ensureTwo(ctx)
      await openDiffFor(ctx, pid)
      await ctx.waitFor('[data-testid="overview-diff"]', 10000)
      // Memory Δ and CPU Δ have no testid; just prove they select without error.
      for (const label of [/Memory/i, /CPU/i]) {
        await ctx.page.locator('.secondary-tab', { hasText: label }).first().click()
        await ctx.sleep(200)
      }
      await ctx.page.locator('.secondary-tab', { hasText: /Syscalls/i }).first().click()
      await ctx.waitFor('[data-testid="syscall-diff"]', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Anomalies/i }).first().click()
      await ctx.waitFor('[data-testid="anomaly-diff"]', 8000)
      await ctx.page.locator('.secondary-tab', { hasText: /Overview/i }).first().click()
      await ctx.waitFor('[data-testid="overview-diff"]', 8000)
    },
  },
  {
    id: 'diff-close-tab', name: 'Close a diff tab removes it',
    tags: ['diff'], timeout: 45000,
    run: async (ctx) => {
      const pid = await ensureTwo(ctx)
      const before = await diffTabCount(ctx)
      await openDiffFor(ctx, pid)
      ctx.assert((await diffTabCount(ctx)) > before, 'diff tab did not open')
      // the freshly opened diff is the active tab — close it via its ×
      await ctx.page.locator('.main-tab--active .main-tab__close').first().click()
      for (let i = 0; i < 15 && (await diffTabCount(ctx)) > before; i++) await ctx.sleep(200)
      ctx.assert((await diffTabCount(ctx)) === before, 'diff tab was not closed')
    },
  },
  {
    id: 'diff-self-not-offered', name: 'Compare submenu excludes the run itself',
    tags: ['diff', 'edge'], timeout: 45000,
    run: async (ctx) => {
      const pid = await ensureTwo(ctx)
      const total = await H.runCount(ctx)
      await H.runContextMenu(ctx, pid)
      await ctx.page.locator('.ctx-item', { hasText: /Compare with/i }).first().click()
      await ctx.waitFor('.ctx-submenu', 4000)
      const items = await ctx.count('.ctx-submenu .ctx-item')
      // self excluded + capped at 12 partners
      const expected = Math.min(total - 1, 12)
      ctx.assert(items === expected, `submenu listed ${items}, expected ${expected} (self excluded?)`)
      await ctx.page.locator('.ctx-backdrop').first().click().catch(() => {})
    },
  },
  {
    id: 'diff-submenu-lists-others', name: 'Compare submenu lists other runs',
    tags: ['diff'], timeout: 45000,
    run: async (ctx) => {
      await ensureTwo(ctx)
      await H.runContextMenu(ctx)
      await ctx.page.locator('.ctx-item', { hasText: /Compare with/i }).first().click()
      await ctx.waitFor('.ctx-submenu', 4000)
      await ctx.waitText(/Compare with/i, 3000)
      ctx.assert((await ctx.count('.ctx-submenu .ctx-item')) >= 1, 'submenu had no partner runs')
      await ctx.page.locator('.ctx-backdrop').first().click().catch(() => {})
    },
  },
  {
    id: 'diff-guard-needs-two', name: 'Compare guard matches run count',
    tags: ['diff', 'edge'], timeout: 45000,
    run: async (ctx) => {
      const pid = await attachOne(ctx)
      const total = await H.runCount(ctx)
      await H.runContextMenu(ctx, pid)
      const btn = ctx.page.locator('.ctx-item', { hasText: /Compare with/i }).first()
      await btn.waitFor({ state: 'visible', timeout: 4000 })
      const disabled = await btn.isDisabled()
      ctx.assert(disabled === (total < 2), `compare disabled=${disabled} but runs=${total}`)
      await ctx.page.locator('.ctx-backdrop').first().click().catch(() => {})
    },
  },
  {
    id: 'diff-reopen-dedupes', name: 'Reopening the same pair does not duplicate the tab',
    tags: ['diff', 'edge'], timeout: 45000,
    run: async (ctx) => {
      const pid = await ensureTwo(ctx)
      await openDiffFor(ctx, pid)
      const afterFirst = await diffTabCount(ctx)
      // same pid → same first partner → same diff key → focuses existing tab
      await openDiffFor(ctx, pid)
      await ctx.sleep(400)
      ctx.assert((await diffTabCount(ctx)) === afterFirst, 'duplicate diff tab created for same pair')
    },
  },
]
