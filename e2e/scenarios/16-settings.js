/** Settings pane — nav sections, collector exclusivity, AI/tools/theme, close paths. */
const H = require('./_helpers')

// Click a settings nav item by label and wait a beat for the pane to render.
async function nav(ctx, re) {
  await ctx.page.locator('.settings__navitem', { hasText: re }).first().click()
  await ctx.sleep(200)
}

// Re-sync the collector checkboxes back to a captured Collectors object.
async function restoreCollectors(ctx, init) {
  const keys = ['psutil', 'strace', 'ltrace', 'perf']
  const cbs = ctx.page.locator('.settings__collector input[type=checkbox]')
  for (let i = 0; i < 4; i++) {
    const cur = await cbs.nth(i).isChecked().catch(() => false)
    if (cur !== !!init[keys[i]]) { await cbs.nth(i).click(); await ctx.sleep(150) }
  }
}

async function closeSettings(ctx) {
  await ctx.press('Escape')
  await ctx.gone('.settings-backdrop', 4000).catch(() => {})
}

module.exports = [
  {
    id: 'set-open-escape', name: 'Settings opens and closes with Escape',
    tags: ['settings'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      ctx.assert(await ctx.exists('.settings-backdrop'), 'backdrop missing')
      await ctx.press('Escape')
      await ctx.gone('.settings-backdrop', 4000)
    },
  },
  {
    id: 'set-close-backdrop', name: 'Clicking the backdrop closes settings',
    tags: ['settings'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings-backdrop', 5000)
      // click the backdrop at a corner well outside the centred panel
      await ctx.page.locator('.settings-backdrop').click({ position: { x: 5, y: 5 } })
      await ctx.gone('.settings-backdrop', 4000)
    },
  },
  {
    id: 'set-nav-general', name: 'General pane shows theme + data locations',
    tags: ['settings'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /General/i)
      await ctx.waitText(/Data locations/i, 5000)
      await ctx.assertText(/Theme/i, 'no Theme row in general')
      await ctx.assertText(/Database/i, 'no data-location keys')
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-nav-collectors', name: 'Collectors pane lists all four collectors',
    tags: ['settings', 'collectors'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /Collectors/i)
      await ctx.waitFor('.settings__collector', 5000)
      const n = await ctx.count('.settings__collector input[type=checkbox]')
      ctx.assert(n === 4, `expected 4 collector checkboxes, got ${n}`)
      await ctx.assertText(/mutually exclusive/i, 'exclusivity note missing')
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-nav-ai', name: 'AI pane exposes base-url/model/key fields',
    tags: ['settings', 'ai'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /AI/i)
      await ctx.waitText(/Base URL/i, 5000)
      await ctx.assertText(/Model/i, 'no Model field')
      await ctx.assertText(/API key/i, 'no API key field')
      const fields = await ctx.count('.field input')
      ctx.assert(fields >= 3, `expected >=3 AI inputs, got ${fields}`)
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-nav-tools', name: 'Tools pane renders every detected tool',
    tags: ['settings', 'tools'],
    run: async (ctx) => {
      const info = await ctx.api.get('/info/tools')
      const expected = (info.tools || []).length
      ctx.assert(expected > 0, 'backend reported no tools')
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /Tracing tools/i)
      await ctx.waitFor('.tool', 6000)
      for (let i = 0; i < 20 && (await ctx.count('.tool')) < expected; i++) await ctx.sleep(200)
      const shown = await ctx.count('.tool')
      ctx.assert(shown === expected, `tools mismatch: UI ${shown} vs API ${expected}`)
      // strace/ltrace/perf should each be named
      await ctx.assertText(/strace/i, 'strace not listed')
      await ctx.assertText(/perf/i, 'perf not listed')
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-nav-guide', name: 'Guide pane renders the usage guide',
    tags: ['settings', 'guide'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /Guide/i)
      await ctx.waitText(/How to use OpenTrace/i, 5000)
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-nav-about', name: 'About pane shows version from /info',
    tags: ['settings', 'about'],
    run: async (ctx) => {
      const info = await ctx.api.get('/info')
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /About/i)
      await ctx.waitText(/About OpenTrace/i, 5000)
      await ctx.assertText(/Version/i, 'no version row')
      if (info.version) {
        await ctx.waitText(new RegExp(String(info.version).replace(/[.]/g, '\\.')), 5000)
      }
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-nav-cycle-active', name: 'Cycling nav items marks the active one',
    tags: ['settings'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      for (const label of [/General/i, /Collectors/i, /AI/i, /Tracing tools/i, /Guide/i, /About/i]) {
        const item = ctx.page.locator('.settings__navitem', { hasText: label }).first()
        await item.click()
        await ctx.sleep(120)
        const cls = await item.getAttribute('class')
        ctx.assert(/settings__navitem--active/.test(cls || ''), `nav ${label} not active after click`)
      }
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-collectors-strace-ltrace-exclusive', name: 'Enabling ltrace disables strace (and vice versa)',
    tags: ['settings', 'collectors'],
    run: async (ctx) => {
      const init = (await ctx.api.get('/config/tracing')).collectors
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /Collectors/i)
      await ctx.waitFor('.settings__collector', 5000)
      const cbs = ctx.page.locator('.settings__collector input[type=checkbox]')
      const strace = cbs.nth(1)
      const ltrace = cbs.nth(2)
      // ensure strace ON first
      if (!(await strace.isChecked())) { await strace.click(); await ctx.sleep(200) }
      ctx.assert(await strace.isChecked(), 'strace failed to enable')
      // enabling ltrace must switch strace OFF
      await ltrace.click()
      await ctx.sleep(300)
      ctx.assert(await ltrace.isChecked(), 'ltrace failed to enable')
      ctx.assert(!(await strace.isChecked()), 'strace still on after enabling ltrace (not exclusive)')
      // ground truth from persisted config
      let cfg = null
      for (let i = 0; i < 15; i++) {
        cfg = (await ctx.api.get('/config/tracing')).collectors
        if (cfg.ltrace && !cfg.strace) break
        await ctx.sleep(200)
      }
      ctx.assert(cfg.ltrace && !cfg.strace, `backend config not exclusive: ${JSON.stringify(cfg)}`)
      // reverse: enabling strace must switch ltrace off
      await strace.click()
      await ctx.sleep(300)
      ctx.assert((await strace.isChecked()) && !(await ltrace.isChecked()), 'reverse exclusivity failed')
      await restoreCollectors(ctx, init)
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-collectors-toggle-persists', name: 'Toggling a collector persists to /config/tracing',
    tags: ['settings', 'collectors'],
    run: async (ctx) => {
      const init = (await ctx.api.get('/config/tracing')).collectors
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /Collectors/i)
      await ctx.waitFor('.settings__collector', 5000)
      const perf = ctx.page.locator('.settings__collector input[type=checkbox]').nth(3)
      const was = await perf.isChecked()
      await perf.click()
      await ctx.sleep(300)
      let cfg = null
      for (let i = 0; i < 15; i++) {
        cfg = (await ctx.api.get('/config/tracing')).collectors
        if (cfg.perf === !was) break
        await ctx.sleep(200)
      }
      ctx.assert(cfg.perf === !was, `perf toggle not persisted: was ${was}, backend ${cfg.perf}`)
      await restoreCollectors(ctx, init)
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-general-theme-toggle', name: 'Theme button in General flips data-theme',
    tags: ['settings', 'theme'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /General/i)
      const before = await ctx.page.evaluate(() => document.documentElement.dataset.theme)
      await ctx.page.locator('.settings__row', { hasText: /Theme/i }).first().locator('button').click()
      await ctx.sleep(300)
      const after = await ctx.page.evaluate(() => document.documentElement.dataset.theme)
      ctx.assert(before !== after, `theme did not flip (${before} -> ${after})`)
      // restore
      await ctx.page.locator('.settings__row', { hasText: /Theme/i }).first().locator('button').click()
      await ctx.sleep(200)
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-ai-continuous-toggle', name: 'Continuous summaries toggle persists to /config/llm',
    tags: ['settings', 'ai'],
    run: async (ctx) => {
      const init = !!(await ctx.api.get('/config/llm')).continuous_summaries
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /AI/i)
      await ctx.waitText(/Continuous incident summaries/i, 5000)
      await ctx.page.locator('.settings__row', { hasText: /Continuous incident summaries/i })
        .first().locator('button').click()
      let cur = init
      for (let i = 0; i < 15; i++) {
        cur = !!(await ctx.api.get('/config/llm')).continuous_summaries
        if (cur !== init) break
        await ctx.sleep(200)
      }
      ctx.assert(cur !== init, `continuous_summaries did not change from ${init}`)
      // restore
      await ctx.page.locator('.settings__row', { hasText: /Continuous incident summaries/i })
        .first().locator('button').click()
      await ctx.sleep(300)
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-tools-recheck', name: 'Tools recheck button re-renders the list',
    tags: ['settings', 'tools'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /Tracing tools/i)
      await ctx.waitFor('.tool', 6000)
      const n0 = await ctx.count('.tool')
      await ctx.page.locator('.settings__refresh', { hasText: /recheck/i }).first().click()
      await ctx.sleep(600)
      const n1 = await ctx.count('.tool')
      ctx.assert(n1 === n0 && n1 > 0, `tool list changed on recheck: ${n0} -> ${n1}`)
      await ctx.assertText(/perf_event_paranoid/i, 'paranoid line missing after recheck')
      await closeSettings(ctx)
    },
  },
  {
    id: 'set-reopen-remembers-nothing-stale', name: 'Reopening settings defaults back to General',
    tags: ['settings', 'edge'],
    run: async (ctx) => {
      // navigate to About, close, reopen — should land on General again
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await nav(ctx, /About/i)
      await ctx.waitText(/About OpenTrace/i, 5000)
      await closeSettings(ctx)
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      const general = ctx.page.locator('.settings__navitem', { hasText: /General/i }).first()
      const cls = await general.getAttribute('class')
      ctx.assert(/settings__navitem--active/.test(cls || ''), 'settings did not default to General on reopen')
      await ctx.assertText(/Data locations/i, 'General pane not shown on reopen')
      await closeSettings(ctx)
    },
  },
]
