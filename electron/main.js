const { app, BrowserWindow, dialog, ipcMain, Menu } = require('electron')
const { spawn } = require('child_process')
const crypto = require('crypto')
const path = require('path')
const http = require('http')
const net = require('net')
const pty = require('./pty')

// Testing: expose the renderer over the Chrome DevTools Protocol so a CDP client
// (e.g. the Playwright MCP: `npx @playwright/mcp --cdp-endpoint http://127.0.0.1:PORT`)
// can drive the live window. Off unless OPENTRACE_REMOTE_DEBUG=<port> is set.
if (process.env.OPENTRACE_REMOTE_DEBUG) {
  app.commandLine.appendSwitch('remote-debugging-port', String(process.env.OPENTRACE_REMOTE_DEBUG))
  app.commandLine.appendSwitch('remote-allow-origins', '*')
}

const PREFERRED_BACKEND_PORT = 8000
// Point at an already-running backend (dev/testing) instead of spawning one.
const EXTERNAL_BACKEND = process.env.OPENTRACE_BACKEND_URL || null
// Resolved in app.whenReady() (port probe) before anything consumes them:
// createWindow() bakes BACKEND_URL into additionalArguments and the pty:start
// handler reads it per call, so late assignment is safe.
let BACKEND_PORT = PREFERRED_BACKEND_PORT
let BACKEND_URL = EXTERNAL_BACKEND || `http://localhost:${BACKEND_PORT}`
const FRONTEND_DEV_URL = 'http://localhost:5173'
const FRONTEND_DIST = path.resolve(__dirname, '..', 'frontend', 'dist', 'index.html')

// CWD where the user invoked `opentrace` / `npm start`. Captured eagerly so
// later directory changes inside the app don't affect the terminal's start dir.
const LAUNCH_CWD = process.env.OPENTRACE_LAUNCH_CWD || process.cwd()

// Random per-launch bearer token, generated only when we spawn our OWN backend
// child (below) — never for an external/reused one, which was never given it
// and would just 401 every request. Empty means the backend requires no auth,
// same as a manual `uvicorn` run or an isolated test backend.
let API_TOKEN = ''
let backendProcess = null
let backendSpawned = false // we own a child (vs external / reused backend)
let backendGaveUp = false // restart budget exhausted — backend is permanently down
let restartAttempts = 0
const MAX_RESTART_ATTEMPTS = 3
let appQuitting = false
let startupDone = false
let mainWin = null
// Last ~40 stderr lines from the backend child, for error dialogs/logs.
const stderrTail = []

function stderrSummary() {
  return stderrTail.slice(-15).join('\n')
}

function sendBackendStatus(payload) {
  if (mainWin && !mainWin.isDestroyed()) {
    mainWin.webContents.send('backend:status', payload)
  }
}

// True if the port is free to bind on localhost.
function probePort(port) {
  return new Promise((resolve) => {
    const srv = net.createServer()
    srv.once('error', () => resolve(false))
    srv.once('listening', () => srv.close(() => resolve(true)))
    srv.listen(port, '127.0.0.1')
  })
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.once('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port
      srv.close(() => resolve(port))
    })
  })
}

// Identity check for an already-listening server: /health's {"status":"ok"} is
// a generic idiom, so require OpenTrace-specific keys from /info instead.
function isOpenTraceBackend(url) {
  return new Promise((resolve) => {
    const req = http.get(`${url}/info`, (res) => {
      let body = ''
      res.on('data', (d) => { body += d })
      res.on('end', () => {
        try {
          const info = JSON.parse(body)
          resolve(res.statusCode === 200 && 'schema_version' in info && 'sessions_dir' in info)
        } catch {
          resolve(false)
        }
      })
    })
    req.on('error', () => resolve(false))
    req.setTimeout(2000, () => req.destroy())
  })
}

