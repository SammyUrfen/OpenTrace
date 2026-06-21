import { useEffect, useState } from 'react'
import './App.css'
import { MainTabs } from './components/MainTabs'
import { SecondaryTabs } from './components/SecondaryTabs'
import {
  MainContentPlaceholder,
  type BackendStatus,
} from './components/MainContentPlaceholder'
import { RunView, RUN_VIEWS } from './components/RunView'
import { RunSidebar } from './components/RunSidebar'
import { LiveMonitor } from './components/LiveMonitor'
import { Terminal } from './components/Terminal'
import { TracingToggle } from './components/TracingToggle'
import { useTracing } from './state/useTracing'
import { useOpenTrace } from './state/useOpenTrace'
import { useRunDetail } from './state/useRunDetail'

const BACKEND_URL =
  (typeof window !== 'undefined' && window.opentrace?.backendUrl) ||
  'http://localhost:8000'

function App() {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('connecting')
  const { enabled: tracing, setEnabled: setTracing, ready: tracingReady } = useTracing()
  const ot = useOpenTrace(BACKEND_URL)

  // Open run tabs + which one is focused + which analytics view.
  const [openRunIds, setOpenRunIds] = useState<string[]>([])
  const [focusedRunId, setFocusedRunId] = useState<string | null>(null)
  const [activeView, setActiveView] = useState('overview')

  const openRuns = openRunIds
    .map((id) => ot.runs.find((r) => r.id === id))
    .filter((r): r is NonNullable<typeof r> => Boolean(r))
  const focusedRun = ot.runs.find((r) => r.id === focusedRunId) ?? null
  const detail = useRunDetail(BACKEND_URL, focusedRunId, focusedRun?.status)
  const focusedLive = focusedRunId ? ot.live[focusedRunId] ?? null : null

  const openRun = (id: string) => {
    setOpenRunIds((prev) => (prev.includes(id) ? prev : [...prev, id]))
    setFocusedRunId(id)
    setActiveView('overview')
  }
  const closeRun = (id: string) => {
    setOpenRunIds((prev) => {
      const idx = prev.indexOf(id)
      const next = prev.filter((x) => x !== id)
      if (focusedRunId === id) {
        // Focus the right neighbour, or the left one if we closed the last tab.
        setFocusedRunId(next[idx] ?? next[idx - 1] ?? null)
      }
      return next
    })
  }

  // Prune open/focused tabs whose run no longer exists (deleted elsewhere), so
  // we never render a dangling tab or fetch detail for a 404'd run.
  useEffect(() => {
    const ids = new Set(ot.runs.map((r) => r.id))
    setOpenRunIds((prev) => {
      const next = prev.filter((id) => ids.has(id))
      return next.length === prev.length ? prev : next // keep ref if unchanged
    })
    if (focusedRunId && !ids.has(focusedRunId)) setFocusedRunId(null)
  }, [ot.runs, focusedRunId])

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
      <MainTabs
        openRuns={openRuns}
        activeRunId={focusedRunId}
        onSelect={setFocusedRunId}
        onClose={closeRun}
        rightSlot={
          <TracingToggle
            enabled={tracing}
            onChange={setTracing}
            disabled={!tracingReady}
          />
        }
      />
      {focusedRun ? (
        <SecondaryTabs views={RUN_VIEWS} active={activeView} onSelect={setActiveView} />
      ) : (
        <div className="region region--secondary-tabs" data-placeholder="secondary-tab-bar">
          <span className="region__label" />
        </div>
      )}
      {focusedRun ? (
        <RunView
          run={focusedRun}
          detail={detail}
          live={focusedLive}
          activeView={activeView}
          backendUrl={BACKEND_URL}
        />
      ) : (
        <MainContentPlaceholder backendStatus={backendStatus} />
      )}
      <div className="region region--sidebar">
        <RunSidebar
          projects={ot.projects}
          runs={ot.runs}
          connected={ot.connected}
          onSelectRun={(run) => openRun(run.id)}
        />
      </div>
      <div className="region region--bottom-panel" data-placeholder="bottom-panel">
        <div className="bottom-split">
          <div className="bottom-split__terminal">
            <Terminal
              onStart={() => {
                void ot.refresh()
              }}
              onExit={() => {
                void ot.refresh()
              }}
            />
          </div>
          <LiveMonitor
            activeRun={ot.runs.find((r) => r.id === ot.liveRunId) ?? null}
            live={ot.liveRunId ? ot.live[ot.liveRunId] ?? null : null}
            tracing={tracing}
          />
        </div>
      </div>
    </div>
  )
}

export default App
