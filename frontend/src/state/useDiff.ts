import { useEffect, useState } from 'react'
import type { MetricSample, Run } from './useOpenTrace'
import type { Anomaly, RunSummary } from './useRunDetail'
import type { SyscallStat } from './useSyscalls'
import { cachedFetch, fetchJsonStrict, isRunImmutable } from './runCache'

export interface RunBundle {
  run: Run | undefined
  summary: RunSummary | null
  metrics: MetricSample[]
  anomalies: Anomaly[]
  syscalls: SyscallStat[]
}

const EMPTY: Omit<RunBundle, 'run'> = {
  summary: null, metrics: [], anomalies: [], syscalls: [],
}

// Finalized runs' payloads come from the shared run cache (same entries the
// single-run tabs use), so closing and reopening a diff tab doesn't refetch.
async function getJson<T>(backendUrl: string, id: string, resource: string, fallback: T) {
  const url = `${backendUrl}/runs/${id}/${resource}`
  try {
    return await cachedFetch<T>(id, url, isRunImmutable(id), () => fetchJsonStrict<T>(url))
  } catch {
    return fallback
  }
}

async function loadRun(backendUrl: string, id: string) {
  const [summary, metrics, anomalies, syscalls] = await Promise.all([
    getJson<RunSummary | null>(backendUrl, id, 'summary', null),
    getJson<MetricSample[]>(backendUrl, id, 'metrics', []),
    getJson<Anomaly[]>(backendUrl, id, 'anomalies', []),
    getJson<SyscallStat[]>(backendUrl, id, 'syscalls', []),
  ])
  return { summary, metrics, anomalies, syscalls }
}

/** Fetch both runs' analytical data for the diff views. */
export function useDiff(backendUrl: string, aId: string, bId: string, runs: Run[]) {
  const [data, setData] = useState<{ a: Omit<RunBundle, 'run'>; b: Omit<RunBundle, 'run'> }>({
    a: EMPTY, b: EMPTY,
  })
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([loadRun(backendUrl, aId), loadRun(backendUrl, bId)]).then(([a, b]) => {
      if (cancelled) return
      setData({ a, b })
      setLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [backendUrl, aId, bId])

  const a: RunBundle = { run: runs.find((r) => r.id === aId), ...data.a }
  const b: RunBundle = { run: runs.find((r) => r.id === bId), ...data.b }
  return { a, b, loading }
}
