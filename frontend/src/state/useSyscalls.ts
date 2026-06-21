import { useEffect, useState } from 'react'

export interface SyscallStat {
  syscall: string
  count: number
  total_ms: number
  avg_ms: number | null
  p50_ms: number | null
  p95_ms: number | null
  p99_ms: number | null
  errors: number
  pct_runtime: number
}

/** Lazily fetch per-syscall stats for a run (only when the tab mounts). */
export function useSyscalls(backendUrl: string, runId: string | null) {
  const [rows, setRows] = useState<SyscallStat[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!runId) {
      setRows([])
      return
    }
    let cancelled = false
    setLoading(true)
    fetch(`${backendUrl}/runs/${runId}/syscalls`)
      .then((r) => (r.ok ? r.json() : []))
      .then((data: SyscallStat[]) => {
        if (!cancelled) {
          setRows(data)
          setLoading(false)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setRows([])
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [backendUrl, runId])

  return { rows, loading }
}
