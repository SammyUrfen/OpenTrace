import { useRunObject } from '../state/useRunObject'
import { formatBytes } from '../state/format'

interface MallocProfile {
  supported: boolean
  n_alloc: number
  n_free: number
  bytes_allocated: number
  bytes_freed: number
  peak_live_bytes: number
  outstanding_bytes: number
  outstanding_blocks: number
  free_unmatched: number
  top_sizes: { size: number; count: number }[]
  largest_live: { addr: string; size: number }[]
}
interface Hotspot {
  function: string
  calls: number
  total_ms: number
  avg_ms: number
  errors: number
}
interface Profile {
  malloc: MallocProfile
  hotspots: Hotspot[]
}

interface Props {
  backendUrl: string
  runId: string
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="stat-cell">
      <div className={`stat-cell__value ${warn ? 'stat-cell__value--warn' : ''}`}>{value}</div>
      <div className="stat-cell__label">{label}</div>
    </div>
  )
}

/** Allocation ledger + library-call hotspots for an ltrace (Library calls) run. */
export function ProfilingTab({ backendUrl, runId }: Props) {
  const { data, loading } = useRunObject<Profile>(backendUrl, runId, 'profile')
  const m = data?.malloc
  const hotspots = data?.hotspots ?? []

  if (loading && !data) {
    return (
      <div className="overview" data-testid="profiling-tab">
        <div className="overview__muted">Loading profile…</div>
      </div>
    )
  }

  if (!m?.supported) {
    return (
      <div className="overview" data-testid="profiling-tab">
        <h3 className="overview__h">Allocation profile</h3>
        <div className="overview__muted">
          No allocation profile for this run. Enable the <b>Library calls</b> collector
          (Live Monitor) and re-run a native program to capture malloc/free activity.
        </div>
      </div>
    )
  }

  const leaked = m.outstanding_bytes > 0

  return (
    <div className="overview" data-testid="profiling-tab">
      <h3 className="overview__h">Allocation profile</h3>
      <div className="stat-grid">
        <Stat label="allocations" value={m.n_alloc.toLocaleString()} />
        <Stat label="frees" value={m.n_free.toLocaleString()} />
        <Stat label="bytes allocated" value={formatBytes(m.bytes_allocated)} />
        <Stat label="bytes freed" value={formatBytes(m.bytes_freed)} />
        <Stat label="peak live" value={formatBytes(m.peak_live_bytes)} />
        <Stat label="leaked (live at exit)" value={formatBytes(m.outstanding_bytes)} warn={leaked} />
        <Stat label="live blocks" value={m.outstanding_blocks.toLocaleString()} warn={leaked} />
        <Stat label="unmatched frees" value={m.free_unmatched.toLocaleString()} warn={m.free_unmatched > 0} />
      </div>

      {m.largest_live.length > 0 && (
        <>
          <h3 className="overview__h">Largest un-freed blocks</h3>
          <div className="top-syscalls">
            {m.largest_live.map((b) => (
              <div key={b.addr} className="top-syscall">
                <span className="top-syscall__name">{b.addr}</span>
                <span className="top-syscall__count">{formatBytes(b.size)}</span>
              </div>
            ))}
          </div>
        </>
      )}

      <h3 className="overview__h">Library-call hotspots — {hotspots.length} functions</h3>
      {hotspots.length === 0 ? (
        <div className="overview__muted">No library calls recorded.</div>
      ) : (
        <table className="syscall-table" data-testid="hotspot-table">
          <thead>
            <tr>
              <th>function</th>
              <th className="num">calls</th>
              <th className="num">total ms</th>
              <th className="num">avg ms</th>
              <th className="num">errors</th>
            </tr>
          </thead>
          <tbody>
            {hotspots.map((h) => (
              <tr key={h.function}>
                <td className="syscall-name">{h.function}</td>
                <td className="num">{h.calls.toLocaleString()}</td>
                <td className="num">{h.total_ms.toFixed(2)}</td>
                <td className="num">{h.avg_ms.toFixed(4)}</td>
                <td className={`num ${h.errors > 0 ? 'errs' : ''}`}>{h.errors}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
