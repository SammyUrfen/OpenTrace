import { useState } from 'react'
import type { Project, Run } from '../state/useOpenTrace'
import {
  formatDuration,
  formatTime,
  severityColor,
  statusClass,
  statusLabel,
} from '../state/format'

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
}: {
  run: Run
  active: boolean
  onSelect?: (r: Run) => void
  onContextMenu?: (e: React.MouseEvent, r: Run) => void
}) {
  return (
    <button
      type="button"
      className={`run-row ${active ? 'run-row--active' : ''}`}
      onClick={() => onSelect?.(run)}
      onContextMenu={(e) => onContextMenu?.(e, run)}
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
  const runsByProject = new Map<string, Run[]>()
  for (const r of runs) {
    const list = runsByProject.get(r.session_id) ?? []
    list.push(r)
    runsByProject.set(r.session_id, list)
  }

  const openMenu = (e: React.MouseEvent, run: Run) => {
    e.preventDefault()
    setMenu({ run, x: e.clientX, y: e.clientY, mode: 'main' })
  }
  const closeMenu = () => setMenu(null)
  const doDelete = (run: Run) => {
    closeMenu()
    if (
      window.confirm(
        `Delete run "${run.label ?? run.display_name}"? This permanently removes its data and cannot be undone.`,
      )
    ) {
      onDeleteRun?.(run)
    }
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
        <span className="session-list__hint" title="Create from the menu (File ▸ New Session) or ⌘/Ctrl+K">
          ⌘K
        </span>
      </div>
      <div className="session-list__body">
        {projects.length === 0 && (
          <div className="session-list__empty">No sessions yet</div>
        )}
        {projects.map((p) => {
          const projectRuns = runsByProject.get(p.id) ?? []
          const isActive = p.id === activeSessionId
          return (
            <div key={p.id} className="project-group">
              <button
                type="button"
                className={`project-group__header ${isActive ? 'project-group__header--active' : ''}`}
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
              {projectRuns.length === 0 && (
                <div className="project-group__empty">no runs yet</div>
              )}
              {projectRuns.map((r) => (
                <RunRow
                  key={r.id}
                  run={r}
                  active={r.id === activeRunId}
                  onSelect={onSelectRun}
                  onContextMenu={openMenu}
                />
              ))}
            </div>
          )
        })}
      </div>

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