// Pick the backend port before spawning: keep the :8000 default when free (the
// external-terminal otrace/shell-hook fallback and docs assume it); if occupied
// by another OpenTrace backend (orphan from a crash, or the dev server), reuse
// it instead of spawning; otherwise fall back to an ephemeral free port.
// Returns true if a backend must be spawned.
async function resolveBackendPort() {
  if (await probePort(PREFERRED_BACKEND_PORT)) {
    BACKEND_PORT = PREFERRED_BACKEND_PORT
  } else if (await isOpenTraceBackend(`http://localhost:${PREFERRED_BACKEND_PORT}`)) {
    BACKEND_PORT = PREFERRED_BACKEND_PORT
    BACKEND_URL = `http://localhost:${BACKEND_PORT}`
    console.log(`[electron] reusing existing OpenTrace backend at ${BACKEND_URL}`)
    return false
  } else {
    BACKEND_PORT = await freePort()
    console.warn(
      `[electron] port ${PREFERRED_BACKEND_PORT} is occupied by a foreign server — using port ${BACKEND_PORT}`,
    )
  }
  BACKEND_URL = `http://localhost:${BACKEND_PORT}`
  return true
}

function startBackend() {
  const python = process.env.OPENTRACE_PYTHON || 'python3'
  const backendDir = path.resolve(__dirname, '..', 'backend')
  const startedAt = Date.now()
  let settled = false // 'error' (e.g. ENOENT) may fire without a matching 'exit'

  backendSpawned = true
  backendProcess = spawn(
    python,
    ['-m', 'uvicorn', 'app.main:app', '--port', String(BACKEND_PORT)],
    { cwd: backendDir, env: API_TOKEN ? { ...process.env, OPENTRACE_API_TOKEN: API_TOKEN } : process.env }
  )

  backendProcess.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`))
  backendProcess.stderr.on('data', (d) => {
    process.stderr.write(`[backend] ${d}`)
    for (const line of String(d).split('\n')) {
      if (line.trim()) stderrTail.push(line)
    }
    while (stderrTail.length > 40) stderrTail.shift()
  })
  backendProcess.on('error', (err) => {
    console.error(`[backend] spawn failed: ${err.message}`)
    stderrTail.push(`spawn failed: ${err.message}`)
    if (settled) return
    settled = true
    backendProcess = null
    scheduleRestartOrGiveUp(startedAt)
  })
  backendProcess.on('exit', (code, signal) => {
    console.log(`[backend] exited (code=${code}, signal=${signal})`)
    if (settled) return
    settled = true
    backendProcess = null
    scheduleRestartOrGiveUp(startedAt)
  })
}

function scheduleRestartOrGiveUp(startedAt) {
  if (appQuitting) return
  // A long healthy stretch resets the budget so week-long sessions never
  // permanently exhaust it.
  if (Date.now() - startedAt > 60000) restartAttempts = 0
  if (restartAttempts >= MAX_RESTART_ATTEMPTS) {
    backendGaveUp = true
    console.error(
      `[electron] backend failed permanently after ${MAX_RESTART_ATTEMPTS} restart attempts\n${stderrSummary()}`,
    )
    sendBackendStatus({ state: 'failed' })
    // During startup, the waitForBackend() catch in app.whenReady() owns the
    // dialog/quit so we don't show two dialogs.
    if (!startupDone) return
    if (process.env.OPENTRACE_SMOKE) {
      app.quit()
    } else {
      dialog.showErrorBox(
        'OpenTrace backend failed',
        `The backend on port ${BACKEND_PORT} exited and could not be restarted.\n\n${stderrSummary()}`,
      )
    }
    return
  }
  const delay = 1000 * 2 ** restartAttempts
  restartAttempts += 1
  console.log(`[backend] restarting in ${delay} ms (attempt ${restartAttempts}/${MAX_RESTART_ATTEMPTS})`)
  sendBackendStatus({ state: 'restarting', attempt: restartAttempts, max: MAX_RESTART_ATTEMPTS })
  setTimeout(() => {
    if (appQuitting) return
    startBackend()
    waitForBackend(15000)
      .then(() => {
        console.log(`[backend] restarted and healthy at ${BACKEND_URL}`)
        sendBackendStatus({ state: 'ok' })
      })
      .catch(() => {
        // the child's own exit handler drives the next restart attempt
      })
  }, delay)
}

function stopBackend() {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill('SIGTERM')
  }
}

// Poll /health until the backend responds OK or we time out (10 s). Fails fast
// when a spawned backend has exhausted its restart budget instead of polling
// a dead child for the full timeout.
function waitForBackend(timeoutMs = 10000) {
  const start = Date.now()
  return new Promise((resolve, reject) => {
    function attempt() {
      http.get(`${BACKEND_URL}/health`, (res) => {
        if (res.statusCode === 200) return resolve()
        retry()
      }).on('error', retry)
    }
    function retry() {
      if (backendSpawned && backendGaveUp) {
        return reject(new Error('backend process exited and could not be restarted'))
      }
      if (Date.now() - start >= timeoutMs) {
        return reject(new Error(`backend did not respond within ${timeoutMs} ms`))
      }
      setTimeout(attempt, 200)
    }
    attempt()
  })
}

function createWindow() {
  const { existsSync } = require('fs')
  const useDist = !process.env.OPENTRACE_DEV && existsSync(FRONTEND_DIST)

  const [winW, winH] = (process.env.OPENTRACE_WIN || '1280x800')
    .split('x')
    .map((n) => parseInt(n, 10) || 0)
  const win = new BrowserWindow({
    width: winW || 1280,
    height: winH || 800,
    title: 'OpenTrace',
    autoHideMenuBar: true, // native OS menu bar hidden; the app renders its own MenuBar
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      additionalArguments: [
        `--opentrace-backend-url=${BACKEND_URL}`,
        `--opentrace-api-token=${API_TOKEN}`,
      ],
    },
  })

  win.on('closed', () => pty.dispose())

  if (useDist) {
    win.loadFile(FRONTEND_DIST)
  } else {
    win.loadURL(FRONTEND_DEV_URL)
  }

  // Open DevTools in development
  if (process.env.OPENTRACE_DEV || process.env.DEBUG) {
    win.webContents.openDevTools()
  }

  // Smoke mode: render, screenshot ourselves, then quit. Used for automated
  // boot/render verification; a no-op for normal launches.
  if (process.env.OPENTRACE_SMOKE) {
    const outPath = process.env.OPENTRACE_SMOKE
    const delay = Number(process.env.OPENTRACE_SMOKE_DELAY || 4000)
    // comma-separated CSS selectors clicked in sequence (e.g. open run, switch tab)
    const clickSels = (process.env.OPENTRACE_SMOKE_CLICK || '')
      .split(',').map((s) => s.trim()).filter(Boolean)
    win.webContents.once('did-finish-load', () => {
      setTimeout(async () => {
        try {
          for (const sel of clickSels) {
            await win.webContents.executeJavaScript(
              `document.querySelector(${JSON.stringify(sel)})?.click()`,
            )
            await new Promise((r) => setTimeout(r, 1200)) // let fetch + render settle
          }
          if (process.env.OPENTRACE_SMOKE_JS) {
            await win.webContents.executeJavaScript(process.env.OPENTRACE_SMOKE_JS)
            await new Promise((r) => setTimeout(r, 600))
          }
          const img = await win.webContents.capturePage()
          require('fs').writeFileSync(outPath, img.toPNG())
          console.log(`[smoke] captured ${outPath}`)
        } catch (e) {
          console.error(`[smoke] capture failed: ${e.message}`)
        } finally {
          app.quit()
        }
      }, delay)
    })
  }
  return win
}

// Build the application menu. Custom items dispatch a string action to the
// renderer (window.opentrace.menu.onAction); native items use built-in roles.
function buildMenu(win) {
  const send = (action) => () => win.webContents.send('menu:action', action)
  const isMac = process.platform === 'darwin'
  const template = [
    {
      label: 'File',
      submenu: [
        { label: 'New Session', accelerator: 'CmdOrCtrl+N', click: send('new-session') },
        { type: 'separator' },
        { label: 'Settings…', accelerator: 'CmdOrCtrl+,', click: send('settings') },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' }, { role: 'redo' }, { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Command Palette…', accelerator: 'CmdOrCtrl+K', click: send('command-palette') },
        { type: 'separator' },
        { label: 'Toggle Sidebar', accelerator: 'CmdOrCtrl+B', click: send('toggle-sidebar') },
        { label: 'Toggle Terminal', accelerator: 'CmdOrCtrl+J', click: send('toggle-terminal') },
        { type: 'separator' },
        // Dev-only: the hidden menu's accelerators stay active, so a production
        // Ctrl+R would otherwise reload the renderer (killing the live shell)
        // instead of reaching the shell as reverse-i-search.
        ...(process.env.OPENTRACE_DEV || process.env.DEBUG
          ? [{ role: 'reload' }, { role: 'toggleDevTools' }, { type: 'separator' }]
          : []),
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Run',
      submenu: [
        { label: 'Toggle Tracing', accelerator: 'CmdOrCtrl+Shift+T', click: send('toggle-tracing') },
      ],
    },
    {
      label: 'Help',
      submenu: [
        { label: 'How to Use OpenTrace', click: send('guide') },
        { label: 'About OpenTrace', click: send('about') },
      ],
    },
  ]
  if (isMac) {
    template.unshift({ role: 'appMenu' })
  }
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

function registerIpc() {
  ipcMain.handle('pty:start', (event, opts = {}) => {
    const info = pty.start({
      webContents: event.sender,
      cwd: LAUNCH_CWD,
      cols: opts.cols,
      rows: opts.rows,
      backendUrl: BACKEND_URL,
      apiToken: API_TOKEN,
      // Mirror terminal scrollback under userData (respects OPENTRACE_USERDATA,
      // so e2e's throwaway profiles stay isolated) to restore it after a restart.
      scrollbackPath: path.join(app.getPath('userData'), 'terminal-scrollback.log'),
    })
    return { ...info, tracing: pty.isTracing() }
  })
  ipcMain.on('pty:write', (_event, data) => pty.write(data))
  ipcMain.on('pty:resize', (_event, { cols, rows }) => pty.resize(cols, rows))
  ipcMain.handle('tracing:set', (_event, enabled) => {
    pty.setTracing(enabled)
    return pty.isTracing()
  })
  ipcMain.handle('tracing:get', () => pty.isTracing())
  ipcMain.handle('session:set', (_event, id) => {
    pty.setSessionEnv(id)
    return true
  })
}

// Optional isolated profile (fresh localStorage) for testing the first-run flow.
if (process.env.OPENTRACE_USERDATA) {
  app.setPath('userData', process.env.OPENTRACE_USERDATA)
}

app.whenReady().then(async () => {
  registerIpc()
  if (!EXTERNAL_BACKEND) {
    const shouldSpawn = await resolveBackendPort()
    if (shouldSpawn) {
      API_TOKEN = crypto.randomBytes(32).toString('hex')
      startBackend()
    }
  }
  try {
    await waitForBackend()
    console.log(`[electron] backend ready at ${BACKEND_URL}`)
  } catch (err) {
    // A spawned child that died (vs a merely slow start, which self-heals via
    // the frontend's reconnecting EventSource) is fatal: explain and quit.
    if (backendSpawned && (backendGaveUp || backendProcess === null)) {
      console.error(`[electron] ${err.message}\n${stderrSummary()}`)
      if (!process.env.OPENTRACE_SMOKE) {
        dialog.showErrorBox(
          'OpenTrace backend failed to start',
          `The backend did not start on port ${BACKEND_PORT} (exit before /health succeeded).\n\n${stderrSummary()}`,
        )
      }
      app.quit()
      return
    }
    console.warn(`[electron] ${err.message} — opening window anyway`)
  }
  startupDone = true
  const win = createWindow()
  mainWin = win
  // Register the application menu for its keyboard accelerators (Ctrl+N/K/…),
  // but keep the native bar hidden: it does not render on KDE Plasma/Wayland, so
  // the app draws its own in-window MenuBar (which fires the same actions).
  buildMenu(win)
  win.setMenuBarVisibility(false)
})

app.on('window-all-closed', () => app.quit())
app.on('before-quit', () => {
  appQuitting = true
  pty.dispose()
  stopBackend()
})
