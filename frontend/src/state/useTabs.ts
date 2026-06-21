import { useCallback, useState } from 'react'

/** An open main tab: either a single run, or a diff of two runs (A ↔ B). */
export type Tab =
  | { kind: 'run'; runId: string }
  | { kind: 'diff'; aId: string; bId: string }

export function tabKey(t: Tab): string {
  return t.kind === 'run' ? `run:${t.runId}` : `diff:${t.aId}:${t.bId}`
}

export function useTabs() {
  const [tabs, setTabs] = useState<Tab[]>([])
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [activeView, setActiveView] = useState('overview')

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
