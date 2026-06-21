import { useCallback, useEffect, useState } from 'react'

export type Theme = 'dark' | 'light' | 'auto'

export const THEME_KEY = 'opentrace-theme'

export function resolveTheme(t: Theme): 'dark' | 'light' {
  if (t === 'auto') {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  }
  return t
}

/** Apply a theme to <html data-theme> immediately. Exported so `main.tsx` can
 *  call it before first paint (no flash-of-dark). */
export function applyTheme(t: Theme): void {
  document.documentElement.dataset.theme = resolveTheme(t)
}

export function readStoredTheme(): Theme {
  const v = localStorage.getItem(THEME_KEY)
  return v === 'light' || v === 'auto' ? v : 'dark'
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme)

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // Track OS preference while in "auto".
  useEffect(() => {
    if (theme !== 'auto') return
    const mq = window.matchMedia('(prefers-color-scheme: light)')
    const handler = () => applyTheme('auto')
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [theme])

  const setTheme = useCallback((t: Theme) => {
    localStorage.setItem(THEME_KEY, t)
    setThemeState(t)
  }, [])

  const toggle = useCallback(() => {
    setTheme(resolveTheme(theme) === 'dark' ? 'light' : 'dark')
  }, [theme, setTheme])

  return { theme, resolved: resolveTheme(theme), setTheme, toggle }
}
