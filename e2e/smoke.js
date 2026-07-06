/**
 * Harness smoke test: launch the real app isolated, dismiss onboarding, prove we
 * can read the DOM + click + screenshot, then report. Run: `node smoke.js`.
 */
const path = require('path')
const { launchApp } = require('./lib/harness')

async function main() {
  const t0 = Date.now()
  const app = await launchApp()
  const { page } = app
  const log = (m) => console.log(`[smoke ${((Date.now() - t0) / 1000).toFixed(1)}s] ${m}`)
  try {
    log(`launched, backend ${app.backendUrl}`)
    // React mounts a moment after domcontentloaded — wait for the app shell.
    await page.waitForSelector('body *', { timeout: 15000 })
    await page.waitForTimeout(1500)

    // Dismiss the first-run wizard if present (Continue = .ai-btn--primary, ~4 steps).
    for (let i = 0; i < 6; i++) {
      const btn = page.locator('.ai-btn--primary').first()
      if (await btn.count() && await btn.isVisible().catch(() => false)) {
        await btn.click().catch(() => {})
        await page.waitForTimeout(300)
      } else break
    }

    const title = await page.title().catch(() => '?')
    const bodyText = (await page.locator('body').innerText().catch(() => '')).slice(0, 400)
    log(`title=${JSON.stringify(title)}`)
    log(`visible text (first 400): ${JSON.stringify(bodyText)}`)

    // Prove we can find real UI: the sessions sidebar + the tracing toggle.
    const hasSessions = await page.getByText(/sessions/i).count().catch(() => 0)
    const hasToggle = await page.getByText(/OpenTrace (ON|OFF)/i).count().catch(() => 0)
    log(`found: sessions-panel=${hasSessions} tracing-toggle=${hasToggle}`)

    const shot = path.join(__dirname, 'out', 'smoke.png')
    await page.screenshot({ path: shot })
    log(`screenshot → ${shot}`)

    log(`console errors: ${app.consoleErrors.length} | page errors: ${app.pageErrors.length}`)
    for (const e of app.consoleErrors.slice(0, 8)) log(`  console: ${e.slice(0, 160)}`)
    for (const e of app.pageErrors.slice(0, 8)) log(`  pageerror: ${e.slice(0, 200)}`)

    const ok = hasSessions > 0 && hasToggle > 0
    log(ok ? 'RESULT: PASS — harness can drive the app' : 'RESULT: FAIL — expected UI not found')
    process.exitCode = ok ? 0 : 1
  } catch (e) {
    log(`ERROR: ${e && e.stack || e}`)
    process.exitCode = 2
  } finally {
    await app.cleanup()
  }
}

main()
