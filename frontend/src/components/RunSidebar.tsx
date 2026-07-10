import { useEffect, useState } from 'react'
import type { Project, Run } from '../state/useOpenTrace'
import {
  formatDuration,
  formatTime,
  severityColor,
  statusClass,
  statusLabel,
} from '../state/format'
import { ConfirmModal } from './ConfirmModal'

const COLLAPSE_KEY = 'opentrace.sidebar.collapsedSessions'
function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    return new Set(raw ? (JSON.parse(raw) as string[]) : [])
  } catch {
    return new Set()
  }
}
function saveCollapsed(s: Set<string>) {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...s])) } catch { /* best-effort */ }
}

interface Props {
  projects: Project[]
  runs: Run[]
  connected: boolean
  activeRunId?: string | null
  activeSessionId?: string | null
  onSelectRun?: (run: Run) => void
  onDeleteRun?: (run: Run) => void
  onRenameRun?: (run: Run) => void
  onCompareRuns?: (a: Run, b: Run) => void
  onSelectSession?: (project: Project) => void
  onRenameSession?: (project: Project) => void
}

interface MenuState {
  run: Run
  x: number
  y: number
  mode: 'main' | 'compare'
}

function RunRow({
  run,
  active,
  onSelect,
  onContextMenu,
  onDeleteKey,
}: {
  run: Run
  active: boolean
  onSelect?: (r: Run) => void
  onContextMenu?: (e: React.MouseEvent, r: Run) => void
  onDeleteKey?: (r: Run) => void
}) {
  return (
    <button
      type="button"
      className={`run-row ${active ? 'run-row--active' : ''}`}
      onClick={() => onSelect?.(run)}
      onContextMenu={(e) => onContextMenu?.(e, run)}
      onKeyDown={(e) => {
        // Delete/Backspace on a focused run row deletes it (same confirm flow as
        // the context menu). Scoped to this button's own focus — never a global
        // listener — so it can't ever eat a Backspace typed into the terminal.
        if (e.key === 'Delete' || e.key === 'Backspace') {
          e.preventDefault()
          onDeleteKey?.(run)
        }
      }}
      title={run.label ? `${run.label} — ${run.command}` : run.command}
    >
      <span
        className="run-row__dot"
        style={{ background: severityColor(run.max_severity, run.status) }}
      />
      <span className="run-row__main">
        {/* the run's name: a user-given label if renamed, else the command */}
        <span className="run-row__command">{run.label ?? run.command}</span>
        <span className="run-row__meta">
          {formatTime(run.started_at)} · {formatDuration(run.duration_ms)}
        </span>
      </span>
      <span className={`run-row__status run-row__status--${statusClass(run)}`}>
        {statusLabel(run)}
      </span>
    </button>
  )
}

/**
 * Right sidebar: projects (sessions), each expandable into its runs newest
 * first, with a severity dot per run. Mirrors the roadmap's Sessions section.
 */
