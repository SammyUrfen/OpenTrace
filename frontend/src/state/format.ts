/** Shared formatting + severity helpers for the renderer. */

export const SEVERITY_COLOR: Record<string, string> = {
  critical: '#f87171',
  high: '#fb923c',
  medium: '#fbbf24',
  low: '#60a5fa',
  clean: '#4ade80',
}

/** A run's severity dot colour: live runs pulse green; finished use severity. */
export function severityColor(maxSeverity: string | null, status: string): string {
  if (status === 'running' || status === 'analyzing') return '#4ade80'
  if (status === 'error') return '#f87171'
  return SEVERITY_COLOR[maxSeverity ?? 'clean'] ?? '#4ade80'
}

export function statusLabel(run: { status: string; exit_code: number | null; exit_signal: string | null }): string {
  switch (run.status) {
    case 'running':
      return 'running'
    case 'analyzing':
      return 'analyzing'
    case 'error':
      return 'error'
    default:
      if (run.exit_signal) return run.exit_signal
      if (run.exit_code === 0) return 'ok'
      return `exit ${run.exit_code ?? '?'}`
  }
}

export function statusClass(run: { status: string; exit_code: number | null }): string {
  if (run.status === 'running' || run.status === 'analyzing') return 'running'
  if (run.status === 'error') return 'fail'
  return run.exit_code === 0 ? 'ok' : 'fail'
}

export function formatDuration(ms: number | null): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s - m * 60)}s`
}

export function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function formatBytesPerSec(bps: number | null): string {
  if (bps == null) return '—'
  if (bps < 1024) return `${bps.toFixed(0)} B/s`
  if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`
  return `${(bps / (1024 * 1024)).toFixed(1)} MB/s`
}
