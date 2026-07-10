import { useEffect, useRef } from 'react'

interface Props {
  title: string
  body: string
  confirmLabel?: string
  danger?: boolean
  onConfirm: () => void
  onClose: () => void
}

/** Generic styled confirm dialog (replaces window.confirm, which Electron renders
 *  unstyled and which blocks the whole process). Escape cancels, Enter confirms. */
export function ConfirmModal({ title, body, confirmLabel = 'Confirm', danger, onConfirm, onClose }: Props) {
  const ref = useRef<HTMLButtonElement>(null)
  useEffect(() => { ref.current?.focus() }, [])

  return (
    <div
      className="modal-backdrop"
      onMouseDown={onClose}
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
        else if (e.key === 'Enter') onConfirm()
      }}
    >
      <div className="modal modal--small" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h2>{title}</h2>
          <button type="button" className="modal__close" onClick={onClose} aria-label="close">×</button>
        </div>
        <div className="modal__body">
          <p className="confirm__body">{body}</p>
        </div>
        <div className="modal__foot">
          <button type="button" className="ai-btn" onClick={onClose}>Cancel</button>
          <button
            ref={ref}
            type="button"
            className={`ai-btn ai-btn--primary${danger ? ' ai-btn--danger' : ''}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
