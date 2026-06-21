import { afterEach, describe, expect, it } from 'vitest'
import { applyTheme, readStoredTheme, resolveTheme, THEME_KEY } from './useTheme'

afterEach(() => {
  localStorage.clear()
  delete document.documentElement.dataset.theme
})

describe('theme helpers', () => {
  it('resolveTheme passes through explicit values', () => {
    expect(resolveTheme('dark')).toBe('dark')
    expect(resolveTheme('light')).toBe('light')
  })

  it('resolveTheme("auto") follows the OS preference (stubbed → dark)', () => {
    expect(resolveTheme('auto')).toBe('dark')
  })

  it('applyTheme sets <html data-theme>', () => {
    applyTheme('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    applyTheme('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('readStoredTheme defaults to dark and reads valid values', () => {
    expect(readStoredTheme()).toBe('dark')
    localStorage.setItem(THEME_KEY, 'light')
    expect(readStoredTheme()).toBe('light')
    localStorage.setItem(THEME_KEY, 'garbage')
    expect(readStoredTheme()).toBe('dark')
  })
})
