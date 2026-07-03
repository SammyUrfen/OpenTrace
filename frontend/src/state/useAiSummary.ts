import { useCallback, useEffect, useSyncExternalStore } from 'react'

export type AiStatus =
  | 'idle'
  | 'thinking'
  | 'streaming'
  | 'done'
  | 'error'
  | 'unconfigured'

interface AiSnapshot {
  text: string
  status: AiStatus
  error: string | null
  cached: boolean
}

interface AiState extends AiSnapshot {
  generate: (force?: boolean) => void
}

/**
 * Per-run AI-summary stream lives in a module-level store (NOT in component
 * state) so it keeps generating when the Overview tab unmounts — switching tabs
 * mid-generation no longer aborts the stream. Components subscribe to the store
 * for their run via `useSyncExternalStore`; the EventSource is owned by the
 * store and only closed on done/error/re-analyze.
 */
interface Entry {
  text: string
  status: AiStatus
  error: string | null
  cached: boolean
  es: EventSource | null
  loaded: boolean // cached summary has been fetched for this run
  snapshot: AiSnapshot // stable reference for useSyncExternalStore
  listeners: Set<() => void>
}

const DEFAULT_SNAPSHOT: AiSnapshot = { text: '', status: 'idle', error: null, cached: false }
const store = new Map<string, Entry>()

const keyFor = (backendUrl: string, runId: string) => `${backendUrl}::${runId}`

function getEntry(key: string): Entry {
  let e = store.get(key)
  if (!e) {
    e = {
      text: '',
      status: 'idle',
      error: null,
      cached: false,
      es: null,
      loaded: false,
      snapshot: DEFAULT_SNAPSHOT,
      listeners: new Set(),
    }
    store.set(key, e)
  }
  return e
}

/** Rebuild the immutable snapshot and notify subscribers. */
function commit(e: Entry) {
  e.snapshot = { text: e.text, status: e.status, error: e.error, cached: e.cached }
  e.listeners.forEach((l) => l())
}

const isActive = (e: Entry) =>
  e.es !== null || e.status === 'streaming' || e.status === 'thinking'

/** Load the cached summary once per run, without ever clobbering a live stream. */
function loadCached(backendUrl: string, runId: string) {
  const e = getEntry(keyFor(backendUrl, runId))
  if (e.loaded || isActive(e) || (e.status === 'done' && e.text)) return
  e.loaded = true
  fetch(`${backendUrl}/runs/${runId}/ai-summary`)
    .then((r) => r.json())
    .then((d: { text: string | null; configured: boolean }) => {
      if (isActive(e)) return // a stream started while we were fetching — leave it
      if (d.text) {
        e.text = d.text
        e.status = 'done'
        e.cached = true
      } else if (!d.configured) {
        e.status = 'unconfigured'
      } else {
        e.status = 'idle'
      }
      commit(e)
    })
    .catch(() => {})
}

function startGenerate(backendUrl: string, runId: string, force: boolean) {
  const e = getEntry(keyFor(backendUrl, runId))
  e.es?.close()
  e.es = null
  e.text = ''
  e.error = null
  e.cached = false
  e.status = 'thinking'
  commit(e)

  const es = new EventSource(
    `${backendUrl}/runs/${runId}/ai-summary/stream?force=${force ? 'true' : 'false'}`,
  )
  e.es = es
  const detach = () => {
    es.close()
    if (e.es === es) e.es = null
  }
  es.onmessage = (ev) => {
    let m: { type: string; text?: string; message?: string }
    try {
      m = JSON.parse(ev.data)
    } catch {
      return
    }
    if (m.type === 'content') {
      e.status = 'streaming'
      e.text += m.text ?? ''
      commit(e)
    } else if (m.type === 'error') {
      e.status = 'error'
      e.error = m.message ?? 'unknown error'
      detach()
      commit(e)
    } else if (m.type === 'done') {
      e.status = 'done'
      detach()
      commit(e)
    }
    // 'thinking' keeps status as-is until content arrives
  }
  es.onerror = () => {
    // Connection dropped: keep partial content as 'done', else surface the error.
    // (We close() on done/error above, so this only fires on a real failure.)
    if (e.status !== 'done' && e.status !== 'streaming') {
      e.status = e.text ? 'done' : 'error'
    } else if (e.status === 'streaming') {
      e.status = 'done'
    }
    detach()
    commit(e)
  }
}

export function useAiSummary(backendUrl: string, runId: string | null): AiState {
  const key = runId ? keyFor(backendUrl, runId) : null

  const subscribe = useCallback(
    (cb: () => void) => {
      if (!key) return () => {}
      const e = getEntry(key)
      e.listeners.add(cb)
      return () => {
        e.listeners.delete(cb)
      }
    },
    [key],
  )

  const getSnapshot = useCallback(
    () => (key ? getEntry(key).snapshot : DEFAULT_SNAPSHOT),
    [key],
  )

  const snap = useSyncExternalStore(subscribe, getSnapshot)

  // Fetch the cached summary when the run changes (no-op if a stream is live).
  useEffect(() => {
    if (key && runId) loadCached(backendUrl, runId)
  }, [key, backendUrl, runId])

  const generate = useCallback(
    (force = false) => {
      if (runId) startGenerate(backendUrl, runId, force)
    },
    [backendUrl, runId],
  )

  return { ...snap, generate }
}
