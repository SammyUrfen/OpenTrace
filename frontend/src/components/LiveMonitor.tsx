import type { LiveAlert, LiveState, Run } from '../state/useOpenTrace'
import type { Collectors } from '../state/useCollectors'
import { SEVERITY_COLOR, formatBytesPerSec, formatDuration } from '../state/format'
import { Sparkline } from './Sparkline'

interface Props {
  activeRun: Run | null
  live: LiveState | null
  alerts: LiveAlert[]
  tracing: boolean
  collectors: Collectors | null
  onToggleCollector: (key: keyof Collectors) => void
}

const COLLECTOR_ROWS: {
  key: keyof Collectors
  label: string
  sub: string
  enabled: boolean
}[] = [
  { key: 'psutil', label: 'Resource metrics', sub: 'CPU · Memory · FDs', enabled: true },
  { key: 'strace', label: 'Syscall trace', sub: 'Syscalls · I/O · Network', enabled: true },
  { key: 'ltrace', label: 'Library calls', sub: 'malloc/free · hotspots', enabled: true },
  { key: 'perf', label: 'Hardware perf', sub: 'CPU flamegraph', enabled: true },
]

const COLLECTOR_HINTS: Partial<Record<keyof Collectors, string>> = {
  ltrace: 'Library + malloc/free tracing — uses ptrace, so it replaces Syscall trace',
  perf: 'Sample CPU call stacks for a flamegraph — most accurate with tracers off',
}

function Collectors({
  collectors,
  onToggle,
}: {
  collectors: Collectors | null
  onToggle: (key: keyof Collectors) => void
}) {
  return (
    <div className="collectors">
      <div className="collectors__title">Collectors</div>
      {COLLECTOR_ROWS.map((c) => {
        const on = collectors ? collectors[c.key] : false
        return (
          <label
            key={c.key}
            className={`collector ${c.enabled ? '' : 'collector--disabled'}`}
            title={COLLECTOR_HINTS[c.key] ?? ''}
          >
            <input
              type="checkbox"
              checked={on}
              disabled={!c.enabled || !collectors}
              onChange={() => onToggle(c.key)}
            />
            <span className="collector__text">
              <span className="collector__label">{c.label}</span>
              <span className="collector__sub">{c.sub}</span>
            </span>
          </label>
        )
      })}
    </div>
  )
}

function Metric({
  label,
  value,
  data,
  color,
}: {
  label: string
  value: string
  data: number[]
  color: string
}) {
  return (
    <div className="live-metric">
      <div className="live-metric__head">
        <span className="live-metric__label">{label}</span>
        <span className="live-metric__value">{value}</span>
      </div>
      <Sparkline data={data} color={color} width={150} height={26} />
    </div>
  )
}

/**
 * Right pane of the bottom panel. Shows live metrics for the currently running
 * trace (streamed over SSE) or an idle/last-value state otherwise.
 */
export function LiveMonitor({ activeRun, live, alerts, tracing, collectors, onToggleCollector }: Props) {
  const latest = live?.latest ?? null
  const running = activeRun?.status === 'running'

  return (
    <div className="live-monitor">
      <div className="live-monitor__header">
        <span className="region__label">Live Monitor</span>
        <span className={`live-dot ${running ? 'live-dot--on' : ''}`} />
      </div>

      <Collectors collectors={collectors} onToggle={onToggleCollector} />

      {alerts.length > 0 && (
        <div className="live-alerts">
          {alerts.map((a, i) => (
            <div key={i} className="live-alert" style={{ borderLeftColor: SEVERITY_COLOR[a.severity] }}>
              ⚠ {a.title}
            </div>
          ))}
        </div>
      )}

      {!activeRun && (
        <div className="live-monitor__idle">
          {tracing
            ? 'Tracing ON — run a command in the terminal'
            : 'Tracing OFF — toggle OpenTrace on to trace'}
        </div>
      )}

      {activeRun && (
        <div className="live-monitor__body">
          <div className="live-monitor__cmd" title={activeRun.command}>
            {activeRun.command}
          </div>
          <div className="live-monitor__sub">
            {activeRun.status}
            {activeRun.duration_ms != null && (
              <> · {formatDuration(activeRun.duration_ms)}</>
            )}
          </div>

          <Metric
            label="CPU"
            value={latest?.cpu_pct != null ? `${latest.cpu_pct.toFixed(0)}%` : '—'}
            data={live?.cpu ?? []}
            color="#60a5fa"
          />
          <Metric
            label="Memory"
            value={latest?.rss_mb != null ? `${latest.rss_mb.toFixed(0)} MB` : '—'}
            data={live?.rss ?? []}
            color="#c084fc"
          />
          <Metric
            label="Open FDs"
            value={latest?.open_fds != null ? String(latest.open_fds) : '—'}
            data={live?.fds ?? []}
            color="#34d399"
          />

          <div className="live-monitor__row">
            <span>Threads</span>
            <span>{latest?.threads ?? '—'}</span>
          </div>
          <div className="live-monitor__row">
            <span>Syscalls/s</span>
            <span>{latest?.syscall_rate != null ? latest.syscall_rate.toFixed(0) : '—'}</span>
          </div>
          <div className="live-monitor__row">
            <span>I/O read</span>
            <span>{formatBytesPerSec(latest?.io_read_bps ?? null)}</span>
          </div>
          <div className="live-monitor__row">
            <span>I/O write</span>
            <span>{formatBytesPerSec(latest?.io_write_bps ?? null)}</span>
          </div>
        </div>
      )}
    </div>
  )
}
