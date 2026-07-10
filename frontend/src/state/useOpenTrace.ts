import { useCallback, useEffect, useRef, useState } from 'react'
import { evictLiveMetrics, recordLiveSample } from './liveMetrics'
import { clearRunCache, registerRunStatuses } from './runCache'

/** Mirrors `app.sessions.Session` (a project/workspace). */
export interface Project {
  id: string
  display_name: string
  slug: string
  created_at: number
  updated_at: number
  last_opened_at: number | null
  notes: string | null
}

/** Mirrors `app.runs.Run` (one traced command execution). */
export interface Run {
  id: string
  session_id: string
  terminal_id: string | null
  display_name: string
  command: string
  command_basename: string
  cwd: string
  started_at: number
  ended_at: number | null
  duration_ms: number | null
  exit_code: number | null
  exit_signal: string | null
  status: string
  label: string | null
  max_severity: string | null
  collector_config: Record<string, boolean> | null
  created_at: number
}

/** Mirrors `app.trace.events.MetricSample` (one psutil sample). */
export interface MetricSample {
  timestamp_ms: number
  cpu_pct: number | null
  rss_mb: number | null
  vms_mb: number | null
  open_fds: number | null
  threads: number | null
  syscall_rate: number | null
  io_read_bps: number | null
  io_write_bps: number | null
}

export interface LiveState {
  latest: MetricSample | null
  cpu: number[]
  rss: number[]
  fds: number[]
}

/** A live anomaly alert pushed during a run (from metric thresholds). */
export interface LiveAlert {
  severity: string
  title: string
  timestamp_ms: number
}

/** A monitor-mode incident: an anomaly with when / where (hot path) / context. */
export interface Incident {
  id: string
  run_id: string
  ts: number
  first_ts?: number
  last_ts?: number
  /** occurrences collapsed into this one entry (repeats of the same rule) */
  count?: number
  rule_id: string
  severity: string
  title: string
  hot: { functions: string[]; stack: string[]; samples?: number } | null
  metrics: MetricSample[]
  /** true sample count when the endpoint downsampled the embedded window */
  metrics_n?: number
  ai: string | null
}

/** One captured DB query nested under a sampled request span. */
export interface RequestDbSpan {
  name: string
  dur_ms: number
  /** CLOCK_MONOTONIC start (relative to its parent request) — waterfall x-axis. */
  start_ns?: number
  statement: string | null
}
/**
 * Per-request (or per-endpoint aggregate) decomposition of wall time. The four ms buckets
 * sum to the duration: on-CPU + run-queue + DB-wait (off-CPU overlapping a DB span) +
 * other off-CPU (labelled by blocking reason). Endpoint rows also carry the `_pct` shares
 * and the dominant `top_off_reason`.
 */
export interface RequestBreakdown {
  on_cpu_ms: number
  runq_ms: number
  db_wait_ms: number
  other_off_ms: number
  off_reasons?: Record<string, number>
  on_cpu_pct?: number
  runq_pct?: number
  db_wait_pct?: number
  other_off_pct?: number
  top_off_reason?: string | null
}
/** A sampled (slowest) request for the waterfall, with its nested DB spans + breakdown. */
export interface RequestSampleSpan {
  kind: string
  method: string | null
  route: string | null
  name: string
  status: number | null
  dur_ms: number
  tid: number
  db_ms: number
  start_ns?: number
  breakdown?: RequestBreakdown | null
  db: RequestDbSpan[]
}
/** One row of the per-endpoint RED table. */
export interface RequestEndpoint {
  method: string
  route: string
  count: number
  p50_ms: number | null
  p95_ms: number | null
  p99_ms: number | null
  err_pct: number
  db_ms_share: number
  breakdown?: RequestBreakdown | null
}
/** A curated slow/errored request span persisted to SQLite (GET /runs/{id}/request-spans). */
export interface RequestSpanRow {
  timestamp_ms: number
  method: string | null
  route: string | null
  name: string
  status: number | null
  dur_ms: number
  db_ms: number
  tid: number
  breakdown?: RequestBreakdown | null
  db: RequestDbSpan[]
}
/** The request-tracing rollup (`GET /runs/{id}/requests` + `request_rollup` SSE). */
export interface Requests {
  available: boolean
  reason: string | null
  window_s: number | null
  engine: string
  endpoints: RequestEndpoint[]
  spans: RequestSampleSpan[]
  request_count: number
  db_span_count: number
  has_breakdown?: boolean
}

