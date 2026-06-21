import { useCallback, useEffect, useRef, useState } from 'react'

export type AiStatus =
  | 'idle'
  | 'thinking'
  | 'streaming'
  | 'done'
  | 'error'
  | 'unconfigured'

interface AiState {
  text: string
  status: AiStatus
  error: string | null
  cached: boolean
  generate: (force?: boolean) => void
}

/**
 * Manages a run's AI summary: loads the cached one, and streams a fresh one over
 * SSE on demand (thinking → content → done), persisted server-side on completion.
 */
export function useAiSummary(backendUrl: string, runId: string | null): AiState {
  const [text, setText] = useState('')
  const [status, setStatus] = useState<AiStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const [cached, setCached] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  // Load cached summary (and configured-ness) when the run changes.
  useEffect(() => {
    esRef.current?.close()
    esRef.current = null
    setText('')
    setError(null)
    setStatus('idle')
    setCached(false)
    if (!runId) return
    let cancelled = false
    fetch(`${backendUrl}/runs/${runId}/ai-summary`)
      .then((r) => r.json())
      .then((d: { text: string | null; configured: boolean }) => {
        if (cancelled) return
        if (d.text) {
          setText(d.text)
          setStatus('done')
          setCached(true)
        } else if (!d.configured) {
          setStatus('unconfigured')
        } else {
          setStatus('idle')
        }
      })
      .catch(() => {})
    return () => {
      cancelled = true
      esRef.current?.close()
      esRef.current = null
    }
  }, [backendUrl, runId])

  const generate = useCallback(
    (force = false) => {
      if (!runId) return
      esRef.current?.close()
      setText('')
      setError(null)
      setCached(false)
      setStatus('thinking')
      const es = new EventSource(
        `${backendUrl}/runs/${runId}/ai-summary/stream?force=${force ? 'true' : 'false'}`,
      )
      esRef.current = es
      es.onmessage = (e) => {
        let m: { type: string; text?: string; message?: string }
        try {
          m = JSON.parse(e.data)
        } catch {
          return
        }
        if (m.type === 'content') {
          setStatus('streaming')
          setText((t) => t + (m.text ?? ''))
        } else if (m.type === 'error') {
          setStatus('error')
          setError(m.message ?? 'unknown error')
          es.close()
        } else if (m.type === 'done') {
          setStatus('done')
          es.close()
        }
        // 'thinking' keeps status as-is ('thinking') until content arrives
      }
      es.onerror = () => {
        // Ignore transient errors once we've already received content/done.
        setStatus((s) => (s === 'done' || s === 'streaming' ? s : 'error'))
        es.close()
      }
    },
    [backendUrl, runId],
  )

  return { text, status, error, cached, generate }
}
