import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import { MainTabs, type TabInfo } from './components/MainTabs'
import { SecondaryTabs } from './components/SecondaryTabs'
import {
  MainContentPlaceholder,
  type BackendStatus,
} from './components/MainContentPlaceholder'
import { RunView, runViews } from './components/RunView'
import { MenuBar, type MenuDef } from './components/MenuBar'
import { DiffView, DIFF_VIEWS } from './components/DiffView'
import { RunSidebar } from './components/RunSidebar'
import { LiveMonitor } from './components/LiveMonitor'
import { Terminal } from './components/Terminal'
import { TracingToggle } from './components/TracingToggle'
import { SettingsPage, type SettingsSection } from './components/SettingsPage'
import { CommandPalette, type Command } from './components/CommandPalette'
import { SessionModal } from './components/SessionModal'
import { AttachModal } from './components/AttachModal'
import { RunNameBar } from './components/RunNameBar'
import { FirstRunWizard } from './components/FirstRunWizard'
import { useTracing } from './state/useTracing'
import { useOpenTrace } from './state/useOpenTrace'
import { useRunDetail } from './state/useRunDetail'
import { useTheme } from './state/useTheme'
import { useCollectors } from './state/useCollectors'
import { useTabs, tabKey } from './state/useTabs'
import { useResizable } from './state/useResizable'
import { severityColor } from './state/format'
import { commandBasename } from './state/text'

const BACKEND_URL =
  (typeof window !== 'undefined' && window.opentrace?.backendUrl) ||
  'http://localhost:8000'

