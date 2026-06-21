import type { LiveState, Run } from '../state/useOpenTrace'
import { formatBytesPerSec, formatDuration } from '../state/format'
import { Sparkline } from './Sparkline'

interface Props {
  activeRun: Run | null
  live: LiveState | null
  tracing: boolean
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
export function LiveMonitor({ activeRun, live, tracing }: Props) {
  const latest = live?.latest ?? null
  const running = activeRun?.status === 'running'

  return (
    <div className="live-monitor">
      <div className="live-monitor__header">
        <span className="region__label">Live Monitor</span>
        <span className={`live-dot ${running ? 'live-dot--on' : ''}`} />
      </div>

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
