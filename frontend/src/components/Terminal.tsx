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
      theme: { background: '#0f1014' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host)
    fit.fit()

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
