import type { Incident, LiveState, Run } from '../state/useOpenTrace'
import type { RunDetail } from '../state/useRunDetail'
import { SEVERITY_COLOR, formatDuration, formatTime, statusClass, statusLabel } from '../state/format'
import { maxOf } from './seriesUtils'
import { Sparkline } from './Sparkline'
import { StatCell } from './StatCell'
import { AiSummary } from './AiSummary'

interface Props {
  run: Run
  detail: RunDetail
  live: LiveState | null
  backendUrl: string
  onOpenSettings: () => void
  /** Live monitor incidents (SSE) — for a running monitor run, Top findings are
   *  derived from these so Overview agrees with the Incidents tab. */
  incidents?: Incident[]
}

/** The minimal shape the Top-findings card renders — satisfied by both a
 *  finalized `Anomaly` and a live `Incident` mapped into it. */
interface Finding {
  id: string
  severity: string
  title: string
  description: string
  occurrence_count: number
}

function num(v: number | null | undefined, suffix = '', digits = 0): string {
  return v == null ? '—' : `${v.toFixed(digits)}${suffix}`
}

function AnomalyCard({ a }: { a: Finding }) {
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

export function OverviewTab({ run, detail, live, backendUrl, onOpenSettings, incidents }: Props) {
  const { summary, metrics, anomalies, loading } = detail
  // A running monitor run finalizes no anomalies yet, but streams incidents — so
  // derive Top findings from the live incident store to match the Incidents tab.
  const liveMonitor = !!run.collector_config?.monitor && run.status === 'running'

  const cpuSeries = metrics.length
    ? metrics.map((m) => m.cpu_pct ?? 0)
    : (live?.cpu ?? [])
  const rssSeries = metrics.length
    ? metrics.map((m) => m.rss_mb ?? 0)
    : (live?.rss ?? [])

  // Loop-based peaks: these series are unbounded for monitor runs, so a spread
  // (Math.max(...arr)) would overflow the argument limit and crash the render.
  const peakCpu = summary?.peaks?.cpu_pct ?? maxOf(cpuSeries, (v) => v)
  const peakRss = summary?.peaks?.rss_mb ?? maxOf(rssSeries, (v) => v)
  const peakFds = summary?.peaks?.open_fds ?? maxOf(metrics, (m) => m.open_fds ?? 0)
  const threads = summary?.peaks?.threads ?? null
  const syscalls = summary?.totals?.syscall_events ?? null
  const errors = summary?.totals?.errors ?? null
  const samples = summary?.totals?.metric_samples ?? metrics.length
  const findings: Finding[] = liveMonitor
    ? (incidents ?? []).map((inc) => ({
        id: inc.id,
        severity: inc.severity,
        title: inc.title,
        description:
          inc.ai ??
          (inc.hot?.functions?.length
            ? `Hot path: ${inc.hot.functions.slice(0, 3).join(' → ')}`
            : 'Live incident captured during monitoring.'),
        occurrence_count: inc.count ?? 1,
      }))
    : [...anomalies].sort((a, b) => b.severity_score - a.severity_score)
  // On a finalized run "Analyzing…" bridges the gap before anomalies land; a live
  // monitor run has no such analyzing phase (incidents stream in), so skip it there.
  const findingsLoading = !liveMonitor && loading && anomalies.length === 0

  return (
    <div className="overview" data-testid="overview-tab">
      <div className="overview__header">
        <span className="overview__command" title={run.command}>
          {run.command}
        </span>
        <span className={`run-row__status run-row__status--${statusClass(run)}`}>
          {statusLabel(run)}
        </span>
      </div>
      <div className="overview__sub">
        {formatTime(run.started_at)} · {formatDuration(run.duration_ms)} ·{' '}
        {run.cwd}
      </div>

      <AiSummary
        backendUrl={backendUrl}
        runId={run.id}
        onOpenSettings={onOpenSettings}
      />

      {/* Anomalies */}
      <h3 className="overview__h">Top findings</h3>
      {findingsLoading ? (
        <div className="overview__muted">Analyzing…</div>
      ) : findings.length === 0 ? (
        liveMonitor ? (
          <div className="overview__muted">Monitoring — no incidents yet.</div>
        ) : (
          <div className="overview__clean">✓ No anomalies detected — this run looks clean.</div>
        )
      ) : (
        <div className="overview__anomalies">
          {findings.map((a) => (
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
