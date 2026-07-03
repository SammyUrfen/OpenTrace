import { useEffect, useMemo, useRef, useState } from 'react'

export interface Command {
  id: string
  label: string
  hint?: string
  group?: string
  run: () => void
}

interface Props {
  open: boolean
  commands: Command[]
  onClose: () => void
}

/** Subsequence fuzzy match; returns a score (lower = better) or -1 for no match. */
function score(text: string, q: string): number {
  if (!q) return 0
  const t = text.toLowerCase()
  let ti = 0
  let gaps = 0
  let last = -1
  for (const ch of q.toLowerCase()) {
    const found = t.indexOf(ch, ti)
    if (found === -1) return -1
    if (last !== -1) gaps += found - last - 1
    last = found
    ti = found + 1
  }
  return gaps
}

/** Ctrl+K quick-switcher: fuzzy-find a run/session/action and run it. */
export function CommandPalette({ open, commands, onClose }: Props) {
  const [q, setQ] = useState('')
  const [idx, setIdx] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const filtered = useMemo(() => {
    const scored = commands
      .map((c) => ({ c, s: score(`${c.group ?? ''} ${c.label} ${c.hint ?? ''}`, q) }))
      .filter((x) => x.s >= 0)
      .sort((a, b) => a.s - b.s)
    return scored.map((x) => x.c).slice(0, 50)
  }, [commands, q])

  useEffect(() => {
    if (open) {
      setQ('')
      setIdx(0)
      // focus after paint
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])
  useEffect(() => setIdx(0), [q])

  if (!open) return null

  const choose = (c: Command | undefined) => {
    if (!c) return
    onClose()
    c.run()
  }

  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div className="palette" onMouseDown={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="palette__input"
          placeholder="Jump to a run, session, or action…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setIdx((i) => Math.min(i + 1, filtered.length - 1))
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setIdx((i) => Math.max(i - 1, 0))
            } else if (e.key === 'Enter') {
              e.preventDefault()
              choose(filtered[idx])
            } else if (e.key === 'Escape') {
              e.preventDefault()
              onClose()
            }
          }}
        />
        <ul className="palette__list">
          {filtered.length === 0 && <li className="palette__empty">No matches</li>}
          {filtered.map((c, i) => (
            <li
              key={c.id}
              className={`palette__item ${i === idx ? 'palette__item--active' : ''}`}
              onMouseEnter={() => setIdx(i)}
              onMouseDown={(e) => {
                e.preventDefault()
                choose(c)
              }}
            >
              {c.group && <span className="palette__group">{c.group}</span>}
              <span className="palette__label">{c.label}</span>
              {c.hint && <span className="palette__hint">{c.hint}</span>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
