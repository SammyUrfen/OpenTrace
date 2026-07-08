import { useCallback, useEffect, useRef, useState } from 'react'

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
  const [activeKey, setActiveKeyState] = useState<string | null>(
    () => localStorage.getItem(ACTIVE_KEY),
  )
  const [activeView, setActiveViewState] = useState('overview')
  // Each tab remembers its last-selected secondary view, so switching main
  // tabs (and back) restores the view instead of resetting to Overview —
  // which also avoids refetching that view's data on every return.
  const viewByTab = useRef<Map<string, string>>(new Map())
  // Mirror of `activeKey` for stable callbacks; updated only via setActiveKey.
  const activeKeyRef = useRef(activeKey)

  const setActiveKey = useCallback((k: string | null) => {
    activeKeyRef.current = k
    setActiveKeyState(k)
  }, [])

  const setActiveView = useCallback((v: string) => {
    const k = activeKeyRef.current
    if (k) viewByTab.current.set(k, v)
    setActiveViewState(v)
  }, [])

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

  // `focus` false opens the tab in the background without stealing the active
  // selection — used when a run finishes while the user is looking at another tab.
  const open = useCallback((t: Tab, focus = true) => {
    const k = tabKey(t)
    setTabs((prev) => (prev.some((x) => tabKey(x) === k) ? prev : [...prev, t]))
    if (focus) {
      setActiveKey(k)
      setActiveViewState(viewByTab.current.get(k) ?? 'overview')
    }
  }, [setActiveKey])

  const openRun = useCallback(
    (id: string, focus = true) => open({ kind: 'run', runId: id }, focus),
    [open],
  )
  const openDiff = useCallback(
    (aId: string, bId: string) => open({ kind: 'diff', aId, bId }),
    [open],
  )

  const select = useCallback((k: string) => {
    setActiveKey(k)
    setActiveViewState(viewByTab.current.get(k) ?? 'overview')
  }, [setActiveKey])

  const close = useCallback(
    (k: string) => {
      viewByTab.current.delete(k)
      setTabs((prev) => prev.filter((x) => tabKey(x) !== k))
      if (activeKeyRef.current === k) {
        const idx = tabs.findIndex((x) => tabKey(x) === k)
        const next = tabs.filter((x) => tabKey(x) !== k)
        const nk = next[idx] ? tabKey(next[idx]) : next[idx - 1] ? tabKey(next[idx - 1]) : null
        setActiveKey(nk)
        if (nk) setActiveViewState(viewByTab.current.get(nk) ?? 'overview')
      }
    },
    [tabs, setActiveKey],
  )

  return {
    tabs, setTabs, activeKey, setActiveKey, activeView, setActiveView,
    openRun, openDiff, select, close,
  }
}
