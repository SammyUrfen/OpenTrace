import { useCallback, useState } from 'react'

interface Opts {
  axis: 'x' | 'y'
  min: number
  max: number
  /** drag toward smaller coords increases the value (right sidebar / bottom panel) */
  invert?: boolean
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

/**
 * A persisted, draggable dimension (e.g. sidebar width or panel height).
 * Returns the current value and an onMouseDown to attach to a drag handle.
 */
export function useResizable(key: string, defaultVal: number, opts: Opts) {
  const [val, setVal] = useState(() => {
    const stored = Number(localStorage.getItem(key))
    return stored ? clamp(stored, opts.min, opts.max) : defaultVal
  })

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      const startPos = opts.axis === 'x' ? e.clientX : e.clientY
      const startVal = val
      const onMove = (ev: MouseEvent) => {
        const cur = opts.axis === 'x' ? ev.clientX : ev.clientY
        const delta = opts.invert ? startPos - cur : cur - startPos
        setVal(clamp(startVal + delta, opts.min, opts.max))
      }
      const onUp = () => {
        document.removeEventListener('mousemove', onMove)
        document.removeEventListener('mouseup', onUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        setVal((v) => {
          localStorage.setItem(key, String(v))
          return v
        })
      }
      document.body.style.cursor = opts.axis === 'x' ? 'col-resize' : 'row-resize'
      document.body.style.userSelect = 'none'
      document.addEventListener('mousemove', onMove)
      document.addEventListener('mouseup', onUp)
    },
    [val, key, opts.axis, opts.invert, opts.min, opts.max],
  )

  return { val, onMouseDown }
}
