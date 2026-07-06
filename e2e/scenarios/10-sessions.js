/** Sessions batch — create/rename/switch/count/delete + name edge cases. */
const H = require('./_helpers')

const uid = () => Math.random().toString(36).slice(2, 7)
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const re = (s) => new RegExp(esc(s))
const sessById = async (ctx, id) => (await ctx.api.get('/sessions')).find((s) => s.id === id)
const findByName = async (ctx, name) => (await ctx.api.get('/sessions')).find((s) => s.display_name === name)
const header = (ctx, name) => ctx.page.locator('.project-group__header', { hasText: name }).first()
const isActive = async (ctx, name) => {
  const cls = (await header(ctx, name).getAttribute('class')) || ''
  return /--active/.test(cls)
}
// runs the backend attributes to a given session id
const sessionRunCount = async (ctx, id) =>
  (await ctx.api.get('/runs?limit=500')).filter((r) => r.session_id === id).length

module.exports = [
  {
    id: 'sess-create-basic', name: 'Create a session shows in sidebar + backend',
    tags: ['sessions'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `alpha-${uid()}`
      await H.newSession(ctx, name)
      await ctx.waitFor('.session-list', 4000)
      await ctx.waitText(re(name), 5000)
      const s = await findByName(ctx, name)
      ctx.assert(s, `session "${name}" not in GET /sessions`)
      ctx.assert(s.slug && s.id, 'session missing slug/id')
    },
  },
  {
    id: 'sess-create-active', name: 'Newly created session becomes the active project',
    tags: ['sessions'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `active-${uid()}`
      await H.newSession(ctx, name)
      await ctx.waitText(re(name), 5000)
      ctx.assert(await isActive(ctx, name), 'new session is not marked active')
      // the active dot renders inside the active header's name span
      const dots = await header(ctx, name).locator('.project-group__active-dot').count()
      ctx.assert(dots > 0, 'active-dot indicator missing on new session')
    },
  },
  {
    id: 'sess-create-several', name: 'Create several sessions, all present',
    tags: ['sessions'],
    timeout: 40000,
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const names = [`multi-a-${uid()}`, `multi-b-${uid()}`, `multi-c-${uid()}`]
      for (const n of names) {
        await H.newSession(ctx, n)
        await ctx.waitText(re(n), 5000)
      }
      const all = await ctx.api.get('/sessions')
      for (const n of names) ctx.assert(all.some((s) => s.display_name === n), `missing "${n}" in backend`)
      for (const n of names) ctx.assert(await ctx.exists(`.project-group__header:has-text("${n}")`), `missing "${n}" row`)
    },
  },
  {
    id: 'sess-name-long', name: 'Session with a very long name is stored verbatim',
    tags: ['sessions', 'edge'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `long-${uid()}-` + 'x'.repeat(90)
      await H.newSession(ctx, name)
      const s = await findByName(ctx, name)
      ctx.assert(s, 'long-named session not stored')
      ctx.assert(s.display_name.length === name.length, 'long name was truncated by backend')
      // sidebar renders the full name in the DOM (may be scrolled off / CSS-clipped)
      ctx.assert((await ctx.count(`.project-group__name:has-text("${name.slice(0, 40)}")`)) > 0, 'long name not rendered')
    },
  },
  {
    id: 'sess-name-spaces', name: 'Session name with spaces keeps display, slugifies',
    tags: ['sessions', 'edge'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `spacey ${uid()} project name`
      await H.newSession(ctx, name)
      const s = await findByName(ctx, name)
      ctx.assert(s, 'spaced session not stored')
      ctx.assert(s.display_name === name, 'display_name should preserve spaces')
      ctx.assert(!/\s/.test(s.slug), `slug should be filesystem-safe, got "${s.slug}"`)
    },
  },
  {
    id: 'sess-name-unicode', name: 'Unicode session name round-trips',
    tags: ['sessions', 'edge'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `nétwörk-café-${uid()}-日本語`
      await H.newSession(ctx, name)
      await ctx.waitText(re(name), 5000)
      const s = await findByName(ctx, name)
      ctx.assert(s, 'unicode session not stored')
      ctx.assert(s.display_name === name, 'unicode name mangled')
      ctx.assert(s.slug && s.slug.length > 0, 'unicode name produced empty slug')
    },
  },
  {
    id: 'sess-name-emoji', name: 'Emoji session name round-trips',
    tags: ['sessions', 'edge'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `🚀 rocket ${uid()} 🔥`
      await H.newSession(ctx, name)
      await ctx.waitText(re(name), 5000)
      const s = await findByName(ctx, name)
      ctx.assert(s, 'emoji session not stored')
      ctx.assert(s.display_name === name, 'emoji name mangled')
    },
  },
  {
    id: 'sess-rename-ui', name: 'Rename a session via double-click reflects everywhere',
    tags: ['sessions'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const oldName = `torename-${uid()}`
      const newName = `renamed-${uid()}`
      await H.newSession(ctx, oldName)
      await ctx.waitText(re(oldName), 5000)
      const id = (await findByName(ctx, oldName)).id
      await header(ctx, oldName).dblclick()
      await ctx.waitFor('.modal--small input', 4000)
      await ctx.fill('.modal--small input', newName)
      await ctx.click('.modal--small .ai-btn--primary')
      await ctx.gone('.modal--small', 4000).catch(() => {})
      await ctx.waitText(re(newName), 5000)
      // ground truth: same id, new display_name
      for (let i = 0; i < 15; i++) { if ((await sessById(ctx, id)).display_name === newName) break; await ctx.sleep(200) }
      const s = await sessById(ctx, id)
      ctx.assert(s.display_name === newName, `rename not persisted (got "${s.display_name}")`)
      ctx.assert(!(await ctx.exists(`.project-group__header:has-text("${oldName}")`)), 'old name still shown')
    },
  },
  {
    id: 'sess-switch', name: 'Switch active session between two projects',
    tags: ['sessions'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const a = `switch-a-${uid()}`
      const b = `switch-b-${uid()}`
      await H.newSession(ctx, a)
      await H.newSession(ctx, b)
      await ctx.waitText(re(a), 5000)
      // b was created last => active; switching to a flips it
      ctx.assert(await isActive(ctx, b), 'last-created session should be active')
      await header(ctx, a).click()
      await ctx.sleep(400)
      ctx.assert(await isActive(ctx, a), 'clicking a session did not make it active')
      ctx.assert(!(await isActive(ctx, b)), 'previous session still marked active')
    },
  },
  {
    id: 'sess-run-count-badge', name: 'Session run-count badge tracks attached runs',
    tags: ['sessions', 'attach'],
    timeout: 40000,
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `counted-${uid()}`
      await H.newSession(ctx, name)
      await ctx.waitText(re(name), 5000)
      const id = (await findByName(ctx, name)).id
      ctx.assert((await sessionRunCount(ctx, id)) === 0, 'fresh session should have 0 runs')
      const badge = () => header(ctx, name).locator('.project-group__count')
      ctx.assert((await badge().innerText()).trim() === '0', 'badge should start at 0')
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      for (let i = 0; i < 25 && (await sessionRunCount(ctx, id)) < 1; i++) await ctx.sleep(300)
      ctx.assert((await sessionRunCount(ctx, id)) === 1, 'attached run did not land in this session')
      for (let i = 0; i < 15 && (await badge().innerText()).trim() !== '1'; i++) await ctx.sleep(300)
      ctx.assert((await badge().innerText()).trim() === '1', 'run-count badge did not update to 1')
    },
  },
  {
    id: 'sess-attach-lands-active', name: 'Attach lands under the active session, not another',
    tags: ['sessions', 'attach'],
    timeout: 40000,
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const other = `bystander-${uid()}`
      const target = `target-${uid()}`
      await H.newSession(ctx, other)  // created first
      await H.newSession(ctx, target) // created last => active
      await ctx.waitText(re(target), 5000)
      const otherId = (await findByName(ctx, other)).id
      const targetId = (await findByName(ctx, target)).id
      const pid = await ctx.spawnTarget('cpu')
      await H.attachPid(ctx, pid, { window: 3 })
      for (let i = 0; i < 25 && (await sessionRunCount(ctx, targetId)) < 1; i++) await ctx.sleep(300)
      ctx.assert((await sessionRunCount(ctx, targetId)) === 1, 'run did not attach to active session')
      ctx.assert((await sessionRunCount(ctx, otherId)) === 0, 'run leaked into the non-active session')
      // UI: the run row appears inside the active project's group
      const group = ctx.page.locator('.project-group', { has: ctx.page.locator('.project-group__name', { hasText: target }) })
      await group.locator('.run-row', { hasText: `pid ${pid}` }).first().waitFor({ state: 'visible', timeout: 8000 })
    },
  },
  {
    id: 'sess-delete-api', name: 'Delete a session removes it from the backend list',
    tags: ['sessions', 'edge'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `doomed-${uid()}`
      const created = await ctx.api.post('/sessions', { display_name: name })
      ctx.assert(created && created.id, 'POST /sessions did not return a session')
      ctx.assert(await findByName(ctx, name), 'created session missing before delete')
      const res = await ctx.api.del(`/sessions/${created.id}`)
      ctx.assert(res && res.deleted === true, 'delete did not report success')
      ctx.assert(!(await sessById(ctx, created.id)), 'session still present after delete')
      const got = await ctx.api.get(`/sessions/${created.id}`).catch(() => null)
      ctx.assert(!got || got.id !== created.id, 'deleted session still fetchable by id')
    },
  },
  {
    id: 'sess-empty-state-invariant', name: 'Empty-state and project list are mutually exclusive',
    tags: ['sessions'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      await ctx.waitFor('.session-list', 4000)
      const sessions = await ctx.api.get('/sessions')
      const emptyShown = await ctx.exists('.session-list__empty')
      const groups = await ctx.count('.project-group')
      if (sessions.length === 0) {
        ctx.assert(emptyShown, 'no sessions but "No sessions yet" empty state absent')
        ctx.assert(groups === 0, 'empty backend but project groups rendered')
        await ctx.assertText(/No sessions yet/i, 'empty-state text missing')
      } else {
        ctx.assert(!emptyShown, 'sessions exist but empty state still shown')
        ctx.assert(groups >= 1, 'sessions exist but no project group rendered')
      }
    },
  },
  {
    id: 'sess-duplicate-name-unique-slug', name: 'Two sessions with the same name get distinct slugs',
    tags: ['sessions', 'edge'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `dupe-${uid()}`
      await H.newSession(ctx, name)
      await H.newSession(ctx, name)
      const matches = (await ctx.api.get('/sessions')).filter((s) => s.display_name === name)
      ctx.assert(matches.length >= 2, `expected 2 sessions named "${name}", got ${matches.length}`)
      const slugs = new Set(matches.map((s) => s.slug))
      ctx.assert(slugs.size === matches.length, `slugs collided: ${[...slugs].join(', ')}`)
    },
  },
  {
    id: 'sess-dom-count-matches-backend', name: 'Sidebar project count matches GET /sessions',
    tags: ['sessions'],
    run: async (ctx) => {
      await ctx.dismissOnboarding()
      const name = `sync-${uid()}`
      await H.newSession(ctx, name)
      await ctx.waitText(re(name), 5000)
      const sessions = await ctx.api.get('/sessions')
      let groups = 0
      for (let i = 0; i < 15; i++) { groups = await ctx.count('.project-group'); if (groups === sessions.length) break; await ctx.sleep(200) }
      ctx.assert(groups === sessions.length, `sidebar shows ${groups} projects, backend has ${sessions.length}`)
    },
  },
]
