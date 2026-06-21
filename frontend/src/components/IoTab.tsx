import { useRunResource } from '../state/useRunResource'
import { formatBytes } from '../state/format'

export interface IoRow {
  path: string
  opens: number
  closes: number
  reads: number
  writes: number
  read_bytes: number
  write_bytes: number
  leaked: number
}

function basename(p: string): string {
  const i = p.lastIndexOf('/')
  return i >= 0 ? p.slice(i + 1) || p : p
}

interface Props {
  backendUrl: string
  runId: string
}

/** Pure table — exported so it can be tested without fetching. */
export function IoTable({ rows }: { rows: IoRow[] }) {
  const maxAccess = Math.max(...rows.map((r) => r.opens + r.reads + r.writes), 1)
  return (
    <table className="syscall-table" data-testid="io-table">
      <thead>
        <tr>
          <th>file</th>
          <th className="num">opens</th>
          <th className="num">reads</th>
          <th className="num">writes</th>
          <th className="num">read</th>
          <th className="num">written</th>
          <th className="num">access</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const access = r.opens + r.reads + r.writes
          return (
            <tr key={r.path}>
              <td className="syscall-name" title={r.path}>
                {r.leaked > 0 && (
                  <span className="io-leak" title={`${r.leaked} fd(s) never closed`}>⊘ </span>
                )}
                {basename(r.path)}
              </td>
              <td className="num">{r.opens.toLocaleString()}</td>
              <td className="num">{r.reads.toLocaleString()}</td>
              <td className="num">{r.writes.toLocaleString()}</td>
              <td className="num">{formatBytes(r.read_bytes)}</td>
              <td className="num">{formatBytes(r.write_bytes)}</td>
              <td className="num io-bar-cell">
                <span className="io-bar" style={{ width: `${(access / maxAccess) * 100}%` }} />
                <span className="io-bar-num">{access.toLocaleString()}</span>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

export function IoTab({ backendUrl, runId }: Props) {
  const { rows, loading } = useRunResource<IoRow>(backendUrl, runId, 'io')
  const leakedCount = rows.filter((r) => r.leaked > 0).length

  return (
    <div className="overview" data-testid="io-tab">
      <h3 className="overview__h">
        File I/O — {rows.length} file{rows.length === 1 ? '' : 's'} touched
        {leakedCount > 0 && (
          <span className="io-leak"> · {leakedCount} with unclosed fds (⊘)</span>
        )}
      </h3>
      {loading && rows.length === 0 ? (
        <div className="overview__muted">Aggregating…</div>
      ) : rows.length === 0 ? (
        <div className="overview__muted">No file I/O recorded for this run.</div>
      ) : (
        <>
          <IoTable rows={rows} />
          <p className="chart-caption">
            ⊘ marks files whose descriptors were still open at exit. For
            short-lived programs this is normal (exit closes them); for long-running
            ones it can indicate a descriptor leak.
          </p>
        </>
      )}
    </div>
  )
}
