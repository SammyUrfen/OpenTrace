import { useSyscalls } from '../state/useSyscalls'
import { SyscallTable } from './SyscallTable'

interface Props {
  backendUrl: string
  runId: string
}

export function SyscallTab({ backendUrl, runId }: Props) {
  const { rows, loading } = useSyscalls(backendUrl, runId)
  const totalCalls = rows.reduce((s, r) => s + r.count, 0)

  return (
    <div className="overview" data-testid="syscall-tab">
      <h3 className="overview__h">
        Syscall explorer — {rows.length} distinct syscalls,{' '}
        {totalCalls.toLocaleString()} calls
      </h3>
      {loading && rows.length === 0 ? (
        <div className="overview__muted">Aggregating…</div>
      ) : rows.length === 0 ? (
        <div className="overview__muted">No syscalls recorded for this run.</div>
      ) : (
        <SyscallTable rows={rows} />
      )}
    </div>
  )
}
