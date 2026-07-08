import { useEffect, useState } from 'react'
import type { MetricSample } from './useOpenTrace'
import { cachedFetch, fetchJsonStrict } from './runCache'

/** Mirrors `app.storage.read_anomalies` rows. */
export interface Anomaly {
  id: string
  rule_id: string
  severity: string
  severity_score: number
  title: string
  description: string
  evidence_ids: string[]
  first_seen_ms: number | null
  last_seen_ms: number | null
  occurrence_count: number
}

/** Mirrors the `meta.json` summary written by the orchestrator. */
export interface RunSummary {
  run_id?: string
  command?: string
  cwd?: string
  started_at?: number
  ended_at?: number | null
  exit_code?: number | null
  exit_signal?: string | null
  totals?: {
    syscall_events: number
    errors: number
    signals: number
    metric_samples: number
    top_syscalls: [string, number][]
  }
  peaks?: {
    rss_mb: number | null
    cpu_pct: number | null
    open_fds: number | null
    threads: number | null
  }
  averages?: { cpu_pct: number | null; rss_mb: number | null }
  anomalies?: { rule_id: string; severity: string; title: string }[]
  max_severity?: string | null
  pending?: boolean
}

export interface RunDetail {
  summary: RunSummary | null
  metrics: MetricSample[]
  anomalies: Anomaly[]
  loading: boolean
}

/**
 * Fetch a run's analytical detail (summary + metrics + anomalies). Re-fetches
 * when `runId` changes or the run finalizes — pass the run's status as
 * `statusKey` so a still-running run's detail refreshes once it's done. The
 * dependency is the derived `finalized` boolean (not the raw status) so the
 * transient running→analyzing hop doesn't trigger a wasted full refetch.
 * Finalized runs are immutable, so their payloads are served from the shared
 * run cache on revisit instead of re-downloading the full metrics array.
 */
export function useRunDetail(
  backendUrl: string,
  runId: string | null,
  statusKey?: string,
): RunDetail {
  const [detail, setDetail] = useState<RunDetail>({
    summary: null,
    metrics: [],
    anomalies: [],
    loading: false,
  })
  const finalized = statusKey !== 'running' && statusKey !== 'analyzing'

  useEffect(() => {
    if (!runId) {
      setDetail({ summary: null, metrics: [], anomalies: [], loading: false })
      return
    }
    let cancelled = false
    setDetail((d) => ({ ...d, loading: true }))
    const get = <T,>(resource: string, fallback: T): Promise<T> => {
      const url = `${backendUrl}/runs/${runId}/${resource}`
      return cachedFetch<T>(runId, url, finalized, () => fetchJsonStrict<T>(url)).catch(
        () => fallback,
      )
    }
    Promise.all([
      get<RunSummary | null>('summary', null),
      get<MetricSample[]>('metrics', []),
      get<Anomaly[]>('anomalies', []),
    ]).then(([summary, metrics, anomalies]) => {
      if (cancelled) return
      setDetail({ summary, metrics, anomalies, loading: false })
    })
    return () => {
      cancelled = true
    }
  }, [backendUrl, runId, finalized])

  return detail
}
