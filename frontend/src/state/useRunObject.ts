import { useEffect, useState } from 'react'

/**
 * Lazy fetch of a run sub-resource that returns a single JSON object (e.g.
 * `profile`, `flamegraph`). Re-fetches when the run changes; cancels stale
 * responses. Mirrors `useRunResource` but for object (not array) payloads.
 */
export function useRunObject<T>(
  backendUrl: string,
  runId: string | null,
  resource: string,
): { data: T | null; loading: boolean } {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!runId) {
      setData(null)
      return
    }
    let cancelled = false
    setLoading(true)
    fetch(`${backendUrl}/runs/${runId}/${resource}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d: T) => {
        if (!cancelled) {
          setData(d ?? null)
          setLoading(false)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setData(null)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [backendUrl, runId, resource])

  return { data, loading }
}
