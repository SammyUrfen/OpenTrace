const { app, BrowserWindow, ipcMain } = require('electron')
const { spawn } = require('child_process')
const path = require('path')
const http = require('http')
const pty = require('./pty')

const BACKEND_PORT = 8000
const BACKEND_URL = `http://localhost:${BACKEND_PORT}`
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

  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    title: 'OpenTrace',
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
  return win
}

function registerIpc() {
  ipcMain.handle('pty:start', (event, opts = {}) => {
    const info = pty.start({
      webContents: event.sender,
      cwd: LAUNCH_CWD,
      cols: opts.cols,
      rows: opts.rows,
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
}

app.whenReady().then(async () => {
  registerIpc()
  startBackend()
  try {
    await waitForBackend()
    console.log('[electron] backend ready')
  } catch (err) {
    console.warn(`[electron] ${err.message} — opening window anyway`)
  }
  createWindow()
})

app.on('window-all-closed', () => app.quit())
app.on('before-quit', () => {
  pty.dispose()
  stopBackend()
})
