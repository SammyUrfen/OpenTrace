import type { Session } from '../state/useSessions'

interface Props {
  sessions: Session[]
  loading: boolean
  error: string | null
}

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  })
}

function shortPath(cwd: string): string {
  const home = '/home/'
  if (cwd.startsWith(home)) {
    const rest = cwd.slice(home.length)
    const slash = rest.indexOf('/')
    return slash === -1 ? '~' : '~' + rest.slice(slash)
  }
  return cwd
}

function statusOf(s: Session): { label: string; cls: string } {
  if (s.ended_at == null) return { label: 'running', cls: 'running' }
  if (s.exit_code === 0) return { label: 'ok', cls: 'ok' }
  if (s.exit_signal) return { label: s.exit_signal, cls: 'fail' }
  return { label: `exit ${s.exit_code ?? '?'}`, cls: 'fail' }
}

export function SessionList({ sessions, loading, error }: Props) {
  return (
    <div className="session-list">
      <div className="session-list__header">
        <span className="region__label">Sessions</span>
        <span className="session-list__count">{sessions.length}</span>
      </div>
      <div className="session-list__body">
        {loading && <div className="session-list__empty">Loading…</div>}
        {!loading && error && (
          <div className="session-list__empty session-list__empty--error">
            {error}
          </div>
        )}
        {!loading && !error && sessions.length === 0 && (
          <div className="session-list__empty">No sessions yet</div>
        )}
        {sessions.map((s) => {
          const status = statusOf(s)
          return (
            <div key={s.id} className="session-row">
              <div className="session-row__top">
                <span className="session-row__command" title={s.command}>
                  {s.command}
                </span>
                <span
                  className={`session-row__status session-row__status--${status.cls}`}
                >
                  {status.label}
                </span>
              </div>
              <div className="session-row__meta">
                <span className="session-row__cwd" title={s.cwd}>
                  {shortPath(s.cwd)}
                </span>
                <span className="session-row__time">
                  {formatTime(s.started_at)}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
