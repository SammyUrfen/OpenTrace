import type { LiveState, Run } from '../state/useOpenTrace'
import type { Anomaly, RunDetail } from '../state/useRunDetail'
import { SEVERITY_COLOR, formatDuration, formatTime, statusLabel } from '../state/format'
import { Sparkline } from './Sparkline'

interface Props {
  run: Run
  detail: RunDetail
  live: LiveState | null
}

function num(v: number | null | undefined, suffix = '', digits = 0): string {
  return v == null ? '—' : `${v.toFixed(digits)}${suffix}`
}

function StatCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat-cell">
      <div className="stat-cell__value">{value}</div>
      <div className="stat-cell__label">{label}</div>
    </div>
  )
}

function AnomalyCard({ a }: { a: Anomaly }) {
  const color = SEVERITY_COLOR[a.severity] ?? '#60a5fa'
  return (
    <div className="anomaly-card" style={{ borderLeftColor: color }}>
      <div className="anomaly-card__head">
        <span className="anomaly-card__sev" style={{ color }}>
          {a.severity.toUpperCase()}
        </span>
        <span className="anomaly-card__title">{a.title}</span>
        {a.occurrence_count > 1 && (
          <span className="anomaly-card__count">×{a.occurrence_count}</span>
        )}
      </div>
      <div className="anomaly-card__desc">{a.description}</div>
    </div>
  )
}

export function OverviewTab({ run, detail, live }: Props) {
  const { summary, metrics, anomalies, loading } = detail

  const cpuSeries = metrics.length
    ? metrics.map((m) => m.cpu_pct ?? 0)
    : (live?.cpu ?? [])
  const rssSeries = metrics.length
    ? metrics.map((m) => m.rss_mb ?? 0)
    : (live?.rss ?? [])

  const peakCpu = summary?.peaks?.cpu_pct ?? (cpuSeries.length ? Math.max(...cpuSeries) : null)
  const peakRss = summary?.peaks?.rss_mb ?? (rssSeries.length ? Math.max(...rssSeries) : null)
  const peakFds =
    summary?.peaks?.open_fds ??
    (metrics.length ? Math.max(...metrics.map((m) => m.open_fds ?? 0)) : null)
  const threads = summary?.peaks?.threads ?? null
  const syscalls = summary?.totals?.syscall_events ?? null
  const errors = summary?.totals?.errors ?? null
  const samples = summary?.totals?.metric_samples ?? metrics.length
  const sorted = [...anomalies].sort((a, b) => b.severity_score - a.severity_score)

  return (
    <div className="overview" data-testid="overview-tab">
      <div className="overview__header">
        <span className="overview__command" title={run.command}>
          {run.command}
        </span>
        <span className={`run-row__status run-row__status--${run.status === 'completed' && run.exit_code === 0 ? 'ok' : run.status === 'running' ? 'running' : 'fail'}`}>
          {statusLabel(run)}
        </span>
      </div>
      <div className="overview__sub">
        {formatTime(run.started_at)} · {formatDuration(run.duration_ms)} ·{' '}
        {run.cwd}
      </div>

      {/* AI summary placeholder (Phase 4) */}
      <div className="overview__ai">
        <span className="overview__ai-dot" /> AI summary — available once an LLM is
        configured (Phase 4)
      </div>

      {/* Anomalies */}
      <h3 className="overview__h">Top findings</h3>
      {loading && anomalies.length === 0 ? (
        <div className="overview__muted">Analyzing…</div>
      ) : sorted.length === 0 ? (
        <div className="overview__clean">✓ No anomalies detected — this run looks clean.</div>
      ) : (
        <div className="overview__anomalies">
          {sorted.map((a) => (
            <AnomalyCard key={a.id} a={a} />
          ))}
        </div>
      )}

      {/* Execution snapshot */}
      <h3 className="overview__h">Execution snapshot</h3>
      <div className="overview__sparks">
        <div className="overview__spark">
          <div className="overview__spark-label">CPU %</div>
          <Sparkline data={cpuSeries} color="#60a5fa" width={240} height={40} />
        </div>
        <div className="overview__spark">
          <div className="overview__spark-label">Memory MB</div>
          <Sparkline data={rssSeries} color="#c084fc" width={240} height={40} />
        </div>
      </div>
      <div className="stat-grid">
        <StatCell label="peak CPU" value={num(peakCpu, '%')} />
        <StatCell label="peak RSS" value={num(peakRss, ' MB')} />
        <StatCell label="peak FDs" value={num(peakFds)} />
        <StatCell label="threads" value={num(threads)} />
        <StatCell label="syscalls" value={syscalls == null ? '—' : syscalls.toLocaleString()} />
        <StatCell label="errors" value={num(errors)} />
        <StatCell label="samples" value={num(samples)} />
        <StatCell label="duration" value={formatDuration(run.duration_ms)} />
      </div>

      {summary?.totals?.top_syscalls && summary.totals.top_syscalls.length > 0 && (
        <>
          <h3 className="overview__h">Top syscalls</h3>
          <div className="top-syscalls">
            {summary.totals.top_syscalls.slice(0, 8).map(([name, count]) => (
              <div key={name} className="top-syscall">
                <span className="top-syscall__name">{name}</span>
                <span className="top-syscall__count">{count.toLocaleString()}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
