import { useCallback, useEffect, useState } from 'react'

/** An open main tab: either a single run, or a diff of two runs (A ↔ B). */
export type Tab =
  | { kind: 'run'; runId: string }
  | { kind: 'diff'; aId: string; bId: string }

export function tabKey(t: Tab): string {
  return t.kind === 'run' ? `run:${t.runId}` : `diff:${t.aId}:${t.bId}`
}

const TABS_KEY = 'opentrace-tabs'
const ACTIVE_KEY = 'opentrace-active-tab'

function loadTabs(): Tab[] {
  try {
    const raw = localStorage.getItem(TABS_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

export function useTabs() {
  // Restore open tabs across restarts; App prunes any whose run(s) no longer
  // exist once the run list has loaded.
  const [tabs, setTabs] = useState<Tab[]>(loadTabs)
  const [activeKey, setActiveKey] = useState<string | null>(
    () => localStorage.getItem(ACTIVE_KEY),
  )
  const [activeView, setActiveView] = useState('overview')

  useEffect(() => {
    try {
      localStorage.setItem(TABS_KEY, JSON.stringify(tabs))
    } catch {
      /* storage full / unavailable — non-fatal */
    }
  }, [tabs])
  useEffect(() => {
    try {
      if (activeKey) localStorage.setItem(ACTIVE_KEY, activeKey)
      else localStorage.removeItem(ACTIVE_KEY)
    } catch {
      /* non-fatal */
    }
  }, [activeKey])

  const open = useCallback((t: Tab) => {
    const k = tabKey(t)
    setTabs((prev) => (prev.some((x) => tabKey(x) === k) ? prev : [...prev, t]))
    setActiveKey(k)
    setActiveView('overview')
  }, [])

  const openRun = useCallback((id: string) => open({ kind: 'run', runId: id }), [open])
  const openDiff = useCallback(
    (aId: string, bId: string) => open({ kind: 'diff', aId, bId }),
    [open],
  )

  const select = useCallback((k: string) => {
    setActiveKey(k)
    setActiveView('overview')
  }, [])

  const close = useCallback(
    (k: string) => {
      setTabs((prev) => prev.filter((x) => tabKey(x) !== k))
      setActiveKey((cur) => {
        if (cur !== k) return cur
        const idx = tabs.findIndex((x) => tabKey(x) === k)
        const next = tabs.filter((x) => tabKey(x) !== k)
        return next[idx] ? tabKey(next[idx]) : next[idx - 1] ? tabKey(next[idx - 1]) : null
      })
    },
    [tabs],
  )

  return {
    tabs, setTabs, activeKey, setActiveKey, activeView, setActiveView,
    openRun, openDiff, select, close,
  }
}