interface Hook {
  projects: Project[]
  runs: Run[]
  alerts: Record<string, LiveAlert[]>
  /** monitor-mode incidents keyed by runId, newest first */
  incidents: Record<string, Incident[]>
  /** latest request-tracing rollup keyed by runId (live SSE for monitor runs) */
  requests: Record<string, Requests>
  liveRunId: string | null
  /** The most recently finalized run, as `{id, n}` — `n` increments per end so
   *  the same run ending twice (rare) still triggers a re-open. */
  lastEnded: { id: string; n: number } | null
  connected: boolean
  /** True once the SSE stream has errored at least once since the last open —
   *  lets the UI distinguish "still connecting" from "backend unreachable"
   *  without a health poll. */
  connectionError: boolean
  /** True once the first projects+runs fetch has resolved (used to avoid
   *  pruning restored tabs before the run list has loaded). */
  loaded: boolean
  refresh: () => Promise<void>
  /** Merge a run fetched outside the newest-200 window into local state. */
  upsertRun: (run: Run) => void
  deleteRun: (id: string) => Promise<void>
  stopMonitor: (id: string) => Promise<void>
  renameRun: (id: string, displayName: string) => Promise<void>
  createSession: (displayName: string) => Promise<Project | null>
  renameSession: (id: string, displayName: string) => Promise<void>
}

/**
 * Single source of truth for the renderer: projects + runs fetched over REST,
 * kept live via the backend's `/stream` SSE channel (run lifecycle + metrics).
 */
