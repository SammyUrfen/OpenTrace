import { useCallback, useSyncExternalStore } from 'react'
import type { LiveState, MetricSample } from './useOpenTrace'

/**
 * Live per-run metric ring buffers live in a module-level store, NOT in React
 * state at the App root: the /stream SSE delivers one sample per 250ms per
 * running run, and routing that through useOpenTrace's useState re-rendered
 * the entire tree 4×/s. Only the components that actually show live data
 * (LiveMonitor, a running run's Overview tab) subscribe, per runId.
 */

const RING = 120 // samples kept per series for the sparklines

function push(arr: number[], v: number | null): number[] {
  const next = arr.concat(v ?? 0)
  return next.length > RING ? next.slice(next.length - RING) : next
}

const states = new Map<string, LiveState>()
const listeners = new Map<string, Set<() => void>>()

function notify(runId: string) {
  listeners.get(runId)?.forEach((l) => l())
}

export function recordLiveSample(runId: string, s: MetricSample): void {
  const cur = states.get(runId) ?? { latest: null, cpu: [], rss: [], fds: [] }
  states.set(runId, {
    latest: s,
    cpu: push(cur.cpu, s.cpu_pct),
    rss: push(cur.rss, s.rss_mb),
    fds: push(cur.fds, s.open_fds),
  })
  notify(runId)
}

/** Drop a finished/deleted run's ring buffers (metrics persist server-side). */
export function evictLiveMetrics(runId: string): void {
  if (states.delete(runId)) notify(runId)
}

/** Subscribe to a run's live metric ring buffers (null while none exist). */
export function useLiveMetrics(runId: string | null): LiveState | null {
  const subscribe = useCallback(
    (cb: () => void) => {
      if (!runId) return () => {}
      let set = listeners.get(runId)
      if (!set) {
        set = new Set()
        listeners.set(runId, set)
      }
      set.add(cb)
      return () => {
        set.delete(cb)
        if (set.size === 0 && listeners.get(runId) === set) listeners.delete(runId)
      }
    },
    [runId],
  )
  const getSnapshot = useCallback(
    () => (runId ? states.get(runId) ?? null : null),
    [runId],
  )
  return useSyncExternalStore(subscribe, getSnapshot)
}
