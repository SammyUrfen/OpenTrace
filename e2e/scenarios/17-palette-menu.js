/** Command palette + MenuBar batch — filtering, keyboard nav, every action, menu dropdowns. */
const H = require('./_helpers')

const openPalette = async (ctx) => {
  await ctx.press('Control+k')
  await ctx.waitFor('.palette__input', 4000)
}
const openTop = async (ctx, topRe) => {
  await ctx.page.locator('.menubar__top', { hasText: topRe }).first().click()
  await ctx.waitFor('.menubar__dropdown', 4000)
}
const dropdownTexts = (ctx) => ctx.page.locator('.menubar__dropdown .menubar__menuitem').allInnerTexts()

module.exports = [
  {
    id: 'pal-filter-attach', name: 'Typing "attach" filters palette to the attach action',
    tags: ['palette'],
    run: async (ctx) => {
      await openPalette(ctx)
      await ctx.type('.palette__input', 'attach')
      const item = ctx.page.locator('.palette__item', { hasText: /Attach to running process/i })
      await item.first().waitFor({ state: 'visible', timeout: 4000 })
      ctx.assert((await item.count()) >= 1, 'attach action not shown for "attach" filter')
      await ctx.press('Escape')
      await ctx.gone('.palette', 4000)
    },
  },
  {
    id: 'pal-filter-no-match', name: 'Nonsense query shows the empty "No matches" state',
    tags: ['palette', 'edge'],
    run: async (ctx) => {
      await openPalette(ctx)
      await ctx.type('.palette__input', 'zzzqqxnope')
      await ctx.waitFor('.palette__empty', 4000)
      await ctx.assertText(/No matches/i, 'empty state text missing')
      ctx.assert((await ctx.count('.palette__item')) === 0, 'items still present with no match')
      await ctx.press('Escape')
      await ctx.gone('.palette', 4000)
    },
  },
  {
    id: 'pal-arrow-nav', name: 'ArrowDown/ArrowUp moves the active palette item',
    tags: ['palette'],
    run: async (ctx) => {
      await openPalette(ctx)
      const n = await ctx.count('.palette__item')
      ctx.assert(n >= 3, `need several items to nav, got ${n}`)
      const activeIdx = async () => ctx.page.evaluate(() => {
        const items = [...document.querySelectorAll('.palette__item')]
        return items.findIndex((el) => el.classList.contains('palette__item--active'))
      })
      const start = await activeIdx()
      await ctx.press('ArrowDown')
      await ctx.press('ArrowDown')
      const down = await activeIdx()
      ctx.assert(down > start, `ArrowDown did not advance active (${start} -> ${down})`)
      await ctx.press('ArrowUp')
      const up = await activeIdx()
      ctx.assert(up < down, `ArrowUp did not move back (${down} -> ${up})`)
      await ctx.press('Escape')
      await ctx.gone('.palette', 4000)
    },
  },
  {
    id: 'pal-enter-runs-action', name: 'Enter runs the highlighted action (Toggle theme)',
    tags: ['palette', 'settings'],
    run: async (ctx) => {
      const before = await ctx.page.evaluate(() => document.documentElement.dataset.theme)
      await openPalette(ctx)
      await ctx.type('.palette__input', 'Toggle theme')
      await ctx.page.locator('.palette__item', { hasText: /Toggle theme/i }).first().waitFor({ state: 'visible', timeout: 4000 })
      await ctx.press('Enter')
      await ctx.gone('.palette', 4000)
      await ctx.sleep(300)
      const after = await ctx.page.evaluate(() => document.documentElement.dataset.theme)
      ctx.assert(before !== after, `Enter did not run Toggle theme (${before} -> ${after})`)
      await H.toggleTheme(ctx) // restore
      await ctx.sleep(200)
    },
  },
  {
    id: 'pal-escape-closes', name: 'Escape closes the palette even mid-filter',
    tags: ['palette'],
    run: async (ctx) => {
      await openPalette(ctx)
      await ctx.type('.palette__input', 'sett')
      await ctx.press('Escape')
      await ctx.gone('.palette', 4000)
    },
  },
  {
    id: 'pal-action-new-session', name: 'Palette "New session" action creates a session',
    tags: ['palette', 'sessions'],
    run: async (ctx) => {
      const name = `pal-sess-${Date.now()}`
      await H.newSession(ctx, name)
      await ctx.waitText(new RegExp(name, 'i'), 5000)
      const sessions = await ctx.api.get('/sessions')
      ctx.assert(sessions.some((s) => (s.display_name || s.name || '') === name), 'session not in backend')
    },
  },
  {
    id: 'pal-action-open-settings', name: 'Palette "Open settings" opens the settings page',
    tags: ['palette', 'settings'],
    run: async (ctx) => {
      await H.openSettings(ctx)
      await ctx.waitFor('.settings', 5000)
      ctx.assert(await ctx.exists('.settings__nav'), 'settings nav missing')
      await ctx.press('Escape')
      await ctx.gone('.settings', 4000).catch(() => {})
    },
  },
  {
    id: 'pal-action-guide', name: 'Palette "How to use OpenTrace" opens the Guide section',
    tags: ['palette', 'settings'],
    run: async (ctx) => {
      await H.cmd(ctx, /How to use OpenTrace/i)
      await ctx.waitFor('.settings', 5000)
      const active = ctx.page.locator('.settings__navitem--active')
      await active.waitFor({ state: 'visible', timeout: 4000 })
      ctx.assert(/Guide/i.test(await active.innerText()), 'guide section not active')
      await ctx.press('Escape')
      await ctx.gone('.settings', 4000).catch(() => {})
    },
  },
  {
    id: 'pal-action-tracing-toggle', name: 'Palette turns tracing ON then OFF',
    tags: ['palette'],
    run: async (ctx) => {
      // detect state via the toggle CLASS — body text is unreliable (the Live
      // Monitor hint literally says "toggle OpenTrace on to trace").
      const isOn = () => ctx.exists('.tracing-toggle--on')
      if (await isOn()) { await H.toggleTracing(ctx); await ctx.waitFor('.tracing-toggle--off', 4000) }
      await H.toggleTracing(ctx)
      await ctx.waitFor('.tracing-toggle--on', 4000)
      await H.toggleTracing(ctx)
      await ctx.waitFor('.tracing-toggle--off', 4000)
    },
  },
  {
    id: 'pal-menu-file-items', name: 'MenuBar File dropdown lists New Session + Settings',
    tags: ['menu'],
    run: async (ctx) => {
      await openTop(ctx, /File/i)
      const texts = (await dropdownTexts(ctx)).join(' | ')
      ctx.assert(/New Session/i.test(texts), `File missing New Session: ${texts}`)
      ctx.assert(/Settings/i.test(texts), `File missing Settings: ${texts}`)
      await ctx.press('Escape')
      await ctx.gone('.menubar__dropdown', 4000)
    },
  },
  {
    id: 'pal-menu-view-items', name: 'MenuBar View dropdown lists palette/sidebar/terminal/theme',
    tags: ['menu'],
    run: async (ctx) => {
      await openTop(ctx, /View/i)
      const texts = (await dropdownTexts(ctx)).join(' | ')
      for (const re of [/Command Palette/i, /Toggle Sidebar/i, /Toggle Terminal/i, /Toggle Theme/i]) {
        ctx.assert(re.test(texts), `View missing ${re}: ${texts}`)
      }
      await ctx.press('Escape')
      await ctx.gone('.menubar__dropdown', 4000)
    },
  },
  {
    id: 'pal-menu-run-help-items', name: 'MenuBar Run + Help dropdowns list their actions',
    tags: ['menu'],
    run: async (ctx) => {
      await openTop(ctx, /Run/i)
      const runTexts = (await dropdownTexts(ctx)).join(' | ')
      ctx.assert(/Attach to running process/i.test(runTexts), `Run missing Attach: ${runTexts}`)
      ctx.assert(/Turn Tracing (On|Off)/i.test(runTexts), `Run missing tracing toggle: ${runTexts}`)
      await ctx.press('Escape')
      await ctx.gone('.menubar__dropdown', 4000)
      await openTop(ctx, /Help/i)
      const helpTexts = (await dropdownTexts(ctx)).join(' | ')
      ctx.assert(/How to Use OpenTrace/i.test(helpTexts), `Help missing guide: ${helpTexts}`)
      ctx.assert(/About OpenTrace/i.test(helpTexts), `Help missing about: ${helpTexts}`)
      await ctx.press('Escape')
      await ctx.gone('.menubar__dropdown', 4000)
    },
  },
  {
    id: 'pal-menu-toggle-terminal', name: 'View → Toggle Terminal flips the terminal region',
    tags: ['menu', 'ui'],
    run: async (ctx) => {
      const hidden = () => ctx.page.evaluate(() => !!document.querySelector('.app-shell--no-terminal'))
      const before = await hidden()
      await H.menu(ctx, /View/i, /Toggle Terminal/i)
      await ctx.sleep(400)
      ctx.assert((await hidden()) !== before, 'terminal region did not toggle')
      await H.menu(ctx, /View/i, /Toggle Terminal/i) // restore
      await ctx.sleep(300)
      ctx.assert((await hidden()) === before, 'terminal toggle did not restore')
    },
  },
  {
    id: 'pal-menu-about', name: 'Help → About OpenTrace opens the settings About section',
    tags: ['menu', 'settings'],
    run: async (ctx) => {
      await H.menu(ctx, /Help/i, /About OpenTrace/i)
      await ctx.waitFor('.settings', 5000)
      const active = ctx.page.locator('.settings__navitem--active')
      await active.waitFor({ state: 'visible', timeout: 4000 })
      ctx.assert(/About/i.test(await active.innerText()), 'About section not active')
      await ctx.assertText(/About OpenTrace/i, 'about pane heading missing')
      await ctx.press('Escape')
      await ctx.gone('.settings', 4000).catch(() => {})
    },
  },
]
