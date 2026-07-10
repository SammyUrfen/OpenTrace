const { contextBridge, ipcRenderer } = require('electron')

// Extract --opentrace-backend-url=... from the additional args set in main.js.
function readBackendUrl() {
  const prefix = '--opentrace-backend-url='
  const arg = process.argv.find((a) => a.startsWith(prefix))
  return arg ? arg.slice(prefix.length) : 'http://localhost:8000'
}

// Extract --opentrace-api-token=... (empty when main.js reused/pointed at an
// external backend rather than spawning + tokening its own).
function readApiToken() {
  const prefix = '--opentrace-api-token='
  const arg = process.argv.find((a) => a.startsWith(prefix))
  return arg ? arg.slice(prefix.length) : ''
}

// Subscribe to an ipcRenderer channel and return an unsubscribe function.
function subscribe(channel, cb) {
  const listener = (_event, payload) => cb(payload)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('opentrace', {
  backendUrl: readBackendUrl(),
  apiToken: readApiToken(),
  terminal: {
    start: (opts) => ipcRenderer.invoke('pty:start', opts || {}),
    write: (data) => ipcRenderer.send('pty:write', data),
    resize: (cols, rows) => ipcRenderer.send('pty:resize', { cols, rows }),
    onData: (cb) => subscribe('pty:data', cb),
    onExit: (cb) => subscribe('pty:exit', cb),
  },
  tracing: {
    set: (enabled) => ipcRenderer.invoke('tracing:set', enabled),
    get: () => ipcRenderer.invoke('tracing:get'),
  },
  session: {
    set: (id) => ipcRenderer.invoke('session:set', id),
  },
  // Backend lifecycle from the main process: {state: 'restarting'|'ok'|'failed',
  // attempt?, max?} — lets the renderer distinguish "crashed, restarting (n/3)"
  // from a generic SSE disconnect.
  backend: {
    onStatus: (cb) => subscribe('backend:status', cb),
  },
  // Native application-menu / keyboard-shortcut actions are delivered here as
  // string action names (e.g. 'new-session', 'settings', 'toggle-tracing').
  menu: {
    onAction: (cb) => subscribe('menu:action', cb),
  },
})
