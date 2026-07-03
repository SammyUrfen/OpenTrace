import { useEffect, useRef } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

export interface TerminalStartInfo {
  shell: string
  shellName: string
  cwd: string
  pid: number
}

export interface TerminalExitInfo {
  exitCode: number
  signal?: number
}

interface Props {
  onStart?: (info: TerminalStartInfo) => void
  onExit?: (info: TerminalExitInfo) => void
}

/**
 * xterm.js terminal wired to the main-process pty via window.opentrace.terminal.
 *
 * Responsibilities (deliberately narrow):
 *   - own the xterm.js instance and the fit addon
 *   - forward user input to the pty
 *   - paint pty output into xterm
 *   - keep pty cols/rows in sync with the visible cell grid
 *   - emit start/exit lifecycle events for the caller (e.g. session recording)
 *
 * It does NOT know about tracing state or session persistence — those flow
 * through main.js and App.tsx respectively. Tracing surfaces here as ordinary
 * pty data (the banner main injects on toggle).
 */
// xterm palettes matching the app's espresso (dark) / warm-paper (light) themes.
const TERM_DARK = {
  background: '#15100b', foreground: '#f0e7db', cursor: '#ff8c42',
  cursorAccent: '#15100b', selectionBackground: '#3d2f25',
  black: '#271e18', red: '#f06a51', green: '#9ccc65', yellow: '#ffb454',
  blue: '#7fb3d5', magenta: '#c39ac9', cyan: '#5fbfb3', white: '#d6c8b6',
  brightBlack: '#a8957f', brightRed: '#ff8266', brightGreen: '#b6e07a',
  brightYellow: '#ffc777', brightBlue: '#99c7e0', brightMagenta: '#d6b3da',
  brightCyan: '#7fd6c9', brightWhite: '#f0e7db',
}
const TERM_LIGHT = {
  background: '#efe4d2', foreground: '#2e2318', cursor: '#d9722b',
  cursorAccent: '#efe4d2', selectionBackground: '#e2d5c1',
  black: '#2e2318', red: '#b03a28', green: '#4a7c2f', yellow: '#a8701a',
  blue: '#2c6a9a', magenta: '#8a4b8f', cyan: '#2a7d72', white: '#6b5a44',
  brightBlack: '#8a7660', brightRed: '#c84a35', brightGreen: '#5a8f3a',
  brightYellow: '#bf8420', brightBlue: '#3a7aad', brightMagenta: '#9d5aa2',
  brightCyan: '#369085', brightWhite: '#2e2318',
}

function currentTermTheme() {
  return document.documentElement.dataset.theme === 'light' ? TERM_LIGHT : TERM_DARK
}

export function Terminal({ onStart, onExit }: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  // Keep callbacks current without re-running the heavy setup effect.
  const onStartRef = useRef(onStart)
  const onExitRef = useRef(onExit)
  onStartRef.current = onStart
  onExitRef.current = onExit

  useEffect(() => {
    const host = hostRef.current
    const api = window.opentrace?.terminal
    if (!host || !api) return

    const term = new XTerm({
      convertEol: false,
      cursorBlink: true,
      fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
      fontSize: 13,
      theme: currentTermTheme(),
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host)
    fit.fit()

    // Clipboard: Ctrl+Shift+C copies the selection, Ctrl+Shift+V pastes; a
    // right-click copies a selection or otherwise pastes. (xterm wires none of
    // these by default, which is why copy "didn't work".)
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown' || !e.ctrlKey || !e.shiftKey) return true
      if (e.code === 'KeyC') {
        const sel = term.getSelection()
        if (sel) void navigator.clipboard?.writeText(sel)
        return false
      }
      if (e.code === 'KeyV') {
        navigator.clipboard?.readText().then((t) => t && api.write(t)).catch(() => {})
        return false
      }
      return true
    })
    const onContextMenu = (e: MouseEvent) => {
      e.preventDefault()
      const sel = term.getSelection()
      if (sel) void navigator.clipboard?.writeText(sel)
      else navigator.clipboard?.readText().then((t) => t && api.write(t)).catch(() => {})
    }
    host.addEventListener('contextmenu', onContextMenu)

    // Re-theme the terminal when the app theme toggles.
    const themeObserver = new MutationObserver(() => {
      term.options.theme = currentTermTheme()
    })
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })

    const unsubscribeData = api.onData((data) => term.write(data))
    const unsubscribeExit = api.onExit((info) => {
      term.write(
        `\r\n\x1b[90m[shell exited code=${info.exitCode}${
          info.signal ? ` signal=${info.signal}` : ''
        }]\x1b[0m\r\n`,
      )
      onExitRef.current?.(info)
    })

    const inputDisposable = term.onData((data) => api.write(data))

    api
      .start({ cols: term.cols, rows: term.rows })
      .then((info) => {
        onStartRef.current?.({
          shell: info.shell,
          shellName: info.shellName,
          cwd: info.cwd,
          pid: info.pid,
        })
      })
      .catch((err) => term.write(`\r\n[opentrace] failed to start pty: ${err}\r\n`))

    const observer = new ResizeObserver(() => {
      try {
        fit.fit()
        api.resize(term.cols, term.rows)
      } catch {
        // fit can throw before the host has nonzero dimensions
      }
    })
    observer.observe(host)

    return () => {
      observer.disconnect()
      themeObserver.disconnect()
      host.removeEventListener('contextmenu', onContextMenu)
      inputDisposable.dispose()
      unsubscribeData()
      unsubscribeExit()
      term.dispose()
    }
  }, [])

  if (!window.opentrace?.terminal) {
    return (
      <div className="terminal-pane terminal-pane--unavailable">
        <span className="region__label">
          Terminal unavailable (Electron bridge not loaded)
        </span>
      </div>
    )
  }

  return <div ref={hostRef} className="terminal-pane" />
}
