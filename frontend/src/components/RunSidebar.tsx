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
  onSelectRun?: (run: Run) => void
}

function RunRow({ run, onSelect }: { run: Run; onSelect?: (r: Run) => void }) {
  return (
    <button
      type="button"
      className="run-row"
      onClick={() => onSelect?.(run)}
      title={run.command}
    >
      <span
        className="run-row__dot"
        style={{ background: severityColor(run.max_severity, run.status) }}
      />
      <span className="run-row__main">
        <span className="run-row__command">{run.command}</span>
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
export function RunSidebar({ projects, runs, connected, onSelectRun }: Props) {
  const runsByProject = new Map<string, Run[]>()
  for (const r of runs) {
    const list = runsByProject.get(r.session_id) ?? []
    list.push(r)
    runsByProject.set(r.session_id, list)
  }

  return (
    <div className="session-list">
      <div className="session-list__header">
        <span className="region__label">Sessions</span>
        <span className="session-list__count">
          <span
            className={`conn-dot ${connected ? 'conn-dot--on' : ''}`}
            title={connected ? 'live' : 'disconnected'}
          />
          {runs.length}
        </span>
      </div>
      <div className="session-list__body">
        {projects.length === 0 && (
          <div className="session-list__empty">No sessions yet</div>
        )}
        {projects.map((p) => {
          const projectRuns = runsByProject.get(p.id) ?? []
          return (
            <div key={p.id} className="project-group">
              <div className="project-group__header">
                <span className="project-group__name">{p.display_name}</span>
                <span className="project-group__count">{projectRuns.length}</span>
              </div>
              {projectRuns.length === 0 && (
                <div className="project-group__empty">no runs yet</div>
              )}
              {projectRuns.map((r) => (
                <RunRow key={r.id} run={r} onSelect={onSelectRun} />
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}
