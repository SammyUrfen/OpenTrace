import { useRunResource } from './useRunResource'

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
export const useSyscalls = (backendUrl: string, runId: string | null) =>
  useRunResource<SyscallStat>(backendUrl, runId, 'syscalls')
