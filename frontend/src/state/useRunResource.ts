import { useEffect, useState } from 'react'
import { cachedFetch, fetchJsonStrict, isRunImmutable } from './runCache'

/**
 * Generic lazy fetch of a run sub-resource (e.g. `io`, `network`, `logs`) that
 * returns an array. Re-fetches when the run changes; cancels stale responses.
 * Finalized runs' payloads are immutable and served from the shared run cache,
 * so remounting the tab doesn't re-pay the backend aggregation.
 */
export function useRunResource<T>(
  backendUrl: string,
  runId: string | null,
  resource: string,
): { rows: T[]; loading: boolean } {
  const [rows, setRows] = useState<T[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!runId) {
      setRows([])
      return
    }
    let cancelled = false
    setLoading(true)
    const url = `${backendUrl}/runs/${runId}/${resource}`
    cachedFetch<T[]>(runId, url, isRunImmutable(runId), () => fetchJsonStrict<T[]>(url))
      .then((data) => {
        if (!cancelled) {
          setRows(Array.isArray(data) ? data : [])
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
  }, [backendUrl, runId, resource])

  return { rows, loading }
}
