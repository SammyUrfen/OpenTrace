/**
 * Process handling for the embedded terminal.
 *
 * Owns the node-pty session that backs the renderer's xterm. Exposes a small
 * IPC surface to main.js; nothing in here knows about Electron windows beyond
 * the WebContents handle passed in at start().
 *
 * Tracing ON/OFF lives here because the wrapper hook point (if any) will
 * eventually wrap the pty process. For Phase 0 the toggle only writes a
 * banner into the terminal stream — the shell itself is untouched in both
 * states, so OFF is always a plain shell path.
 */
const pty = require('node-pty')
const path = require('path')
const fs = require('fs')
const os = require('os')


let session = null
let tracingEnabled = false
let lastStartOpts = null

function shellType(shellPath) {
  const base = path.basename(shellPath)
  if (base.includes('zsh')) return 'zsh'
  return 'bash'
}

function defaultShell() {
  if (process.platform === 'win32') {
    return process.env.COMSPEC || 'cmd.exe'
  }
  return process.env.SHELL || '/bin/bash'
}

function banner(text) {
  return `\r\n\x1b[36m[opentrace] ${text}\x1b[0m\r\n`
}

function tracesDir() {
  const home = process.env.OPENTRACE_HOME || path.join(os.homedir(), '.opentrace')
  const dir = path.join(home, 'traces')
  try{
    fs.mkdirSync(dir, { recursive: true , mode: 0o700 })
    // on systems that preserve modes, re-assert
    try { fs.chmodSync(dir, 0o700) } catch {}
  } catch(e) {
    // ignore: best-effort creation
  }
  return dir
}


/**
 * Start a pty in the given cwd and stream its output to the given WebContents.
 * Idempotent for a given WebContents — calling again disposes the previous
 * session first. Returns a small info object the caller can hand to a session
 * record.
 */
function start({ webContents, cwd, cols = 80, rows = 24 }) {
  if (session) dispose()

  const shell = defaultShell()
  const resolvedCwd = cwd || process.cwd()

  const proc = pty.spawn(shell, ['-l'], {
    name: 'xterm-256color',
    cols,
    rows,
    cwd: resolvedCwd,
    env: process.env,
  })

  const type = shellType(shell)

  const hookPath =
    type === 'zsh'
      ? path.join(__dirname, 'shell-hooks', 'opentrace-hook.zsh')
      : path.join(__dirname, 'shell-hooks', 'opentrace-hook.sh')

  setTimeout(() => {
    proc.write(`source "${hookPath}"\r`)
    proc.write(`export OPENTRACE_ENABLE_STRACE=0\r`)
    proc.write(`export OPENTRACE_ENABLE_PERF=0\r`)
  }, 300)


  proc.onData((data) => {
    if (webContents.isDestroyed()) return
    webContents.send('pty:data', data)
  })

  proc.onExit(({ exitCode, signal }) => {
    if (!webContents.isDestroyed()) {
      webContents.send('pty:exit', { exitCode, signal })
    }
    session = null
  })

  session = { proc, webContents, shell, cwd: resolvedCwd, cols, rows }

  return getInfo()
}

function getInfo() {
  if (!session) return null
  return {
    shell: session.shell,
    shellName: path.basename(session.shell),
    cwd: session.cwd,
    pid: session.proc.pid
  }
}

function resize(cols, rows) {
  if (session) {
    try {
      session.proc.resize(cols, rows)
    } catch {
      // node-pty throws if the pty has already exited — safe to ignore.
    }
  }
}

function dispose() {
  if (session) {
    try {
      session.proc.kill()
    } catch {
      // already dead
    }
    session = null
  }
}

function setTracing(enabled) {
  tracingEnabled = Boolean(enabled)

  if (!session) return

  if (tracingEnabled) {
    session.proc.write(`export OPENTRACE_ENABLE_STRACE=1\r`)
    session.proc.write(`export OPENTRACE_ENABLE_PERF=0\r`)
  } else {
    session.proc.write(`export OPENTRACE_ENABLE_STRACE=0\r`)
    session.proc.write(`export OPENTRACE_ENABLE_PERF=0\r`)
  }

  if (!session.webContents.isDestroyed()) {
    session.webContents.send(
      'pty:data',
      banner(`tracing ${tracingEnabled ? 'enabled' : 'disabled'}`)
    )
  }
}

function write(data) {
  if (session) session.proc.write(data)
}

function isTracing() {
  return tracingEnabled
}

module.exports = {
  start,
  write,
  resize,
  dispose,
  setTracing,
  isTracing,
  getInfo,
}
