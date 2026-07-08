import { useRunResource } from '../state/useRunResource'
import { formatDuration } from '../state/format'
import { basename } from './textUtils'

export interface ProcRow {
  pid: number
  parent_pid: number | null
  command: string | null
  syscalls: number
  first_ms: number | null
  last_ms: number | null
  duration_ms: number | null
  exited: boolean
  ephemeral: boolean
}

export function ProcessTable({ rows }: { rows: ProcRow[] }) {
  return (
    <table className="syscall-table" data-testid="process-table">
      <thead>
        <tr>
          <th className="num">pid</th>
          <th>command</th>
          <th className="num">parent</th>
          <th className="num">syscalls</th>
          <th className="num">lifespan</th>
          <th>flags</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.pid}>
            <td className="num">{r.pid}</td>
            <td className="syscall-name" title={r.command ?? ''}>{basename(r.command)}</td>
            <td className="num">{r.parent_pid ?? '—'}</td>
            <td className="num">{r.syscalls.toLocaleString()}</td>
            <td className="num">{formatDuration(r.duration_ms)}</td>
            <td>
              {r.ephemeral && <span className="proc-flag proc-flag--eph" title="lived ≤250ms">⚡ ephemeral</span>}
              {r.exited && <span className="proc-flag" title="process exited during the run">exited</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function ProcessesTab({ backendUrl, runId }: { backendUrl: string; runId: string }) {
  const { rows, loading } = useRunResource<ProcRow>(backendUrl, runId, 'processes')
  const ephemeral = rows.filter((r) => r.ephemeral).length

  return (
    <div className="overview" data-testid="processes-tab">
      <h3 className="overview__h">
        Processes — {rows.length} process{rows.length === 1 ? '' : 'es'}
        {ephemeral > 0 && <span className="proc-eph-label"> · {ephemeral} ephemeral (⚡)</span>}
      </h3>
      {loading && rows.length === 0 ? (
        <div className="overview__muted">Aggregating…</div>
      ) : rows.length === 0 ? (
        <div className="overview__muted">No process data for this run.</div>
      ) : (
        <>
          <ProcessTable rows={rows} />
          <p className="chart-caption">
            Distilled from clone/fork/execve in the trace — including short-lived
            children the 250ms sampler may miss (⚡ ephemeral).
          </p>
        </>
      )}
    </div>
  )
}
