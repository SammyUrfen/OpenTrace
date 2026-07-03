/**
 * Process handling for the embedded terminal.
 *
 * Owns the node-pty session that backs the renderer's xterm. Exposes a small
 * IPC surface to main.js; nothing in here knows about Electron windows beyond
 * the WebContents handle passed in at start().
 *
 * Tracing ON/OFF is a single env var, `OPENTRACE_ENABLE_STRACE`, that the
 * shell hook reads on every command. ON rewrites a simple foreground command to
 * `otrace -- <cmd>` (zsh) or enables the `ot` helper (bash); OFF is a plain
 * shell with zero overhead. The shell itself is never restarted.
 */
const pty = require('node-pty')
const path = require('path')
const os = require('os')
const fs = require('fs')

const HOOKS_DIR = path.join(__dirname, 'shell-hooks')
const OTRACE_PATH = path.join(HOOKS_DIR, 'otrace')
const DEFAULT_BACKEND_URL = 'http://localhost:8000'

let session = null
let tracingEnabled = false
// Live tracing/session state lives in a small file the shell hook sources each
// command — NOT typed into the shell — so `export OPENTRACE_*` lines never echo
// into the terminal. `null` until a pty is started.
let rtFile = null
let activeSessionId = null

function writeRuntime() {
  if (!rtFile) return
  let body = `export OPENTRACE_ENABLE_STRACE=${tracingEnabled ? '1' : '0'}\n`
  if (activeSessionId) body += `export OPENTRACE_SESSION=${activeSessionId}\n`
  try {
    fs.writeFileSync(rtFile, body)
  } catch {
    // best-effort; the hook falls back to env vars if the file is missing
  }
}

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

/**
 * Start a pty in the given cwd and stream its output to the given WebContents.
 * Idempotent for a given WebContents — calling again disposes the previous
 * session first. Returns a small info object the caller can hand to a session
 * record.
 */
function start({ webContents, cwd, cols = 80, rows = 24, backendUrl }) {
  if (session) dispose()

  const shell = defaultShell()
  const resolvedCwd = cwd || process.cwd()
  const type = shellType(shell)

  // Per-pty runtime-state file the hook sources (toggling tracing / switching
  // session updates this file, not the shell's input, so nothing echoes).
  rtFile = path.join(os.tmpdir(), `opentrace-rt-${process.pid}-${Date.now()}`)
  writeRuntime()

  // The hook reads these on source / on each command. Setting them in the spawn
  // env means the very first command already sees the right tracing state.
  // Prepending the hooks dir to PATH lets the zsh widget rewrite a traced
  // command to a short `otrace -- <cmd>` instead of an absolute path.
  const env = {
    ...process.env,
    PATH: `${HOOKS_DIR}${path.delimiter}${process.env.PATH || ''}`,
    OPENTRACE_API: backendUrl || process.env.OPENTRACE_API || DEFAULT_BACKEND_URL,
    OPENTRACE_OTRACE: OTRACE_PATH,
    OPENTRACE_RT: rtFile,
    OPENTRACE_ENABLE_STRACE: tracingEnabled ? '1' : '0',
  }

  const proc = pty.spawn(shell, ['-l'], {
    name: 'xterm-256color',
    cols,
    rows,
    cwd: resolvedCwd,
    env,
  })

  const hookPath = path.join(
    HOOKS_DIR,
    type === 'zsh' ? 'opentrace-hook.zsh' : 'opentrace-hook.sh',
  )

  // Source the hook once the shell has drawn its first prompt. Leading space so
  // it stays out of history when HIST_IGNORE_SPACE is set.
  setTimeout(() => {
    proc.write(` source "${hookPath}"\r`)
  }, 350)

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
    pid: session.proc.pid,
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
  if (rtFile) {
    try {
      fs.unlinkSync(rtFile)
    } catch {
      // best-effort cleanup
    }
    rtFile = null
  }
}

function setTracing(enabled) {
  tracingEnabled = Boolean(enabled)
  // Update the file the hook sources (no echo). The hook picks it up before the
  // next command runs.
  writeRuntime()

  if (session && !session.webContents.isDestroyed()) {
    session.webContents.send(
      'pty:data',
      banner(`tracing ${tracingEnabled ? 'enabled' : 'disabled'}`),
    )
  }
}

function write(data) {
  if (session) session.proc.write(data)
}

// Point the shell at a different OpenTrace session (project) so subsequent
// traced runs attach there — written to the runtime file, never typed.
function setSessionEnv(sessionId) {
  if (!sessionId) return
  activeSessionId = sessionId
  writeRuntime()
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
  setSessionEnv,
  isTracing,
  getInfo,
}
