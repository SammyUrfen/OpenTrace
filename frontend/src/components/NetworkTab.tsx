import { useRunResource } from '../state/useRunResource'

export interface NetConn {
  family: string
  address: string | null
  port: number | null
  result: string
  latency_ms: number | null
  pid: number
}

function dest(c: NetConn): string {
  if (c.family === 'AF_UNIX') return c.address ?? '<unix socket>'
  const a = c.address ?? '?'
  return c.port != null ? `${a}:${c.port}` : a
}

function resultClass(result: string): string {
  if (result === 'ok') return 'net-ok'
  if (result === 'connecting') return 'net-pending'
  return 'net-fail' // any errno or "timed out"
}

export function NetworkTable({ rows }: { rows: NetConn[] }) {
  return (
    <table className="syscall-table" data-testid="network-table">
      <thead>
        <tr>
          <th>destination</th>
          <th>family</th>
          <th>result</th>
          <th className="num">latency</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c, i) => (
          <tr key={`${dest(c)}-${i}`}>
            <td className="syscall-name">{dest(c)}</td>
            <td>{c.family.replace('AF_', '')}</td>
            <td className={resultClass(c.result)}>{c.result}</td>
            <td className="num">
              {c.latency_ms == null
                ? '—'
                : c.latency_ms >= 1000
                  ? `${(c.latency_ms / 1000).toFixed(2)}s`
                  : `${c.latency_ms.toFixed(1)}ms`}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function NetworkTab({ backendUrl, runId }: { backendUrl: string; runId: string }) {
  const { rows, loading } = useRunResource<NetConn>(backendUrl, runId, 'network')
  const failed = rows.filter((c) => c.result !== 'ok' && c.result !== 'connecting').length
  const hosts = new Set(rows.map((c) => c.address)).size

  return (
    <div className="overview" data-testid="network-tab">
      <h3 className="overview__h">
        Network — {rows.length} connection{rows.length === 1 ? '' : 's'}
        {hosts > 0 && <> · {hosts} host{hosts === 1 ? '' : 's'}</>}
        {failed > 0 && <span className="net-fail"> · {failed} failed</span>}
      </h3>
      {loading && rows.length === 0 ? (
        <div className="overview__muted">Aggregating…</div>
      ) : rows.length === 0 ? (
        <div className="overview__muted">No outbound connections recorded for this run.</div>
      ) : (
        <>
          <NetworkTable rows={rows} />
          <p className="chart-caption">
            Parsed from <code>connect()</code> syscalls. DNS lookups
            (<code>getaddrinfo</code>) are a libc call invisible to strace — they
            need ltrace (Phase 6).
          </p>
        </>
      )}
    </div>
  )
}
