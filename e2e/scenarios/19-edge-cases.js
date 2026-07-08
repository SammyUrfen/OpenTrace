/** Edge cases & robustness — bug-hunting: dead pids, bad payloads, rapid toggles, empty states. */
const H = require('./_helpers')

module.exports = [
  {
    id: 'edge-attach-dead-pid-empty', name: 'Attach filter for a dead pid shows empty state',
    tags: ['edge', 'attach'],
    run: async (ctx) => {
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.fill('.attach__search', '99999991')
      await ctx.waitText(/No matching processes/i, 6000)
      ctx.assert(await ctx.exists('.attach__empty'), 'no .attach__empty element for dead pid')
      await ctx.press('Escape')
      await ctx.gone('.attach__search', 4000)
    },
  },
  {
    id: 'edge-attach-out-of-range-pid-4xx', name: 'POST /runs/attach with an absurd pid is rejected',
    tags: ['edge', 'attach', 'api'],
    run: async (ctx) => {
      let threw = false
      try {
        await ctx.api.post('/runs/attach', { pid: 2147483646, window_s: 3, monitor: false, ebpf: false })
      } catch (e) {
        threw = true
        ctx.assert(/-> 4\d\d/.test(String(e.message)), `expected a 4xx, got: ${e.message}`)
      }
      ctx.assert(threw, 'attaching to a non-existent pid should have failed')
    },
  },
  {
    id: 'edge-attach-then-delete-running', name: 'Attach then immediately delete the still-running run',
    tags: ['edge', 'attach', 'runs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 20 })
      const before = await H.runCount(ctx)
      await H.runContextMenu(ctx, pid)
      await ctx.page.locator('.ctx-item--danger', { hasText: /Delete/i }).first().click()
      for (let i = 0; i < 15 && (await H.runCount(ctx)) >= before; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) < before, 'running run was not deleted')
      ctx.assert(!(await ctx.exists(`.run-row:has-text("pid ${pid}")`)), 'row for deleted pid still present')
    },
  },
  {
    id: 'edge-open-then-delete-tab-closes', name: 'Opening a run then deleting it closes its tab',
    tags: ['edge', 'runs', 'tabs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.main-tabs', 6000)
      await H.runContextMenu(ctx, pid)
      await ctx.page.locator('.ctx-item--danger', { hasText: /Delete/i }).first().click()
      await ctx.sleep(1500)
      const tab = ctx.page.locator('.main-tab', { hasText: `pid ${pid}` })
      ctx.assert((await tab.count()) === 0, 'tab for the deleted run stayed open')
    },
  },
  {
    id: 'edge-rapid-theme-toggle', name: 'Rapidly toggling the theme 5x leaves a valid theme',
    tags: ['edge', 'ui'],
    run: async (ctx) => {
      const read = () => ctx.page.evaluate(() => document.documentElement.dataset.theme)
      const start = await read()
      for (let i = 0; i < 5; i++) { await H.toggleTheme(ctx); await ctx.sleep(120) }
      const end = await read()
      ctx.assert(end === 'dark' || end === 'light', `theme ended invalid: ${end}`)
      // 5 flips = odd number, so it should differ from the start
      ctx.assert(end !== start, `expected an odd number of flips to change theme (${start} -> ${end})`)
    },
  },
  {
    id: 'edge-rapid-palette-open-close', name: 'Rapidly opening/closing the palette 5x stays stable',
    tags: ['edge', 'palette'],
    run: async (ctx) => {
      for (let i = 0; i < 5; i++) {
        await ctx.press('Control+k')
        await ctx.waitFor('.palette__input', 4000)
        await ctx.press('Escape')
        await ctx.gone('.palette', 4000)
      }
      // palette fully dismissed and the app still responds
      ctx.assert(!(await ctx.exists('.palette')), 'palette left open after rapid cycling')
      await ctx.assertText(/Terminal Tracing (ON|OFF)/i, 'app shell broke after rapid palette cycling')
    },
  },
  {
    id: 'edge-attach-five-delete-all-to-zero', name: 'Attach 5 runs then delete every run down to zero',
    tags: ['edge', 'attach', 'runs'],
    timeout: 60000,
    run: async (ctx) => {
      for (let i = 0; i < 5; i++) {
        const pid = await ctx.spawnTarget('cpu')
        await H.attachPid(ctx, pid, { window: 3 })
      }
      ctx.assert((await H.runCount(ctx)) >= 5, 'expected at least 5 runs before purge')
      // delete every run via the UI (right-click → Delete), the real user flow —
      // this is what refreshes the sidebar (a raw API delete would not).
      for (let guard = 0; guard < 40 && (await ctx.count('.run-row')) > 0; guard++) {
        await ctx.page.locator('.run-row').first().click({ button: 'right' })
        await ctx.page.locator('.ctx-item--danger', { hasText: /Delete/i }).first().click()
        await ctx.sleep(300)
      }
      ctx.assert((await H.runCount(ctx)) === 0, 'runs remained after deleting all')
      ctx.assert((await ctx.count('.run-row')) === 0, 'sidebar still shows run rows after purge')
    },
  },
  {
    id: 'edge-settings-over-attach-escape-layering', name: 'Escape layering with attach modal then settings',
    tags: ['edge', 'settings', 'attach'],
    run: async (ctx) => {
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      // Escape should close the attach modal cleanly
      await ctx.press('Escape')
      await ctx.gone('.attach__search', 4000)
      // now settings should open on top of a clean shell
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      await ctx.press('Escape')
      await ctx.gone('.settings', 4000)
      ctx.assert(!(await ctx.exists('.attach__search')) && !(await ctx.exists('.settings')), 'a modal leaked after escape layering')
    },
  },
  {
    id: 'edge-session-empty-name-noop', name: 'Creating a session with an empty name is a no-op',
    tags: ['edge', 'sessions'],
    run: async (ctx) => {
      const before = (await ctx.api.get('/sessions')).length
      await H.cmd(ctx, /New session/i)
      await ctx.waitFor('.modal--small input', 4000)
      await ctx.fill('.modal--small input', '')
      const submit = ctx.page.locator('.modal--small .ai-btn--primary').first()
      const disabled = await submit.isDisabled().catch(() => false)
      if (!disabled) await submit.click().catch(() => {})
      await ctx.sleep(600)
      await ctx.press('Escape')
      await ctx.gone('.modal--small', 4000).catch(() => {})
      const after = (await ctx.api.get('/sessions')).length
      ctx.assert(after === before, `empty name created a session (${before} -> ${after})`)
    },
  },
  {
    id: 'edge-attach-window-min', name: 'Attach with the window clamped to its minimum',
    tags: ['edge', 'attach'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      const min = await ctx.page.locator('.attach__window input').first().getAttribute('min')
      await ctx.fill('.attach__search', String(pid))
      await ctx.fill('.attach__window input', String(min || 3))
      const before = await H.runCount(ctx)
      const row = ctx.page.locator('.attach__row', { hasText: `pid ${pid}` }).first()
      await row.waitFor({ state: 'visible', timeout: 8000 })
      await row.click()
      for (let i = 0; i < 20 && (await H.runCount(ctx)) <= before; i++) await ctx.sleep(300)
      ctx.assert((await H.runCount(ctx)) > before, 'min-window attach created no run')
    },
  },
  {
    id: 'edge-attach-window-max-clamps', name: 'Attach window input clamps values above its maximum',
    tags: ['edge', 'attach'],
    run: async (ctx) => {
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__window input', 5000)
      const input = ctx.page.locator('.attach__window input').first()
      const max = Number(await input.getAttribute('max')) || 120
      await input.fill(String(max + 9999))
      await input.blur().catch(() => {})
      await ctx.sleep(300)
      const val = Number(await input.inputValue())
      ctx.assert(val <= max, `window not clamped to max: ${val} > ${max}`)
      await ctx.press('Escape')
      await ctx.gone('.attach__window input', 4000)
    },
  },
  {
    id: 'edge-escape-with-nothing-open', name: 'Pressing Escape with nothing open does not crash',
    tags: ['edge', 'ui'],
    run: async (ctx) => {
      // ensure a clean slate
      ctx.assert(!(await ctx.exists('.palette')) && !(await ctx.exists('.settings')), 'expected nothing open')
      for (let i = 0; i < 3; i++) { await ctx.press('Escape'); await ctx.sleep(150) }
      await ctx.assertText(/Terminal Tracing (ON|OFF)/i, 'shell broke after stray Escapes')
    },
  },
  {
    id: 'edge-attach-filter-clear-restores', name: 'Clearing a dead-pid filter restores the process list',
    tags: ['edge', 'attach'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('idle')
      await H.cmd(ctx, /Attach to running process/i)
      await ctx.waitFor('.attach__search', 5000)
      await ctx.fill('.attach__search', '99999991')
      await ctx.waitText(/No matching processes/i, 6000)
      await ctx.fill('.attach__search', String(pid))
      const row = ctx.page.locator('.attach__row', { hasText: `pid ${pid}` }).first()
      await row.waitFor({ state: 'visible', timeout: 8000 })
      ctx.assert(!(await ctx.exists('.attach__empty')), 'empty state stuck after clearing filter')
      await ctx.press('Escape')
      await ctx.gone('.attach__search', 4000)
    },
  },
  {
    id: 'edge-close-tab-then-reopen', name: 'Closing a run tab then reopening it works',
    tags: ['edge', 'tabs', 'runs'],
    run: async (ctx) => {
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 4 })
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor('.main-tab', 6000)
      const tab = ctx.page.locator('.main-tab', { hasText: `pid ${pid}` }).first()
      await tab.locator('.main-tab__close').first().click()
      await ctx.sleep(600)
      ctx.assert((await ctx.page.locator('.main-tab', { hasText: `pid ${pid}` }).count()) === 0, 'tab did not close')
      // reopen from the sidebar — should not crash and the tab returns
      await H.openRunByPid(ctx, pid)
      await ctx.waitFor(`.main-tab:has-text("pid ${pid}")`, 6000)
    },
  },
  {
    id: 'edge-attach-negative-pid-4xx', name: 'POST /runs/attach with a non-positive pid is rejected',
    tags: ['edge', 'attach', 'api'],
    run: async (ctx) => {
      let threw = false
      try {
        await ctx.api.post('/runs/attach', { pid: -1, window_s: 3, monitor: false, ebpf: false })
      } catch (e) {
        threw = true
        ctx.assert(/-> 4\d\d/.test(String(e.message)), `expected a 4xx, got: ${e.message}`)
      }
      ctx.assert(threw, 'a negative pid should be rejected')
    },
  },
  {
    id: 'edge-empty-then-valid-session', name: 'Empty session name is rejected but a valid one still works',
    tags: ['edge', 'sessions'],
    run: async (ctx) => {
      const before = (await ctx.api.get('/sessions')).length
      await H.cmd(ctx, /New session/i)
      await ctx.waitFor('.modal--small input', 4000)
      await ctx.fill('.modal--small input', '   ')
      const submit = ctx.page.locator('.modal--small .ai-btn--primary').first()
      if (!(await submit.isDisabled().catch(() => false))) await submit.click().catch(() => {})
      await ctx.sleep(400)
      // whitespace name should not have created anything
      const mid = (await ctx.api.get('/sessions')).length
      ctx.assert(mid === before, 'whitespace-only name created a session')
      // recover with a real name in the same or a fresh modal
      if (!(await ctx.exists('.modal--small input'))) {
        await H.cmd(ctx, /New session/i)
        await ctx.waitFor('.modal--small input', 4000)
      }
      await ctx.fill('.modal--small input', 'edge-recover-session')
      await ctx.click('.modal--small .ai-btn--primary')
      await ctx.gone('.modal--small', 4000).catch(() => {})
      await ctx.waitText(/edge-recover-session/i, 5000)
      const after = (await ctx.api.get('/sessions')).length
      ctx.assert(after === before + 1, `expected exactly one new session (${before} -> ${after})`)
    },
  },
]
