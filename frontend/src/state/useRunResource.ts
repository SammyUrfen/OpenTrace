import { useEffect, useState } from 'react'

/**
 * Generic lazy fetch of a run sub-resource (e.g. `io`, `network`, `logs`) that
 * returns an array. Re-fetches when the run changes; cancels stale responses.
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
    fetch(`${backendUrl}/runs/${runId}/${resource}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((data: T[]) => {
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