function App() {
  const { enabled: tracing, setEnabled: setTracing, ready: tracingReady } = useTracing()
  const { resolved: themeResolved, toggle: toggleTheme } = useTheme()
  const { collectors, toggle: toggleCollector } = useCollectors(BACKEND_URL)
  const ot = useOpenTrace(BACKEND_URL)
  const tabsApi = useTabs()
  const { tabs, activeKey, activeView, setActiveView, openRun, openDiff, select, close } = tabsApi

  const [settings, setSettings] = useState<{ section: SettingsSection } | null>(null)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [attachOpen, setAttachOpen] = useState(false)
  const [sessionModal, setSessionModal] = useState<
    { mode: 'create' } | { mode: 'rename'; id: string; name: string } | null
  >(null)
  const [runRename, setRunRename] = useState<{ id: string; name: string } | null>(null)
  // The just-finished run offered up for naming (non-blocking bar), and whether
  // that prompt is enabled at all (Settings ▸ General). `dismissedNames` tracks
  // runs already handled (saved / kept / navigated-away) so the bar never
  // re-pops for the same run.
  const [nameBarRunId, setNameBarRunId] = useState<string | null>(null)
  const dismissedNames = useRef<Set<string>>(new Set())
  const [namePrompt, setNamePrompt] = useState(
    () => localStorage.getItem('opentrace-name-prompt') !== '0',
  )
  const toggleNamePrompt = () =>
    setNamePrompt((v) => {
      const next = !v
      localStorage.setItem('opentrace-name-prompt', next ? '1' : '0')
      return next
    })
  const dismissNameBar = useCallback((id: string | null) => {
    if (id) dismissedNames.current.add(id)
    setNameBarRunId(null)
  }, [])
  const [sidebarHidden, setSidebarHidden] = useState(false)
  const [terminalHidden, setTerminalHidden] = useState(false)
  const [onboarded, setOnboarded] = useState(
    () => localStorage.getItem('opentrace-onboarded') === '1',
  )
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const sidebar = useResizable('ot-sidebar-w', 280, { axis: 'x', min: 200, max: 640, invert: true })
  const bottom = useResizable('ot-bottom-h', 280, { axis: 'y', min: 120, max: 620, invert: true })

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

  // One handler for both the in-app MenuBar and the native menu's accelerators.
  const handleMenuAction = useCallback((action: string) => {
    switch (action) {
      case 'new-session': setSessionModal({ mode: 'create' }); break
      case 'settings': setSettings({ section: 'general' }); break
      case 'guide': setSettings({ section: 'guide' }); break
      case 'about': setSettings({ section: 'about' }); break
      case 'command-palette': setPaletteOpen((v) => !v); break
      case 'attach-process': setAttachOpen(true); break
      case 'toggle-tracing': setTracing(!tracing); break
      case 'toggle-sidebar': setSidebarHidden((v) => !v); break
      case 'toggle-terminal': setTerminalHidden((v) => !v); break
      case 'toggle-theme': toggleTheme(); break
    }
  }, [tracing, setTracing, toggleTheme])

  // Native application-menu accelerators (Ctrl+N etc.) arrive over IPC — the
  // native menu bar itself is hidden (it doesn't render on KDE/Wayland); the
  // visible menu is the in-app MenuBar, which calls the same handler.
  useEffect(() => {
    const api = window.opentrace?.menu
    if (!api) return
    return api.onAction(handleMenuAction)
  }, [handleMenuAction])

  const MENUS: MenuDef[] = [
    { label: 'File', items: [
      { label: 'New Session', action: 'new-session', accel: 'Ctrl+N' },
      { separator: true },
      { label: 'Settings…', action: 'settings', accel: 'Ctrl+,' },
    ] },
    { label: 'View', items: [
      { label: 'Command Palette…', action: 'command-palette', accel: 'Ctrl+K' },
      { separator: true },
      { label: 'Toggle Sidebar', action: 'toggle-sidebar', accel: 'Ctrl+B' },
      { label: 'Toggle Terminal', action: 'toggle-terminal', accel: 'Ctrl+J' },
      { label: 'Toggle Theme', action: 'toggle-theme' },
    ] },
    { label: 'Run', items: [
      { label: tracing ? 'Turn Tracing Off' : 'Turn Tracing On', action: 'toggle-tracing', accel: 'Ctrl+Shift+T' },
      { separator: true },
      { label: 'Attach to running process…', action: 'attach-process' },
    ] },
    { label: 'Help', items: [
      { label: 'How to Use OpenTrace', action: 'guide' },
      { label: 'About OpenTrace', action: 'about' },
    ] },
  ]

  // Ctrl/Cmd+K opens the palette even without the native menu (dev/browser).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        setPaletteOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const focusedTab = tabs.find((t) => tabKey(t) === activeKey) ?? null
  const focusedRunId = focusedTab?.kind === 'run' ? focusedTab.runId : null
  const focusedRun = focusedRunId ? ot.runs.find((r) => r.id === focusedRunId) ?? null : null
  const detail = useRunDetail(BACKEND_URL, focusedRunId, focusedRun?.status)
  const focusedLive = focusedRunId ? ot.live[focusedRunId] ?? null : null

  // Finished runs auto-open as the focused tab (roadmap behaviour), and — unless
  // opted out — surface the non-blocking name prompt for the fresh run (once).
  useEffect(() => {
    if (!ot.lastEnded) return
    openRun(ot.lastEnded.id)
    if (namePrompt && !dismissedNames.current.has(ot.lastEnded.id)) {
      setNameBarRunId(ot.lastEnded.id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ot.lastEnded, openRun])

  // Navigating away from the run being prompted counts as dismissing it, so the
  // bar never re-pops when you come back to that run.
  useEffect(() => {
    if (nameBarRunId && focusedRunId !== nameBarRunId) dismissNameBar(nameBarRunId)
  }, [focusedRunId, nameBarRunId, dismissNameBar])

  const openRunRename = (id: string) => {
    const r = ot.runs.find((x) => x.id === id)
    if (r) setRunRename({ id, name: r.label ?? r.display_name })
  }

  // Prune tabs whose run(s) no longer exist; reconcile the active tab. Wait for
  // the first run-list load so restored tabs aren't wiped against an empty list.
  useEffect(() => {
    if (!ot.loaded) return
    const ids = new Set(ot.runs.map((r) => r.id))
    tabsApi.setTabs((prev) => {
      const next = prev.filter((t) =>
        t.kind === 'run' ? ids.has(t.runId) : ids.has(t.aId) && ids.has(t.bId),
      )
      return next.length === prev.length ? prev : next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ot.runs, ot.loaded])
  useEffect(() => {
    if (activeKey && !tabs.some((t) => tabKey(t) === activeKey)) {
      tabsApi.setActiveKey(tabs.length ? tabKey(tabs[tabs.length - 1]) : null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs, activeKey])

  // Backend reachability is derived from the always-open SSE connection
  // (`useOpenTrace`) rather than a separate /health poll — the stream IS the live
  // connection, so if it's open the backend is up. A prior successful load OR an
  // SSE error means we've been past the initial attempt, so a still-disconnected
  // state reads as "unreachable" rather than a perpetual "connecting" (which
  // would otherwise stick forever if the backend never comes up at cold start).
  const backendStatus: BackendStatus = ot.connected
    ? 'ok'
    : ot.loaded || ot.connectionError
      ? 'unreachable'
      : 'connecting'

  // Build the tab bar info (resolve run names / diff labels).
  const tabInfos: TabInfo[] = tabs.map((t) => {
    if (t.kind === 'run') {
      const r = ot.runs.find((x) => x.id === t.runId)
      return {
        key: tabKey(t),
        label: r?.label ?? r?.display_name ?? t.runId.slice(0, 8),
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
      title: `Diff: ${a?.label ?? a?.display_name ?? t.aId} ↔ ${b?.label ?? b?.display_name ?? t.bId}`,
    }
  })

  const views = focusedTab?.kind === 'diff' ? DIFF_VIEWS : runViews(focusedRun)

  // Command-palette entries: actions + every session + every run.
  const commands: Command[] = [
    { id: 'a-new-session', group: 'Action', label: 'New session', run: () => setSessionModal({ mode: 'create' }) },
    { id: 'a-attach', group: 'Action', label: 'Attach to running process', run: () => setAttachOpen(true) },
    { id: 'a-settings', group: 'Action', label: 'Open settings', run: () => setSettings({ section: 'general' }) },
    { id: 'a-tracing', group: 'Action', label: tracing ? 'Turn tracing OFF' : 'Turn tracing ON', run: () => setTracing(!tracing) },
    { id: 'a-theme', group: 'Action', label: 'Toggle theme', run: toggleTheme },
    { id: 'a-guide', group: 'Action', label: 'How to use OpenTrace', run: () => setSettings({ section: 'guide' }) },
    ...ot.projects.map((p) => ({
      id: `s-${p.id}`, group: 'Session', label: p.display_name, hint: 'switch',
      run: () => selectSession(p.id),
    })),
    ...ot.runs.map((r) => ({
      id: `r-${r.id}`, group: 'Run', label: r.label ?? r.command,
      hint: r.max_severity ?? r.status, run: () => openRun(r.id),
    })),
  ]

  return (
    <div
      className={`app-shell ${sidebarHidden ? 'app-shell--no-sidebar' : ''} ${terminalHidden ? 'app-shell--no-terminal' : ''}`}
      style={{
        gridTemplateColumns: `1fr ${sidebarHidden ? 0 : sidebar.val}px`,
        gridTemplateRows: `34px 40px 36px 1fr ${terminalHidden ? 0 : bottom.val}px`,
      }}
    >
      <div className="region region--menu-bar">
        <MenuBar menus={MENUS} onAction={handleMenuAction} />
        <div className="menu-bar__controls">
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
            onClick={() => setSettings({ section: 'general' })}
          >
            ⚙
          </button>
          <TracingToggle enabled={tracing} onChange={setTracing} disabled={!tracingReady} />
        </div>
      </div>
      <MainTabs
        tabs={tabInfos}
        activeKey={activeKey}
        onSelect={select}
        onClose={close}
        onRename={(key) => { if (key.startsWith('run:')) openRunRename(key.slice(4)) }}
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
          onOpenSettings={() => setSettings({ section: 'ai' })}
          incidents={ot.incidents[focusedRun.id] ?? []}
          onStopMonitor={() => void ot.stopMonitor(focusedRun.id)}
          topSlot={
            namePrompt && nameBarRunId === focusedRun.id ? (
              <RunNameBar
                key={focusedRun.id}
                run={focusedRun}
                onRename={(name) => void ot.renameRun(focusedRun.id, name)}
                onDismiss={() => dismissNameBar(focusedRun.id)}
              />
            ) : undefined
          }
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
        <div
          className="resize-handle resize-handle--x"
          onMouseDown={sidebar.onMouseDown}
          title="Drag to resize"
        />
        <RunSidebar
          projects={ot.projects}
          runs={ot.runs}
          connected={ot.connected}
          activeRunId={focusedRunId}
          activeSessionId={activeSessionId}
          onSelectRun={(run) => openRun(run.id)}
          onDeleteRun={(run) => void ot.deleteRun(run.id)}
          onRenameRun={(run) => setRunRename({ id: run.id, name: run.label ?? run.display_name })}
          onCompareRuns={(a, b) => openDiff(a.id, b.id)}
          onSelectSession={(p) => selectSession(p.id)}
          onRenameSession={(p) => setSessionModal({ mode: 'rename', id: p.id, name: p.display_name })}
        />
      </div>
      <div className="region region--bottom-panel" data-placeholder="bottom-panel">
        <div
          className="resize-handle resize-handle--y"
          onMouseDown={bottom.onMouseDown}
          title="Drag to resize"
        />
        <div className="bottom-split">
          <div className="bottom-split__terminal">
            <Terminal onStart={() => void ot.refresh()} onExit={() => void ot.refresh()} />
          </div>
          <LiveMonitor
            activeRun={ot.runs.find((r) => r.id === ot.liveRunId) ?? null}
            live={ot.liveRunId ? ot.live[ot.liveRunId] ?? null : null}
            alerts={ot.liveRunId ? ot.alerts[ot.liveRunId] ?? [] : []}
            tracing={tracing}
            collectors={collectors}
            onToggleCollector={toggleCollector}
          />
        </div>
      </div>
      <CommandPalette open={paletteOpen} commands={commands} onClose={() => setPaletteOpen(false)} />
      {settings && (
        <SettingsPage
          backendUrl={BACKEND_URL}
          initialSection={settings.section}
          onClose={() => setSettings(null)}
          themeResolved={themeResolved}
          onToggleTheme={toggleTheme}
          collectors={collectors}
          onToggleCollector={toggleCollector}
          namePrompt={namePrompt}
          onToggleNamePrompt={toggleNamePrompt}
        />
      )}
      {attachOpen && (
        <AttachModal
          backendUrl={BACKEND_URL}
          sessionId={activeSessionId}
          onClose={() => setAttachOpen(false)}
          onAttached={() => void ot.refresh()}
        />
      )}
      {runRename && (
        <SessionModal
          title="Rename run"
          submitLabel="Save"
          placeholder="Run name…"
          initial={runRename.name}
          onSubmit={(name) => void ot.renameRun(runRename.id, name)}
          onClose={() => setRunRename(null)}
        />
      )}
      {sessionModal && (
        <SessionModal
          title={sessionModal.mode === 'create' ? 'New session' : 'Rename session'}
          submitLabel={sessionModal.mode === 'create' ? 'Create' : 'Save'}
          initial={sessionModal.mode === 'rename' ? sessionModal.name : ''}
          onSubmit={(name) =>
            sessionModal.mode === 'create'
              ? void createSession(name)
              : void ot.renameSession(sessionModal.id, name)
          }
          onClose={() => setSessionModal(null)}
        />
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