export function RunSidebar({
  projects,
  runs,
  connected,
  activeRunId,
  activeSessionId,
  onSelectRun,
  onDeleteRun,
  onRenameRun,
  onCompareRuns,
  onSelectSession,
  onRenameSession,
}: Props) {
  const [menu, setMenu] = useState<MenuState | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<Run | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed)
  const runsByProject = new Map<string, Run[]>()
  for (const r of runs) {
    const list = runsByProject.get(r.session_id) ?? []
    list.push(r)
    runsByProject.set(r.session_id, list)
  }

  const openMenu = (e: React.MouseEvent, run: Run) => {
    e.preventDefault()
    // clamp into the viewport so the menu (esp. the last item, Delete) can't render
    // off the bottom/right edge when right-clicking a row near an edge
    const MW = 190, MH = 176
    const x = Math.max(8, Math.min(e.clientX, window.innerWidth - MW))
    const y = Math.max(8, Math.min(e.clientY, window.innerHeight - MH))
    setMenu({ run, x, y, mode: 'main' })
  }
  const closeMenu = () => setMenu(null)
  useEffect(() => {
    if (!menu) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeMenu() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menu])
  const doDelete = (run: Run) => {
    closeMenu()
    setConfirmDelete(run)
  }
  const toggleCollapsed = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      saveCollapsed(next)
      return next
    })
  }

  return (
    <div className="session-list">
      <div className="session-list__header">
        <span className="region__label">
          <span
            className={`conn-dot ${connected ? 'conn-dot--on' : ''}`}
            title={connected ? 'live' : 'disconnected'}
          />
          Sessions
        </span>
        <span className="session-list__hint" title="Create from the menu (File ▸ New Session) or Ctrl+K">
          Ctrl+K
        </span>
      </div>
      <div className="session-list__body">
        {projects.length === 0 && (
          <div className="session-list__empty">No sessions yet</div>
        )}
        {projects.map((p) => {
          const projectRuns = runsByProject.get(p.id) ?? []
          const isActive = p.id === activeSessionId
          const isCollapsed = collapsed.has(p.id)
          return (
            <div key={p.id} className="project-group">
              <div className={`project-group__header ${isActive ? 'project-group__header--active' : ''}`}>
                <button
                  type="button"
                  className={`project-group__toggle ${isCollapsed ? 'project-group__toggle--collapsed' : ''}`}
                  onClick={() => toggleCollapsed(p.id)}
                  aria-label={isCollapsed ? 'expand session' : 'collapse session'}
                  aria-expanded={!isCollapsed}
                  title={isCollapsed ? 'Expand' : 'Collapse'}
                >
                  ▾
                </button>
                <button
                  type="button"
                  className="project-group__select"
                  onClick={() => onSelectSession?.(p)}
                  onDoubleClick={() => onRenameSession?.(p)}
                  title={isActive ? 'Active — new runs go here (double-click to rename)' : 'Switch (double-click to rename)'}
                >
                  <span className="project-group__name">
                    {isActive && <span className="project-group__active-dot" />}
                    {p.display_name}
                  </span>
                  <span className="project-group__count">{projectRuns.length}</span>
                </button>
              </div>
              {!isCollapsed && projectRuns.length === 0 && (
                <div className="project-group__empty">no runs yet</div>
              )}
              {!isCollapsed && projectRuns.map((r) => (
                <RunRow
                  key={r.id}
                  run={r}
                  active={r.id === activeRunId}
                  onSelect={onSelectRun}
                  onContextMenu={openMenu}
                  onDeleteKey={doDelete}
                />
              ))}
            </div>
          )
        })}
      </div>

      {confirmDelete && (
        <ConfirmModal
          title="Delete run"
          body={`Delete run "${confirmDelete.label ?? confirmDelete.display_name}"? This permanently removes its data and cannot be undone.`}
          confirmLabel="Delete"
          danger
          onClose={() => setConfirmDelete(null)}
          onConfirm={() => { onDeleteRun?.(confirmDelete); setConfirmDelete(null) }}
        />
      )}

      {menu && (
        <>
          <div className="ctx-backdrop" onClick={closeMenu} onContextMenu={(e) => { e.preventDefault(); closeMenu() }} />
          <div className="ctx-menu" style={{ left: menu.x, top: menu.y }}>
            {menu.mode === 'main' ? (
              <>
                <button type="button" className="ctx-item" onClick={() => { closeMenu(); onSelectRun?.(menu.run) }}>
                  Open
                </button>
                <button type="button" className="ctx-item" onClick={() => { closeMenu(); onRenameRun?.(menu.run) }}>
                  Rename…
                </button>
                <button
                  type="button"
                  className="ctx-item"
                  disabled={runs.length < 2}
                  onClick={() => setMenu({ ...menu, mode: 'compare' })}
                >
                  Compare with… ▸
                </button>
                <button type="button" className="ctx-item ctx-item--danger" onClick={() => doDelete(menu.run)}>
                  Delete
                </button>
              </>
            ) : (
              <div className="ctx-submenu">
                <div className="ctx-submenu__head">Compare with…</div>
                {runs
                  .filter((r) => r.id !== menu.run.id)
                  .slice(0, 12)
                  .map((other) => (
                    <button
                      key={other.id}
                      type="button"
                      className="ctx-item"
                      onClick={() => { closeMenu(); onCompareRuns?.(menu.run, other) }}
                      title={other.command}
                    >
                      {other.label ?? other.display_name}
                    </button>
                  ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
