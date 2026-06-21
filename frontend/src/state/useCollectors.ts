import { useCallback, useEffect, useState } from 'react'

export interface Collectors {
  strace: boolean
  psutil: boolean
  ltrace: boolean
  perf: boolean
}

interface TracingConfig {
  default_enabled: boolean
  collectors: Collectors
}

/** Reads + persists which collectors run, via `/config/tracing`. */
export function useCollectors(backendUrl: string) {
  const [tracing, setTracing] = useState<TracingConfig | null>(null)

  useEffect(() => {
    let cancelled = false
    fetch(`${backendUrl}/config/tracing`)
      .then((r) => r.json())
      .then((d: TracingConfig) => {
        if (!cancelled) setTracing(d)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [backendUrl])

  const toggle = useCallback(
    (key: keyof Collectors) => {
      setTracing((prev) => {
        if (!prev) return prev
        const next: TracingConfig = {
          ...prev,
          collectors: { ...prev.collectors, [key]: !prev.collectors[key] },
        }
        fetch(`${backendUrl}/config/tracing`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(next),
        }).catch(() => {})
        return next
      })
    },
    [backendUrl],
  )

  return { collectors: tracing?.collectors ?? null, toggle }
}
