/**
 * Playwright-Electron harness for OpenTrace scenario tests.
 *
 * launchApp() spins up a FULLY ISOLATED instance: its own backend (spare port +
 * throwaway OPENTRACE_HOME) and its own Electron userData, so many can run in
 * parallel and none touch the user's live app on :8000. It returns the Playwright
 * `page` (the renderer) plus captured renderer console errors / page crashes /
 * backend stderr, and a cleanup() that tears everything down.
 */
const { _electron: electron } = require('playwright')
const { spawn } = require('child_process')
const http = require('http')
const os = require('os')
const path = require('path')
const fs = require('fs')

const ROOT = path.resolve(__dirname, '..', '..')
const BACKEND_DIR = path.join(ROOT, 'backend')
const ELECTRON_DIR = path.join(ROOT, 'electron')
const ELECTRON_BIN = path.join(ELECTRON_DIR, 'node_modules', 'electron', 'dist', 'electron')
const PY = process.env.OPENTRACE_PYTHON ||
  path.join(os.homedir(), 'miniconda3', 'envs', 'opentrace-dev', 'bin', 'python')

let portSeq = 8300 + Math.floor(process.pid % 400)

function nextPort() {
  return portSeq++
}

function waitForHealth(port, timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/health', timeout: 1000 }, (res) => {
        res.resume()
        if (res.statusCode === 200) return resolve()
        retry()
      })
      req.on('error', retry)
      req.on('timeout', () => { req.destroy(); retry() })
    }
    const retry = () => (Date.now() > deadline ? reject(new Error(`backend :${port} health timeout`)) : setTimeout(tick, 250))
    tick()
  })
}

async function launchApp(opts = {}) {
  const port = opts.port || nextPort()
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ot-e2e-home-'))
  const userdata = fs.mkdtempSync(path.join(os.tmpdir(), 'ot-e2e-udata-'))
  const backendLog = []

  // 1) isolated backend
  const backend = spawn(PY, ['-m', 'uvicorn', 'app.main:app', '--port', String(port), '--log-level', 'warning'], {
    cwd: BACKEND_DIR,
    env: { ...process.env, OPENTRACE_HOME: home, PATH: `${path.dirname(PY)}:${process.env.PATH}` },
  })
  backend.stderr.on('data', (d) => backendLog.push(d.toString()))
  backend.stdout.on('data', (d) => backendLog.push(d.toString()))
  await waitForHealth(port)

  // 2) Electron pointed at the isolated backend + its own userData (serves built dist/)
  const app = await electron.launch({
    executablePath: ELECTRON_BIN,
    args: [ELECTRON_DIR, `--user-data-dir=${userdata}`],
    cwd: ELECTRON_DIR,
    env: {
      ...process.env,
      OPENTRACE_BACKEND_URL: `http://127.0.0.1:${port}`,
      OPENTRACE_USERDATA: userdata,
      OPENTRACE_WIN: opts.win || '1280x820',
      OPENTRACE_PYTHON: PY,
      OPENTRACE_DEV: '', // force built dist/
    },
  })

  const page = await app.firstWindow()
  const consoleErrors = []
  const pageErrors = []
  page.on('dialog', (d) => d.accept().catch(() => {})) // auto-accept window.confirm (e.g. delete)
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
  page.on('pageerror', (e) => pageErrors.push(String(e && e.stack || e)))
  page.on('crash', () => pageErrors.push('RENDERER CRASH'))
  await page.waitForLoadState('domcontentloaded').catch(() => {})

  let cleaned = false
  const cleanup = async () => {
    if (cleaned) return
    cleaned = true
    try { await app.close() } catch { /* ignore */ }
    try { backend.kill('SIGTERM') } catch { /* ignore */ }
    for (const dir of [home, userdata]) {
      try { fs.rmSync(dir, { recursive: true, force: true }) } catch { /* ignore */ }
    }
  }

  return { app, page, port, home, userdata, backendUrl: `http://127.0.0.1:${port}`,
           consoleErrors, pageErrors, backendLog, cleanup }
}

module.exports = { launchApp, PY, ROOT }
