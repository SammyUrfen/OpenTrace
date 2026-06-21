import type { ReactNode } from 'react'
import type { Run } from '../state/useOpenTrace'
import { severityColor } from '../state/format'

interface Props {
  openRuns: Run[]
  activeRunId: string | null
  onSelect: (id: string) => void
  onClose: (id: string) => void
  rightSlot?: ReactNode
}

/** Main tab bar: one tab per open run, plus a right-hand slot (tracing toggle). */
export function MainTabs({ openRuns, activeRunId, onSelect, onClose, rightSlot }: Props) {
  return (
    <div className="region region--main-tabs" data-placeholder="main-tab-bar">
      <div className="main-tabs">
        {openRuns.length === 0 && (
          <span className="main-tabs__hint">Click a run in the sidebar to open it</span>
        )}
        {openRuns.map((run) => (
          <div
            key={run.id}
            className={`main-tab ${run.id === activeRunId ? 'main-tab--active' : ''}`}
            onClick={() => onSelect(run.id)}
            role="tab"
            aria-selected={run.id === activeRunId}
            title={run.command}
          >
            <span
              className="main-tab__dot"
              style={{ background: severityColor(run.max_severity, run.status) }}
            />
            <span className="main-tab__label">{run.display_name}</span>
            <button
              type="button"
              className="main-tab__close"
              aria-label="close tab"
              onClick={(e) => {
                e.stopPropagation()
                onClose(run.id)
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
