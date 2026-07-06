/**
 * Scenario context (`ctx`) — the API every scenario uses. A scenario is
 * `async (ctx) => { ... }` and FAILS by throwing (ctx.assert / a Playwright
 * timeout). The runner records the throw + any console/page errors.
 *
 * ctx.page                     the Playwright renderer page (escape hatch)
 * await ctx.dismissOnboarding()  close the first-run wizard if present
 * await ctx.click(sel)         click a CSS selector (auto-waits, visible)
 * await ctx.clickText(re)      click the first element whose text matches
 * await ctx.fill(sel, text)    clear+type into an input
 * await ctx.type(sel, text)    focus+type (keystrokes)
 * await ctx.press(key)         keyboard press (e.g. 'Control+K', 'Escape')
 * await ctx.waitFor(sel, ms?)  wait for a selector to be visible
 * await ctx.waitText(re, ms?)  wait for text to appear anywhere
 * await ctx.gone(sel, ms?)     wait for a selector to detach/hide
 * await ctx.exists(sel)        -> boolean (present now)
 * await ctx.count(sel)         -> number
 * await ctx.text()             -> full visible body text
 * await ctx.shot(name)         screenshot into out/<scenario>-<name>.png
 * ctx.assert(cond, msg)        throw msg if falsy
 * await ctx.assertText(re,msg?) assert body text matches
 * await ctx.sleep(ms)
 * ctx.api.get(path) / ctx.api.post(path, body) / ctx.api.del(path)  -> json
 * await ctx.spawnTarget(kind?) spawn a throwaway process to attach to -> pid
 *                              kinds: 'cpu'|'idle'|'fdleak'|'memgrow' (auto-killed)
 * ctx.backendUrl               the isolated backend base url
 */
const { spawn } = require('child_process')
const path = require('path')
const { PY } = require('./harness')

// Targets hold ~230MB RSS so they float into the attach modal's top-60-by-RSS list
// (the modal filters a server-side top-60, so a tiny process would be invisible).
const _HOLD = '_ballast=bytearray(230*1024*1024)\n'
const TARGETS = {
  cpu: _HOLD + 'def b():\n x=0\n for i in range(3000000): x+=i*i\nwhile True: b()',
  idle: _HOLD + 'import time\nwhile True: time.sleep(1)',
  fdleak: _HOLD + 'import socket,time\nh=[]\nwhile True:\n h.append(socket.socket()); time.sleep(0.02)',
  memgrow: _HOLD + 'import time\nb=[]\nwhile True:\n b.append(bytearray(4*1024*1024)); time.sleep(0.05)',
}

function makeCtx(appHandle, scenarioId) {
  const { page, backendUrl } = appHandle
  const spawned = []
  let shotN = 0

  const api = {
    async _req(method, p, body) {
      const res = await fetch(`${backendUrl}${p}`, {
        method,
        headers: body ? { 'content-type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      const text = await res.text()
      let json = null
      try { json = text ? JSON.parse(text) : null } catch { json = text }
      if (!res.ok) throw new Error(`API ${method} ${p} -> ${res.status}: ${text.slice(0, 200)}`)
      return json
    },
    get: (p) => api._req('GET', p),
    post: (p, b) => api._req('POST', p, b),
    del: (p) => api._req('DELETE', p),
  }

  const ctx = {
    page,
    backendUrl,
    api,
    async dismissOnboarding() {
      for (let i = 0; i < 6; i++) {
        const btn = page.locator('.ai-btn--primary').first()
        if ((await btn.count()) && (await btn.isVisible().catch(() => false))) {
          await btn.click().catch(() => {})
          await page.waitForTimeout(250)
        } else break
      }
    },
    click: (sel) => page.locator(sel).first().click({ timeout: 8000 }),
    clickText: (re) => page.getByText(re).first().click({ timeout: 8000 }),
    fill: (sel, text) => page.locator(sel).first().fill(String(text), { timeout: 8000 }),
    type: async (sel, text) => { await page.locator(sel).first().focus(); await page.keyboard.type(String(text)) },
    press: (key) => page.keyboard.press(key),
    waitFor: (sel, ms = 10000) => page.locator(sel).first().waitFor({ state: 'visible', timeout: ms }),
    waitText: (re, ms = 10000) => page.getByText(re).first().waitFor({ state: 'visible', timeout: ms }),
    gone: (sel, ms = 10000) => page.locator(sel).first().waitFor({ state: 'hidden', timeout: ms }).catch(() =>
      page.locator(sel).first().waitFor({ state: 'detached', timeout: ms })),
    exists: async (sel) => (await page.locator(sel).count()) > 0 && await page.locator(sel).first().isVisible().catch(() => false),
    count: (sel) => page.locator(sel).count(),
    text: () => page.locator('body').innerText(),
    async shot(name) {
      const f = path.join(__dirname, '..', 'out', `${scenarioId}-${++shotN}-${name}.png`)
      await page.screenshot({ path: f }).catch(() => {})
      return f
    },
    assert(cond, msg) { if (!cond) throw new Error(`assert failed: ${msg}`) },
    async assertText(re, msg) {
      const t = await page.locator('body').innerText()
      if (!re.test(t)) throw new Error(`assertText failed: ${msg || re} (not found in page)`)
    },
    sleep: (ms) => page.waitForTimeout(ms),
    async spawnTarget(kind = 'cpu') {
      const code = TARGETS[kind] || TARGETS.cpu
      const proc = spawn(PY, ['-c', code], { stdio: 'ignore', detached: false })
      spawned.push(proc)
      await new Promise((r) => setTimeout(r, 700)) // let it get scheduled
      return proc.pid
    },
    _dispose() { for (const p of spawned) { try { p.kill('SIGKILL') } catch { /* */ } } },
  }
  return ctx
}

module.exports = { makeCtx }
