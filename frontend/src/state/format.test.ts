import { describe, expect, it } from 'vitest'
import {
  formatBytesPerSec,
  formatDuration,
  severityColor,
  statusClass,
  statusLabel,
} from './format'

describe('format helpers', () => {
  it('severityColor: live runs pulse green, finished use severity', () => {
    expect(severityColor('high', 'running')).toBe('#4ade80')
    expect(severityColor('high', 'completed')).toBe('#fb923c')
    expect(severityColor('critical', 'completed')).toBe('#f87171')
    expect(severityColor(null, 'completed')).toBe('#4ade80') // clean
    expect(severityColor('high', 'error')).toBe('#f87171')
  })

  it('statusLabel/statusClass reflect exit + status', () => {
    expect(statusLabel({ status: 'running', exit_code: null, exit_signal: null })).toBe('running')
    expect(statusLabel({ status: 'completed', exit_code: 0, exit_signal: null })).toBe('ok')
    expect(statusLabel({ status: 'completed', exit_code: 7, exit_signal: null })).toBe('exit 7')
    expect(statusLabel({ status: 'completed', exit_code: 137, exit_signal: 'KILL' })).toBe('KILL')
    expect(statusClass({ status: 'completed', exit_code: 0 })).toBe('ok')
    expect(statusClass({ status: 'completed', exit_code: 1 })).toBe('fail')
    expect(statusClass({ status: 'running', exit_code: null })).toBe('running')
  })

  it('attach runs use profiling wording + a neutral class, not exit-code failure', () => {
    const attach = { attach: true }
    // fail-open attach: the target outlived us, so a null exit code is not a failure
    expect(statusLabel({ status: 'completed', exit_code: null, exit_signal: null, collector_config: attach })).toBe('profiled')
    expect(statusClass({ status: 'completed', exit_code: null, collector_config: attach })).toBe('ok')
    // target exited during the window — still non-red wording
    expect(statusLabel({ status: 'completed', exit_code: 0, exit_signal: null, collector_config: attach })).toBe('target exited')
    expect(statusClass({ status: 'completed', exit_code: 3, collector_config: attach })).toBe('ok')
    // launch runs are unaffected
    expect(statusLabel({ status: 'completed', exit_code: null, exit_signal: null })).toBe('exit ?')
    expect(statusClass({ status: 'completed', exit_code: 2 })).toBe('fail')
  })

  it('formatDuration is human readable', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(450)).toBe('450ms')
    expect(formatDuration(2500)).toBe('2.5s')
    expect(formatDuration(90000)).toBe('1m 30s')
  })

  it('formatBytesPerSec scales units', () => {
    expect(formatBytesPerSec(null)).toBe('—')
    expect(formatBytesPerSec(512)).toBe('512 B/s')
    expect(formatBytesPerSec(2048)).toBe('2.0 KB/s')
    expect(formatBytesPerSec(5 * 1024 * 1024)).toBe('5.0 MB/s')
  })
})
