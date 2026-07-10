/**
 * In-memory cache for run sub-resource fetches (summary, metrics, syscalls,
 * flamegraph, …). A finalized run's payloads are immutable — the orchestrator
 * writes everything at `_finalize` — so refetching them on every tab click
 * only re-pays backend decompression plus multi-MB JSON parsing. Entries are
 * cached ONLY for terminal-status runs; a live run keeps the fetch-on-mount
 * behavior, which is its only data-refresh path for these views.
 */
import { apiFetch } from './api'

// Payloads can be MBs (full metrics arrays), so keep the cache bounded (LRU).
const MAX_ENTRIES = 40

const cache = new Map<string, Promise<unknown>>() // insertion order == LRU order

/** runId -> last known status, kept in sync by useOpenTrace. */
const statuses = new Map<string, string>()

export function isTerminalStatus(status: string | null | undefined): boolean {
  return status != null && status !== 'running' && status !== 'analyzing'
}

/** Sync the status registry from the run list (called whenever runs update). */
export function registerRunStatuses(runs: { id: string; status: string }[]): void {
  for (const r of runs) statuses.set(r.id, r.status)
}

/** True when the run is known to be finalized (its artifacts are immutable). */
export function isRunImmutable(runId: string): boolean {
  return isTerminalStatus(statuses.get(runId))
}

/** Fetch JSON, throwing on HTTP errors so failures are never cached. */
export async function fetchJsonStrict<T>(url: string): Promise<T> {
  const r = await apiFetch(url)
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`)
  return (await r.json()) as T
}

const keyFor = (runId: string, url: string) => `${runId}::${url}`

/**
 * Fetch through the cache. `immutable === false` bypasses it entirely. The
 * stored value is the in-flight promise (which also dedupes concurrent
 * requests); a rejected load is evicted immediately so an error can never pin
 * a fallback for the whole session.
 */
export function cachedFetch<T>(
  runId: string,
  url: string,
  immutable: boolean,
  load: () => Promise<T>,
): Promise<T> {
  if (!immutable) return load()
  const key = keyFor(runId, url)
  const hit = cache.get(key) as Promise<T> | undefined
  if (hit) {
    cache.delete(key) // refresh LRU position
    cache.set(key, hit)
    return hit
  }
  const p = load()
  cache.set(key, p)
  p.catch(() => cache.delete(key))
  while (cache.size > MAX_ENTRIES) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    cache.delete(oldest)
  }
  return p
}

/** Drop one run's cached payloads (on delete / run_ended), or everything. */
export function clearRunCache(runId?: string): void {
  if (runId === undefined) {
    cache.clear()
    statuses.clear()
    return
  }
  const prefix = `${runId}::`
  for (const key of [...cache.keys()]) {
    if (key.startsWith(prefix)) cache.delete(key)
  }
  statuses.delete(runId)
}
