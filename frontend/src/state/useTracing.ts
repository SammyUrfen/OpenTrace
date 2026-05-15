import { useCallback, useEffect, useState } from 'react'

/**
 * App-wide ON/OFF state for OpenTrace. Mirrors the main-process value via IPC.
 *
 * Source of truth is the main process so that future wrappers around the pty
 * (and eventually real collectors) can read state without a renderer roundtrip.
 * In Phase 0 the toggle only writes a banner into the terminal stream — the
 * shell itself is untouched, so OFF is always a plain shell path.
 */
export function useTracing() {
  const [enabled, setEnabledState] = useState(false)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    window.opentrace?.tracing
      ?.get()
      .then((value) => {
        if (!cancelled) {
          setEnabledState(value)
          setReady(true)
        }
      })
      .catch(() => {
        if (!cancelled) setReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const setEnabled = useCallback(async (next: boolean) => {
    const tracing = window.opentrace?.tracing
    if (!tracing) {
      setEnabledState(next)
      return
    }
    const applied = await tracing.set(next)
    setEnabledState(applied)
  }, [])

  return { enabled, setEnabled, ready }
}
