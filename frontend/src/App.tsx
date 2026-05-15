import { useEffect, useRef, useState } from 'react'
import './App.css'
import { MainTabBarPlaceholder } from './components/MainTabBarPlaceholder'
import { SecondaryTabBarPlaceholder } from './components/SecondaryTabBarPlaceholder'
import {
  MainContentPlaceholder,
  type BackendStatus,
} from './components/MainContentPlaceholder'
import { SessionList } from './components/SessionList'
import { Terminal } from './components/Terminal'
import { TracingToggle } from './components/TracingToggle'
import { useTracing } from './state/useTracing'
import { useSessions } from './state/useSessions'

const BACKEND_URL =
  (typeof window !== 'undefined' && window.opentrace?.backendUrl) ||
  'http://localhost:8000'

function App() {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('connecting')
  const { enabled: tracing, setEnabled: setTracing, ready: tracingReady } = useTracing()
  const sessionsApi = useSessions(BACKEND_URL)
  // Track the current terminal's session id so we can PATCH it on exit.
  const currentSessionId = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const ping = () =>
      fetch(`${BACKEND_URL}/health`)
        .then((r) => {
          if (cancelled) return
          setBackendStatus(r.ok ? 'ok' : 'unreachable')
        })
        .catch(() => {
          if (!cancelled) setBackendStatus('unreachable')
        })
    ping()
    const id = setInterval(ping, 2000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return (
    <div className="app-shell">
      <MainTabBarPlaceholder
        rightSlot={
          <TracingToggle
            enabled={tracing}
            onChange={setTracing}
            disabled={!tracingReady}
          />
        }
      />
      <SecondaryTabBarPlaceholder />
      <MainContentPlaceholder backendStatus={backendStatus} />
      <div className="region region--sidebar">
        <SessionList
          sessions={sessionsApi.sessions}
          loading={sessionsApi.loading}
          error={sessionsApi.error}
        />
      </div>
      <div className="region region--bottom-panel" data-placeholder="bottom-panel">
        <Terminal
          onStart={async (info) => {
            const sess = await sessionsApi.create({
              command: info.shellName,
              cwd: info.cwd,
              process_name: info.shellName,
              tags: tracing ? ['tracing'] : undefined,
            })
            currentSessionId.current = sess?.id ?? null
          }}
          onExit={async (info) => {
            const id = currentSessionId.current
            if (!id) return
            await sessionsApi.update(id, {
              ended_at: Date.now(),
              exit_code: info.exitCode,
              exit_signal: info.signal ? String(info.signal) : undefined,
            })
            currentSessionId.current = null
          }}
        />
      </div>
    </div>
  )
}

export default App
