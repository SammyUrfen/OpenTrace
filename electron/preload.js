const { contextBridge, ipcRenderer } = require('electron')

// Extract --opentrace-backend-url=... from the additional args set in main.js.
function readBackendUrl() {
  const prefix = '--opentrace-backend-url='
  const arg = process.argv.find((a) => a.startsWith(prefix))
  return arg ? arg.slice(prefix.length) : 'http://localhost:8000'
}

// Subscribe to an ipcRenderer channel and return an unsubscribe function.
function subscribe(channel, cb) {
  const listener = (_event, payload) => cb(payload)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('opentrace', {
  backendUrl: readBackendUrl(),
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
})
