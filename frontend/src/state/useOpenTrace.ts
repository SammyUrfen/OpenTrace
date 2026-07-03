import { useCallback, useEffect, useRef, useState } from 'react'

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

const RING = 120 // samples kept per series for the sparklines

function push(arr: number[], v: number | null): number[] {
  const next = arr.concat(v ?? 0)
  return next.length > RING ? next.slice(next.length - RING) : next
}

interface Hook {
  projects: Project[]
  runs: Run[]
  live: Record<string, LiveState>
  alerts: Record<string, LiveAlert[]>
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
  deleteRun: (id: string) => Promise<void>
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
  const [live, setLive] = useState<Record<string, LiveState>>({})
  const [alerts, setAlerts] = useState<Record<string, LiveAlert[]>>({})
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  const [lastEnded, setLastEnded] = useState<{ id: string; n: number } | null>(null)
  const [connected, setConnected] = useState(false)
  const [connectionError, setConnectionError] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const esRef = useRef<EventSource | null>(null)
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

  useEffect(() => {
    void refresh()
    const es = new EventSource(`${backendUrl}/stream`)
    esRef.current = es
    es.onopen = () => { setConnected(true); setConnectionError(false) }
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
      } else if (type === 'run_analyzing') {
        setRuns((prev) =>
          prev.map((r) => (r.id === run_id ? { ...r, status: 'analyzing' } : r)),
        )
      } else if (type === 'run_ended') {
        if (data && data.id) upsertRun(data as Run)
        setLiveRunId((cur) => (cur === run_id ? null : cur))
        endCount.current += 1
        setLastEnded({ id: run_id, n: endCount.current })
        // Drop the finished run's live ring buffers so `live` can't grow without
        // bound over a long session (the run's metrics are persisted server-side).
        setLive((prev) => {
          if (!(run_id in prev)) return prev
          const { [run_id]: _dropped, ...rest } = prev
          return rest
        })
      } else if (type === 'metric') {
        const s = data as MetricSample
        setLive((prev) => {
          const cur = prev[run_id] ?? { latest: null, cpu: [], rss: [], fds: [] }
          return {
            ...prev,
            [run_id]: {
              latest: s,
              cpu: push(cur.cpu, s.cpu_pct),
              rss: push(cur.rss, s.rss_mb),
              fds: push(cur.fds, s.open_fds),
            },
          }
        })
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
      esRef.current = null
    }
  }, [backendUrl, refresh, upsertRun])

  return {
    projects, runs, live, alerts, liveRunId, lastEnded, connected, connectionError,
    loaded, refresh, deleteRun, renameRun, createSession, renameSession,
  }
}
