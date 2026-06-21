import { useEffect, useState } from 'react'
import './App.css'
import { MainTabs, type TabInfo } from './components/MainTabs'
import { SecondaryTabs } from './components/SecondaryTabs'
import {
  MainContentPlaceholder,
  type BackendStatus,
} from './components/MainContentPlaceholder'
import { RunView, RUN_VIEWS } from './components/RunView'
import { DiffView, DIFF_VIEWS } from './components/DiffView'
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
import { useTabs, tabKey } from './state/useTabs'
import { severityColor } from './state/format'
import { commandBasename } from './state/text'

const BACKEND_URL =
  (typeof window !== 'undefined' && window.opentrace?.backendUrl) ||
  'http://localhost:8000'

function App() {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('connecting')
  const { enabled: tracing, setEnabled: setTracing, ready: tracingReady } = useTracing()
  const { resolved: themeResolved, toggle: toggleTheme } = useTheme()
  const { collectors, toggle: toggleCollector } = useCollectors(BACKEND_URL)
  const ot = useOpenTrace(BACKEND_URL)
  const tabsApi = useTabs()
  const { tabs, activeKey, activeView, setActiveView, openRun, openDiff, select, close } = tabsApi

  const [settingsOpen, setSettingsOpen] = useState(false)
  const [onboarded, setOnboarded] = useState(
    () => localStorage.getItem('opentrace-onboarded') === '1',
  )
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  useEffect(() => {
    if (!activeSessionId && ot.projects.length > 0) {
      setActiveSessionId(ot.projects[0].id)
    }
  }, [ot.projects, activeSessionId])

  const selectSession = (id: string) => {
    setActiveSessionId(id)
    void window.opentrace?.session?.set(id)
  }
  const createSession = async (name: string) => {
    const proj = await ot.createSession(name)
    if (proj) selectSession(proj.id)
  }

  const focusedTab = tabs.find((t) => tabKey(t) === activeKey) ?? null
  const focusedRunId = focusedTab?.kind === 'run' ? focusedTab.runId : null
  const focusedRun = focusedRunId ? ot.runs.find((r) => r.id === focusedRunId) ?? null : null
  const detail = useRunDetail(BACKEND_URL, focusedRunId, focusedRun?.status)
  const focusedLive = focusedRunId ? ot.live[focusedRunId] ?? null : null

  // Finished runs auto-open as the focused tab (roadmap behaviour).
  useEffect(() => {
    if (ot.lastEnded) openRun(ot.lastEnded.id)
  }, [ot.lastEnded, openRun])

  // Prune tabs whose run(s) no longer exist; reconcile the active tab.
  useEffect(() => {
    const ids = new Set(ot.runs.map((r) => r.id))
    tabsApi.setTabs((prev) => {
      const next = prev.filter((t) =>
        t.kind === 'run' ? ids.has(t.runId) : ids.has(t.aId) && ids.has(t.bId),
      )
      return next.length === prev.length ? prev : next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ot.runs])
  useEffect(() => {
    if (activeKey && !tabs.some((t) => tabKey(t) === activeKey)) {
      tabsApi.setActiveKey(tabs.length ? tabKey(tabs[tabs.length - 1]) : null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs, activeKey])

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

  // Build the tab bar info (resolve run names / diff labels).
  const tabInfos: TabInfo[] = tabs.map((t) => {
    if (t.kind === 'run') {
      const r = ot.runs.find((x) => x.id === t.runId)
      return {
        key: tabKey(t),
        label: r?.display_name ?? t.runId.slice(0, 8),
        dotColor: r ? severityColor(r.max_severity, r.status) : undefined,
        title: r?.command,
      }
    }
    const a = ot.runs.find((x) => x.id === t.aId)
    const b = ot.runs.find((x) => x.id === t.bId)
    return {
      key: tabKey(t),
      label: `${commandBasename(a?.command ?? '?')} ↔ ${commandBasename(b?.command ?? '?')}`,
      diff: true,
      title: `Diff: ${a?.display_name ?? t.aId} ↔ ${b?.display_name ?? t.bId}`,
    }
  })

  const views = focusedTab?.kind === 'diff' ? DIFF_VIEWS : RUN_VIEWS

  return (
    <div className="app-shell">
      <MainTabs
        tabs={tabInfos}
        activeKey={activeKey}
        onSelect={select}
        onClose={close}
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
            <TracingToggle enabled={tracing} onChange={setTracing} disabled={!tracingReady} />
          </>
        }
      />
      {focusedTab ? (
        <SecondaryTabs views={views} active={activeView} onSelect={setActiveView} />
      ) : (
        <div className="region region--secondary-tabs" data-placeholder="secondary-tab-bar">
          <span className="region__label" />
        </div>
      )}
      {focusedTab?.kind === 'run' && focusedRun ? (
        <RunView
          run={focusedRun}
          detail={detail}
          live={focusedLive}
          activeView={activeView}
          backendUrl={BACKEND_URL}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      ) : focusedTab?.kind === 'diff' ? (
        <DiffView
          backendUrl={BACKEND_URL}
          aId={focusedTab.aId}
          bId={focusedTab.bId}
          runs={ot.runs}
          activeView={activeView}
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
          onCompareRuns={(a, b) => openDiff(a.id, b.id)}
          onSelectSession={(p) => selectSession(p.id)}
          onCreateSession={createSession}
        />
      </div>
      <div className="region region--bottom-panel" data-placeholder="bottom-panel">
        <div className="bottom-split">
          <div className="bottom-split__terminal">
            <Terminal onStart={() => void ot.refresh()} onExit={() => void ot.refresh()} />
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
