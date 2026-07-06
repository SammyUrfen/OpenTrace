/**
 * Scenario runner. Launches ONE isolated app and runs a set of scenarios in it,
 * recording per-scenario pass/fail + any renderer console errors / page crashes.
 *
 *   node run.js all
 *   node run.js tag:attach,settings
 *   node run.js id:attach-cpu-basic,session-create
 *
 * Writes out/results-<ts>.json and prints a summary. A scenario "passes" only if
 * it neither throws nor triggers an uncaught renderer error / crash.
 */
const fs = require('fs')
const path = require('path')
const { launchApp } = require('./lib/harness')
const { makeCtx } = require('./lib/driver')
const registry = require('./scenarios')

function select(arg) {
  if (!arg || arg === 'all') return registry
  if (arg.startsWith('tag:')) {
    const tags = arg.slice(4).split(',')
    return registry.filter((s) => (s.tags || []).some((t) => tags.includes(t)))
  }
  if (arg.startsWith('id:')) {
    const ids = arg.slice(3).split(',')
    return registry.filter((s) => ids.includes(s.id))
  }
  if (arg.startsWith('file:')) {
    const files = arg.slice(5).split(',')
    return registry.filter((s) => files.includes(s.file))
  }
  if (arg.startsWith('path:')) {
    // run ONLY this file (bypass the full-dir registry) — for isolated exploratory
    // batches that shouldn't be affected by other in-flight scenario files.
    const rel = arg.slice(5)
    const mod = require(require('path').resolve(__dirname, rel))
    return (Array.isArray(mod) ? mod : mod.scenarios || []).map((s) => ({ file: rel, ...s }))
  }
  const ids = arg.split(',')
  return registry.filter((s) => ids.includes(s.id))
}

const withTimeout = (p, ms, id) => Promise.race([
  p,
  new Promise((_, rej) => setTimeout(() => rej(new Error(`scenario timeout after ${ms}ms`)), ms)),
])

async function main() {
  const scenarios = select(process.argv[2])
  if (!scenarios.length) { console.error('no scenarios matched', process.argv[2]); process.exit(2) }
  console.log(`running ${scenarios.length} scenario(s)`)

  const app = await launchApp()
  const results = []
  try {
    // one-time onboarding dismissal
    await makeCtx(app, 'boot').dismissOnboarding()
    for (const sc of scenarios) {
      const ceBefore = app.consoleErrors.length
      const peBefore = app.pageErrors.length
      const ctx = makeCtx(app, sc.id)
      const t0 = Date.now()
      let status = 'pass', error = null
      try {
        await withTimeout(Promise.resolve(sc.run(ctx)), sc.timeout || 45000, sc.id)
      } catch (e) {
        status = 'fail'; error = String((e && e.message) || e)
      } finally {
        ctx._dispose()
        // return to a neutral state for the next scenario
        await app.page.keyboard.press('Escape').catch(() => {})
        await app.page.waitForTimeout(200)
      }
      const consoleErrors = app.consoleErrors.slice(ceBefore)
      const pageErrors = app.pageErrors.slice(peBefore)
      if (pageErrors.length && status === 'pass') status = 'error' // crashed even though steps passed
      const r = { id: sc.id, name: sc.name, tags: sc.tags || [], status, error,
                  durationMs: Date.now() - t0, consoleErrors, pageErrors }
      results.push(r)
      const mark = status === 'pass' ? '✓' : status === 'error' ? '‼' : '✗'
      console.log(`  ${mark} ${sc.id} (${r.durationMs}ms)${error ? ' — ' + error.slice(0, 120) : ''}` +
        `${pageErrors.length ? ` [${pageErrors.length} page-error]` : ''}` +
        `${consoleErrors.length ? ` [${consoleErrors.length} console-error]` : ''}`)
    }
  } finally {
    await app.cleanup()
  }

  const pass = results.filter((r) => r.status === 'pass').length
  const summary = { total: results.length, pass, fail: results.filter(r => r.status === 'fail').length,
                    error: results.filter(r => r.status === 'error').length, results }
  const out = path.join(__dirname, 'out', `results-${process.pid}.json`)
  fs.writeFileSync(out, JSON.stringify(summary, null, 2))
  console.log(`\n${pass}/${results.length} passed → ${out}`)
  process.exit(summary.fail + summary.error > 0 ? 1 : 0)
}

main().catch((e) => { console.error('runner crashed:', e); process.exit(3) })
