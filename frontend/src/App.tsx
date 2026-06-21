import { useCallback, useEffect, useState } from 'react'
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
import { SettingsModal } from './components/SettingsModal'
import { FirstRunWizard } from './components/FirstRunWizard'
import { useTracing } from './state/useTracing'
import { useOpenTrace } from './state/useOpenTrace'
import { useRunDetail } from './state/useRunDetail'
import { useTheme } from './state/useTheme'
import { useCollectors } from './state/useCollectors'

const BACKEND_URL =
  (typeof window !== 'undefined' && window.opentrace?.backendUrl) ||
  'http://localhost:8000'

function App() {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('connecting')
  const { enabled: tracing, setEnabled: setTracing, ready: tracingReady } = useTracing()
  const { resolved: themeResolved, toggle: toggleTheme } = useTheme()
  const { collectors, toggle: toggleCollector } = useCollectors(BACKEND_URL)
  const ot = useOpenTrace(BACKEND_URL)

  // Open run tabs + which one is focused + which analytics view.
  const [openRunIds, setOpenRunIds] = useState<string[]>([])
  const [focusedRunId, setFocusedRunId] = useState<string | null>(null)
  const [activeView, setActiveView] = useState('overview')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [onboarded, setOnboarded] = useState(
    () => localStorage.getItem('opentrace-onboarded') === '1',
  )
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  // Default the active session to the most recent project once loaded (the
  // terminal already attached to it, so we don't push it back to the shell).
  useEffect(() => {
    if (!activeSessionId && ot.projects.length > 0) {
      setActiveSessionId(ot.projects[0].id)
    }
  }, [ot.projects, activeSessionId])

  const selectSession = (id: string) => {
    setActiveSessionId(id)
    void window.opentrace?.session?.set(id) // new traced runs attach here
  }
  const createSession = async (name: string) => {
    const proj = await ot.createSession(name)
    if (proj) selectSession(proj.id)
  }

  const openRuns = openRunIds
    .map((id) => ot.runs.find((r) => r.id === id))
    .filter((r): r is NonNullable<typeof r> => Boolean(r))
  const focusedRun = ot.runs.find((r) => r.id === focusedRunId) ?? null
  const detail = useRunDetail(BACKEND_URL, focusedRunId, focusedRun?.status)
  const focusedLive = focusedRunId ? ot.live[focusedRunId] ?? null : null

  const openRun = useCallback((id: string) => {
    setOpenRunIds((prev) => (prev.includes(id) ? prev : [...prev, id]))
    setFocusedRunId(id)
    setActiveView('overview')
  }, [])

  // When a run finishes, open it as the focused tab so the result is never lost
  // ("where did my run go?"). Mirrors the roadmap: a tab opens on completion.
  useEffect(() => {
    if (ot.lastEnded) openRun(ot.lastEnded.id)
  }, [ot.lastEnded, openRun])
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
          <>
            <button
              type="button"
              className="icon-btn"
              title={`Switch to ${themeResolved === 'dark' ? 'light' : 'dark'} theme`}
              onClick={toggleTheme}
            >
              {themeResolved === 'dark' ? '☾' : '☀'}
            </button>
            <button
              type="button"
              className="icon-btn"
              title="Settings"
              onClick={() => setSettingsOpen(true)}
            >
              ⚙
            </button>
            <TracingToggle
              enabled={tracing}
              onChange={setTracing}
              disabled={!tracingReady}
            />
          </>
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
          onOpenSettings={() => setSettingsOpen(true)}
        />
      ) : (
        <MainContentPlaceholder backendStatus={backendStatus} />
      )}
      <div className="region region--sidebar">
        <RunSidebar
          projects={ot.projects}
          runs={ot.runs}
          connected={ot.connected}
          activeRunId={focusedRunId}
          activeSessionId={activeSessionId}
          onSelectRun={(run) => openRun(run.id)}
          onDeleteRun={(run) => void ot.deleteRun(run.id)}
          onSelectSession={(p) => selectSession(p.id)}
          onCreateSession={createSession}
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
            collectors={collectors}
            onToggleCollector={toggleCollector}
          />
        </div>
      </div>
      {settingsOpen && (
        <SettingsModal backendUrl={BACKEND_URL} onClose={() => setSettingsOpen(false)} />
      )}
      {!onboarded && (
        <FirstRunWizard
          backendUrl={BACKEND_URL}
          onDone={() => {
            localStorage.setItem('opentrace-onboarded', '1')
            setOnboarded(true)
          }}
        />
      )}
    </div>
  )
}

export default App