export function useOpenTrace(backendUrl: string): Hook {
  const [projects, setProjects] = useState<Project[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [alerts, setAlerts] = useState<Record<string, LiveAlert[]>>({})
  const [incidents, setIncidents] = useState<Record<string, Incident[]>>({})
  const [requests, setRequests] = useState<Record<string, Requests>>({})
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  const [lastEnded, setLastEnded] = useState<{ id: string; n: number } | null>(null)
  const [connected, setConnected] = useState(false)
  const [connectionError, setConnectionError] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const endCount = useRef(0)

  const refresh = useCallback(async () => {
    try {
      const [p, r] = await Promise.all([
        fetch(`${backendUrl}/sessions`).then((x) => x.json()),
        fetch(`${backendUrl}/runs?limit=200`).then((x) => x.json()),
      ])
      setProjects(p)
      setRuns(r)
      setLoaded(true)
      // Reconcile live-run state: a run that finished while the stream was
      // down (suspend, backend restart) never delivered its run_ended, so
      // drop its live indicator, alerts and ring buffers here — refresh()
      // runs on every SSE (re)connect, making it the resync path.
      const ended = new Set(
        (r as Run[]).filter((x) => x.status !== 'running').map((x) => x.id),
      )
      setLiveRunId((cur) => (cur && ended.has(cur) ? null : cur))
      setAlerts((prev) => {
        const stale = Object.keys(prev).filter((id) => ended.has(id))
        if (stale.length === 0) return prev
        const next = { ...prev }
        for (const id of stale) delete next[id]
        return next
      })
      ended.forEach((id) => evictLiveMetrics(id))
    } catch {
      /* backend not up yet; SSE + retry will catch up */
    }
  }, [backendUrl])

  const upsertRun = useCallback((run: Run) => {
    setRuns((prev) => {
      const without = prev.filter((r) => r.id !== run.id)
      return [run, ...without].sort((a, b) => b.started_at - a.started_at)
    })
  }, [])

  const deleteRun = useCallback(
    async (id: string) => {
      try {
        await fetch(`${backendUrl}/runs/${id}`, { method: 'DELETE' })
      } catch {
        /* ignore; we still drop it locally */
      }
      setRuns((prev) => prev.filter((r) => r.id !== id))
      const drop = <T,>(m: Record<string, T>) => {
        if (!(id in m)) return m
        const { [id]: _gone, ...rest } = m
        return rest
      }
      setIncidents(drop)
      setRequests(drop)
      setAlerts(drop)
      evictLiveMetrics(id)
      clearRunCache(id)
    },
    [backendUrl],
  )

  const stopMonitor = useCallback(
    async (id: string): Promise<void> => {
      try {
        await fetch(`${backendUrl}/runs/${id}/stop`, { method: 'POST' })
      } catch {
        /* ignore; the run finalizes on target exit regardless */
      }
    },
    [backendUrl],
  )

  const renameRun = useCallback(
    async (id: string, name: string): Promise<void> => {
      // Empty label clears the custom name (falls back to the command / default).
      const label = name.trim() || null
      // Set `label` (not display_name) so the rename shows everywhere the run is
      // referenced — tab AND sidebar AND palette — via `label ?? command`.
      // Optimistic: reflect immediately, reconcile on response.
      setRuns((prev) => prev.map((r) => (r.id === id ? { ...r, label } : r)))
      try {
        const r = await fetch(`${backendUrl}/runs/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label }),
        })
        if (r.ok) {
          const run: Run = await r.json()
          setRuns((prev) => prev.map((x) => (x.id === id ? run : x)))
        }
      } catch {
        /* ignore; optimistic update stands until next refresh */
      }
    },
    [backendUrl],
  )

  const createSession = useCallback(
    async (displayName: string): Promise<Project | null> => {
      try {
        const r = await fetch(`${backendUrl}/sessions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: displayName }),
        })
        if (!r.ok) return null
        const proj: Project = await r.json()
        setProjects((prev) => [proj, ...prev.filter((p) => p.id !== proj.id)])
        return proj
      } catch {
        return null
      }
    },
    [backendUrl],
  )

  const renameSession = useCallback(
    async (id: string, displayName: string): Promise<void> => {
      try {
        const r = await fetch(`${backendUrl}/sessions/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: displayName }),
        })
        if (r.ok) {
          const proj: Project = await r.json()
          setProjects((prev) => prev.map((p) => (p.id === id ? proj : p)))
        }
      } catch {
        /* ignore */
      }
    },
    [backendUrl],
  )

  // Keep the run-status registry in sync so the per-run fetch cache knows
  // which runs are finalized (immutable) — see state/runCache.ts.
  useEffect(() => {
    registerRunStatuses(runs)
  }, [runs])

  useEffect(() => {
    void refresh()
    const es = new EventSource(`${backendUrl}/stream`)
    // Re-sync on every (re)connect: lifecycle events emitted while the stream
    // was down are gone for good (the broker has no replay), so refetch.
    es.onopen = () => { setConnected(true); setConnectionError(false); void refresh() }
    es.onerror = () => { setConnected(false); setConnectionError(true) }
    es.onmessage = (ev) => {
      let msg: { type: string; run_id: string; data: any }
      try {
        msg = JSON.parse(ev.data)
      } catch {
        return
      }
      const { type, run_id, data } = msg
      if (type === 'run_started') {
        upsertRun(data as Run)
        setLiveRunId(run_id)
        setAlerts((prev) => ({ ...prev, [run_id]: [] }))
        void refreshProjects()
      } else if (type === 'anomaly_alert') {
        const al = data as LiveAlert
        setAlerts((prev) => {
          const cur = prev[run_id] ?? []
          return { ...prev, [run_id]: [...cur, al].slice(-12) }
        })
      } else if (type === 'incident') {
        const inc = data as Incident
        setIncidents((prev) => ({
          ...prev,
          [run_id]: [inc, ...(prev[run_id] ?? [])].slice(0, 100),
        }))
      } else if (type === 'incident_update' || type === 'incident_ai') {
        const patch = data as { id: string } & Partial<Incident>
        setIncidents((prev) => {
          const cur = prev[run_id]
          if (!cur) return prev
          return { ...prev, [run_id]: cur.map((i) => (i.id === patch.id ? { ...i, ...patch } : i)) }
        })
      } else if (type === 'request_rollup') {
        // latest per-snapshot endpoint RED for a live monitor run (throttled server-side);
        // the RequestsTab prefers this over its one-shot fetch so it updates in place
        setRequests((prev) => ({ ...prev, [run_id]: data as Requests }))
      } else if (type === 'run_analyzing') {
        setRuns((prev) =>
          prev.map((r) => (r.id === run_id ? { ...r, status: 'analyzing' } : r)),
        )
      } else if (type === 'run_ended') {
        if (data && data.id) upsertRun(data as Run)
        setLiveRunId((cur) => (cur === run_id ? null : cur))
        endCount.current += 1
        setLastEnded({ id: run_id, n: endCount.current })
        // Drop the finished run's live ring buffers so the store can't grow
        // without bound over a long session (metrics persist server-side), and
        // defensively clear any cached fetches made before it finalized.
        evictLiveMetrics(run_id)
        clearRunCache(run_id)
        // live alerts are superseded by the finalized anomalies/incidents; drop them
        setAlerts((prev) => {
          if (!(run_id in prev)) return prev
          const { [run_id]: _dropped, ...rest } = prev
          return rest
        })
      } else if (type === 'metric') {
        // Deliberately NOT React state: 4 samples/s per live run would
        // re-render the whole app — subscribers pull from the module store.
        recordLiveSample(run_id, data as MetricSample)
      }
    }

    async function refreshProjects() {
      try {
        const p = await fetch(`${backendUrl}/sessions`).then((x) => x.json())
        setProjects(p)
      } catch {
        /* ignore */
      }
    }

    return () => {
      es.close()
    }
  }, [backendUrl, refresh, upsertRun])

  return {
    projects, runs, alerts, incidents, requests, liveRunId, lastEnded, connected,
    connectionError, loaded, refresh, upsertRun, deleteRun, stopMonitor,
    renameRun, createSession, renameSession,
  }
}
