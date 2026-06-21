import { useEffect, useState } from 'react'
import type { MetricSample } from './useOpenTrace'

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

async function getJson<T>(url: string, fallback: T): Promise<T> {
  try {
    const r = await fetch(url)
    if (!r.ok) return fallback
    return (await r.json()) as T
  } catch {
    return fallback
  }
}

/**
 * Fetch a run's analytical detail (summary + metrics + anomalies). Re-fetches
 * when `runId` or `statusKey` changes — pass the run's status as `statusKey` so
 * a still-running run's detail refreshes once it finalizes.
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

  useEffect(() => {
    if (!runId) {
      setDetail({ summary: null, metrics: [], anomalies: [], loading: false })
      return
    }
    let cancelled = false
    setDetail((d) => ({ ...d, loading: true }))
    Promise.all([
      getJson<RunSummary | null>(`${backendUrl}/runs/${runId}/summary`, null),
      getJson<MetricSample[]>(`${backendUrl}/runs/${runId}/metrics`, []),
      getJson<Anomaly[]>(`${backendUrl}/runs/${runId}/anomalies`, []),
    ]).then(([summary, metrics, anomalies]) => {
      if (cancelled) return
      setDetail({ summary, metrics, anomalies, loading: false })
    })
    return () => {
      cancelled = true
    }
  }, [backendUrl, runId, statusKey])

  return detail
}
