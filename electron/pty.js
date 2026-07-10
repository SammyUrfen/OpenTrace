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

// --- scrollback persistence -------------------------------------------------
// A rolling, capped copy of the pty's output. Replayed into a freshly-mounted
// xterm (renderer reload / panel remount) so the shell's history isn't lost to a
// blank screen, and mirrored to disk so it survives a full app restart. The
// renderer needs no code for this — the replay flows through the same `pty:data`
// channel the xterm already reads.
const SCROLLBACK_CAP = 256 * 1024   // chars of output kept for restore
const PERSIST_DEBOUNCE_MS = 3000
let scrollbackChunks = []           // output chunks, oldest first
let scrollbackChars = 0
let scrollbackFile = null           // on-disk mirror (survives full restart)
let persistTimer = null

function pushScrollback(data) {
  scrollbackChunks.push(data)
  scrollbackChars += data.length
  while (scrollbackChars > SCROLLBACK_CAP && scrollbackChunks.length > 1) {
    scrollbackChars -= scrollbackChunks.shift().length
  }
  schedulePersist()
}

function getScrollback() {
  return scrollbackChunks.join('')
}

function seedScrollback(text) {
  scrollbackChunks = text ? [text] : []
  scrollbackChars = text ? text.length : 0
}

function schedulePersist() {
  if (persistTimer || !scrollbackFile) return
  persistTimer = setTimeout(() => { persistTimer = null; persistNow() }, PERSIST_DEBOUNCE_MS)
}

function persistNow() {
  if (persistTimer) { clearTimeout(persistTimer); persistTimer = null }
  if (!scrollbackFile) return
  try {
    fs.writeFileSync(scrollbackFile, getScrollback())
  } catch {
    // best-effort; scrollback restore is a convenience, never load-bearing
  }
}

function loadPersisted() {
  if (!scrollbackFile) return ''
  try {
    const buf = fs.readFileSync(scrollbackFile, 'utf8')
    return buf.length > SCROLLBACK_CAP ? buf.slice(buf.length - SCROLLBACK_CAP) : buf
  } catch {
    return ''  // no prior file (first run) or unreadable — start clean
  }
}

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
  // Exact basenames only (leading '-' marks a login shell). Substring checks
  // would misclassify fish: 'fish'.includes('sh') is true.
  const base = path.basename(shellPath).replace(/^-/, '')
  if (base === 'zsh') return 'zsh'
  if (base === 'bash' || base === 'sh' || base === 'dash') return 'bash'
  return 'unsupported'
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

// A live shell survives renderer reloads (Ctrl+R, dev StrictMode remounts):
// rebind the session to the (possibly new) WebContents, replay the saved
// scrollback so the reloaded (blank) xterm shows the full history — not just the
// current prompt a SIGWINCH jog would repaint — then apply the new geometry.
function reattach(webContents, cols, rows) {
  session.webContents = webContents
  session.cols = cols
  session.rows = rows
  const sb = getScrollback()
  if (sb && !webContents.isDestroyed()) webContents.send('pty:data', sb)
  resize(cols, rows)
  return getInfo()
}

/**
 * Start a pty in the given cwd and stream its output to the given WebContents.
 * Idempotent: if a live session exists it is reattached (shell and any running
 * command survive a renderer reload); the shell is only respawned when the
 * previous WebContents is gone (true window recreation). Returns a small info
 * object the caller can hand to a session record.
 */
function start({ webContents, cwd, cols = 80, rows = 24, backendUrl, apiToken, scrollbackPath }) {
  if (scrollbackPath && !scrollbackFile) scrollbackFile = scrollbackPath
  // A blank xterm that needs the full history replayed into it: a rebuilt window
  // (below) or a cold start that loads the disk mirror. A restart after the shell
  // exits reuses the SAME xterm — it still shows the history, so replaying would
  // duplicate every line; that path leaves `blankXterm` false.
  let blankXterm = false
  if (session) {
    if (!session.webContents.isDestroyed()) {
      return reattach(webContents, cols, rows)
    }
    dispose()          // window destroyed; buffer stays in memory
    blankXterm = true
  }

  // Cold start (fresh process, empty buffer): restore from the disk mirror so the
  // new shell's prompt appears below the history the user had last run.
  if (scrollbackChars === 0) {
    const prior = loadPersisted()
    if (prior) {
      seedScrollback(prior)
      blankXterm = true
    }
  }
  const restored = blankXterm ? getScrollback() : ''

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
    OPENTRACE_API_TOKEN: apiToken || process.env.OPENTRACE_API_TOKEN || '',
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
  // it stays out of history when HIST_IGNORE_SPACE is set. The hook is bash/zsh
  // syntax — sourcing it into fish/nushell/etc. would only spew parse errors,
  // so unsupported shells get a plain terminal and an explanatory notice.
  setTimeout(() => {
    if (type === 'unsupported') {
      sendToSession(
        proc,
        banner(`auto-tracing hooks support zsh/bash — terminal works, tracing disabled for ${path.basename(shell)}`),
      )
      return
    }
    proc.write(` source "${hookPath}"\r`)
  }, 350)

  // Read session.webContents at call time (not the start() parameter): a
  // renderer reload rebinds the session to a fresh handle via reattach(). The
  // proc identity check keeps a late exit of an already-disposed shell from
  // touching its replacement session. Every chunk is also mirrored into the
  // scrollback buffer for reload/restart restore.
  proc.onData((data) => {
    pushScrollback(data)
    sendToSession(proc, data, 'pty:data')
  })

  proc.onExit(({ exitCode, signal }) => {
    if (!session || session.proc !== proc) return
    sendToSession(proc, { exitCode, signal }, 'pty:exit')
    persistNow()
    session = null
  })

  session = { proc, webContents, shell, type, cwd: resolvedCwd, cols, rows }
  // Replay restored history first (a marker separates it from the fresh shell),
  // before the new shell's prompt streams in on the same channel.
  if (restored) {
    sendToSession(proc, restored + banner('restored from previous session'), 'pty:data')
  }
  return getInfo()
}

function sendToSession(proc, payload, channel = 'pty:data') {
  if (!session || session.proc !== proc) return
  if (session.webContents.isDestroyed()) return
  session.webContents.send(channel, payload)
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
  // Flush scrollback to disk so the next launch can restore it. Keep the buffer
  // in memory: dispose() fires from BOTH 'closed' and 'before-quit', so clearing
  // here would let the second call overwrite the good file with an empty buffer.
  persistNow()
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
    // Never claim a state the shell can't deliver: without the zsh/bash hook
    // the toggle has no effect.
    const text = session.type === 'unsupported'
      ? `auto-tracing hooks support zsh/bash — tracing unavailable for ${path.basename(session.shell)}`
      : `tracing ${tracingEnabled ? 'enabled' : 'disabled'}`
    session.webContents.send('pty:data', banner(text))
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
