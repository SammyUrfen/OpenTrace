import { useRunResource } from '../state/useRunResource'
import type { Anomaly } from '../state/useRunDetail'

export interface OutputChunk {
  timestamp_ms: number
  stream: string // "stdout" | "stderr"
  text: string
}

function inAnomalyWindow(ts: number, anomalies: Anomaly[]): boolean {
  return anomalies.some(
    (a) =>
      a.first_seen_ms != null &&
      a.last_seen_ms != null &&
      ts >= a.first_seen_ms &&
      ts <= a.last_seen_ms,
  )
}

/** Pure renderer — exported for testing without a fetch. */
export function LogsView({
  chunks,
  anomalies,
}: {
  chunks: OutputChunk[]
  anomalies: Anomaly[]
}) {
  return (
    <pre className="logs" data-testid="logs-view">
      {chunks.map((c, i) => {
        const cls = [
          'logs__chunk',
          c.stream === 'stderr' ? 'logs__chunk--err' : '',
          inAnomalyWindow(c.timestamp_ms, anomalies) ? 'logs__chunk--anomaly' : '',
        ]
          .filter(Boolean)
          .join(' ')
        return (
          <span key={i} className={cls}>
            {c.text}
          </span>
        )
      })}
    </pre>
  )
}

interface Props {
  backendUrl: string
  runId: string
  anomalies: Anomaly[]
}

export function LogsTab({ backendUrl, runId, anomalies }: Props) {
  const { rows, loading } = useRunResource<OutputChunk>(backendUrl, runId, 'logs')
  const hasErr = rows.some((c) => c.stream === 'stderr')

  return (
    <div className="overview" data-testid="logs-tab">
      <h3 className="overview__h">
        Program output — {rows.length} write{rows.length === 1 ? '' : 's'}
        {hasErr && <span className="logs__err-label"> · includes stderr</span>}
      </h3>
      {loading && rows.length === 0 ? (
        <div className="overview__muted">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="overview__muted">
          No captured output. (Output is captured for runs traced with
          stdout/stderr write-logging.)
        </div>
      ) : (
        <>
          <LogsView chunks={rows} anomalies={anomalies} />
          <p className="chart-caption">
            stderr is tinted red; lines that occurred during an anomaly window
            have a left marker.
          </p>
        </>
      )}
    </div>
  )
}
