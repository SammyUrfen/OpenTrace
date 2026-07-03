import { useEffect, useRef, useState } from 'react'

interface Props {
  title: string
  initial?: string
  submitLabel: string
  placeholder?: string
  onSubmit: (name: string) => void
  onClose: () => void
}

/** Small create/rename dialog for sessions and runs (Electron has no window.prompt). */
export function SessionModal({
  title, initial = '', submitLabel, placeholder = 'Session name…', onSubmit, onClose,
}: Props) {
  const [name, setName] = useState(initial)
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])

  const submit = () => {
    const n = name.trim()
    if (n) onSubmit(n)
    onClose()
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal modal--small" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h2>{title}</h2>
          <button type="button" className="modal__close" onClick={onClose} aria-label="close">×</button>
        </div>
        <div className="modal__body">
          <input
            ref={ref}
            className="session-create__input"
            placeholder={placeholder}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit()
              else if (e.key === 'Escape') onClose()
            }}
          />
        </div>
        <div className="modal__foot">
          <button type="button" className="ai-btn ai-btn--primary" onClick={submit}>{submitLabel}</button>
        </div>
      </div>
    </div>
  )
}
