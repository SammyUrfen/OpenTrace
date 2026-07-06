const { app, BrowserWindow, ipcMain, Menu } = require('electron')
const { spawn } = require('child_process')
const path = require('path')
const http = require('http')
const pty = require('./pty')

// Testing: expose the renderer over the Chrome DevTools Protocol so a CDP client
// (e.g. the Playwright MCP: `npx @playwright/mcp --cdp-endpoint http://127.0.0.1:PORT`)
// can drive the live window. Off unless OPENTRACE_REMOTE_DEBUG=<port> is set.
if (process.env.OPENTRACE_REMOTE_DEBUG) {
  app.commandLine.appendSwitch('remote-debugging-port', String(process.env.OPENTRACE_REMOTE_DEBUG))
  app.commandLine.appendSwitch('remote-allow-origins', '*')
}

const BACKEND_PORT = 8000
// Point at an already-running backend (dev/testing) instead of spawning one.
const EXTERNAL_BACKEND = process.env.OPENTRACE_BACKEND_URL || null
const BACKEND_URL = EXTERNAL_BACKEND || `http://localhost:${BACKEND_PORT}`
const FRONTEND_DEV_URL = 'http://localhost:5173'
const FRONTEND_DIST = path.resolve(__dirname, '..', 'frontend', 'dist', 'index.html')

// CWD where the user invoked `opentrace` / `npm start`. Captured eagerly so
// later directory changes inside the app don't affect the terminal's start dir.
const LAUNCH_CWD = process.env.OPENTRACE_LAUNCH_CWD || process.cwd()

let backendProcess = null

function startBackend() {
  const python = process.env.OPENTRACE_PYTHON || 'python'
  const backendDir = path.resolve(__dirname, '..', 'backend')

  backendProcess = spawn(
    python,
    ['-m', 'uvicorn', 'app.main:app', '--port', String(BACKEND_PORT)],
    { cwd: backendDir, env: process.env }
  )

  backendProcess.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`))
  backendProcess.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`))
  backendProcess.on('error', (err) => {
    console.error(`[backend] spawn failed: ${err.message}`)
  })
  backendProcess.on('exit', (code, signal) => {
    console.log(`[backend] exited (code=${code}, signal=${signal})`)
    backendProcess = null
  })
}

function stopBackend() {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill('SIGTERM')
  }
}

// Poll /health until the backend responds OK or we time out (10 s).
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
      additionalArguments: [`--opentrace-backend-url=${BACKEND_URL}`],
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
        { role: 'reload' }, { role: 'toggleDevTools' }, { type: 'separator' },
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
  if (!EXTERNAL_BACKEND) startBackend()
  try {
    await waitForBackend()
    console.log(`[electron] backend ready at ${BACKEND_URL}`)
  } catch (err) {
    console.warn(`[electron] ${err.message} — opening window anyway`)
  }
  const win = createWindow()
  // Register the application menu for its keyboard accelerators (Ctrl+N/K/…),
  // but keep the native bar hidden: it does not render on KDE Plasma/Wayland, so
  // the app draws its own in-window MenuBar (which fires the same actions).
  buildMenu(win)
  win.setMenuBarVisibility(false)
})

app.on('window-all-closed', () => app.quit())
app.on('before-quit', () => {
  pty.dispose()
  stopBackend()
})
