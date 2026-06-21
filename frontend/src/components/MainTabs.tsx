import type { ReactNode } from 'react'

export interface TabInfo {
  key: string
  label: string
  /** severity dot colour for run tabs; omitted for diff tabs (shows a ↔ glyph) */
  dotColor?: string
  diff?: boolean
  title?: string
}

interface Props {
  tabs: TabInfo[]
  activeKey: string | null
  onSelect: (key: string) => void
  onClose: (key: string) => void
  rightSlot?: ReactNode
}

/** Main tab bar: one tab per open run or diff, plus a right-hand slot. */
export function MainTabs({ tabs, activeKey, onSelect, onClose, rightSlot }: Props) {
  return (
    <div className="region region--main-tabs" data-placeholder="main-tab-bar">
      <div className="main-tabs">
        {tabs.length === 0 && (
          <span className="main-tabs__hint">Click a run in the sidebar to open it</span>
        )}
        {tabs.map((tab) => (
          <div
            key={tab.key}
            className={`main-tab ${tab.key === activeKey ? 'main-tab--active' : ''}`}
            onClick={() => onSelect(tab.key)}
            onAuxClick={(e) => {
              if (e.button === 1) {
                e.preventDefault()
                onClose(tab.key)
              }
            }}
            role="tab"
            aria-selected={tab.key === activeKey}
            title={tab.title ?? tab.label}
          >
            {tab.diff ? (
              <span className="main-tab__diff">↔</span>
            ) : (
              <span className="main-tab__dot" style={{ background: tab.dotColor }} />
            )}
            <span className="main-tab__label">{tab.label}</span>
            <button
              type="button"
              className="main-tab__close"
              aria-label="close tab"
              onClick={(e) => {
                e.stopPropagation()
                onClose(tab.key)
              }}
            >
              ×
            </button>
          </div>
        ))}
      </div>
      {rightSlot && <div className="region__right-slot">{rightSlot}</div>}
    </div>
  )
}
