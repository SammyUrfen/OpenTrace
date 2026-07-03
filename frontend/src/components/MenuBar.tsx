import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

export interface MenuItemDef {
  label?: string
  action?: string
  accel?: string
  separator?: boolean
}
export interface MenuDef {
  label: string
  items: MenuItemDef[]
}

interface Props {
  menus: MenuDef[]
  /** Dispatches the same action strings as the native menu (menu:action). */
  onAction: (action: string) => void
}

/**
 * An in-app menu bar (File / View / Run / Help). We render our own instead of
 * relying on Electron's native OS menu bar, which does not display on some
 * Linux setups (notably KDE Plasma under Wayland). The native menu is kept
 * registered for its keyboard accelerators, but this is the visible one.
 *
 * The dropdown is portalled to <body> so it can't be clipped or out-stacked by
 * the app-shell's grid regions (which otherwise paint over an in-flow dropdown).
 * Behaves like a real menu bar: click a top item to open it, hovering another
 * top item switches while a menu is open, outside-click / Escape closes.
 */
export function MenuBar({ menus, onAction }: Props) {
  const [open, setOpen] = useState<number | null>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (open === null) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement
      if (ref.current?.contains(t)) return             // the bar itself
      if (t.closest?.('.menubar__dropdown')) return     // the portalled menu
      setOpen(null)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(null) }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const openAt = (i: number, btn: HTMLElement) => {
    const r = btn.getBoundingClientRect()
    setPos({ left: r.left, top: r.bottom })
    setOpen(i)
  }
  const pick = (action?: string) => {
    setOpen(null)
    if (action) onAction(action)
  }

  return (
    <div className="menubar" ref={ref}>
      {menus.map((m, i) => (
        <button
          key={m.label}
          type="button"
          className={`menubar__top ${open === i ? 'menubar__top--open' : ''}`}
          aria-haspopup="true"
          aria-expanded={open === i}
          onClick={(e) => (open === i ? setOpen(null) : openAt(i, e.currentTarget))}
          onMouseEnter={(e) => { if (open !== null) openAt(i, e.currentTarget) }}
        >
          {m.label}
        </button>
      ))}
      {open !== null && pos && createPortal(
        <div
          className="menubar__dropdown"
          role="menu"
          style={{ position: 'fixed', left: pos.left, top: pos.top }}
        >
          {menus[open].items.map((it, j) =>
            it.separator ? (
              <div key={j} className="menubar__sep" />
            ) : (
              <button
                key={j}
                type="button"
                role="menuitem"
                className="menubar__menuitem"
                onClick={() => pick(it.action)}
              >
                <span>{it.label}</span>
                {it.accel && <span className="menubar__accel">{it.accel}</span>}
              </button>
            ),
          )}
        </div>,
        document.body,
      )}
    </div>
  )
}
