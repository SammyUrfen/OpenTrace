import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { RunBundle } from '../state/useDiff'
import { AnomalyDiff, SyscallDiff } from './DiffPanels'

function bundle(over: Partial<RunBundle>): RunBundle {
  return { run: undefined, summary: null, metrics: [], anomalies: [], syscalls: [], ...over }
}

const anomaly = (rule: string, sev = 'high') => ({
  id: rule, rule_id: rule, severity: sev, severity_score: 70, title: `${rule} title`,
  description: '', evidence_ids: [], first_seen_ms: 0, last_seen_ms: 1, occurrence_count: 1,
})

describe('AnomalyDiff', () => {
  it('splits anomalies into only-A / both / only-B by rule_id', () => {
    const a = bundle({ anomalies: [anomaly('mem_growth'), anomaly('fd_leak')] })
    const b = bundle({ anomalies: [anomaly('fd_leak'), anomaly('slow_syscall')] })
    render(<AnomalyDiff a={a} b={b} />)
    expect(screen.getByText('Only in A (1)')).toBeInTheDocument()
    expect(screen.getByText('In both (1)')).toBeInTheDocument()
    expect(screen.getByText('Only in B (1)')).toBeInTheDocument()
    expect(screen.getByText('mem_growth title')).toBeInTheDocument() // only in A
    expect(screen.getByText('slow_syscall title')).toBeInTheDocument() // only in B
  })
})

const sc = (name: string, count: number, avg: number) => ({
  syscall: name, count, total_ms: count * avg, avg_ms: avg,
  p50_ms: avg, p95_ms: avg, p99_ms: avg, errors: 0, pct_runtime: 0,
})

describe('SyscallDiff', () => {
  it('joins by syscall and shows Δ count sorted by magnitude', () => {
    const a = bundle({ syscalls: [sc('read', 100, 0.5), sc('openat', 10, 1)] })
    const b = bundle({ syscalls: [sc('read', 150, 0.4), sc('write', 5, 0.2)] })
    const { container } = render(<SyscallDiff a={a} b={b} />)
    const firstRowCells = container.querySelectorAll('tbody tr')[0].querySelectorAll('td')
    expect(firstRowCells[0].textContent).toBe('read') // |Δ|=50 largest
    expect(firstRowCells[3].textContent).toBe('+50')
    // openat only in A -> B count 0, Δ -10
    expect(screen.getByText('openat')).toBeInTheDocument()
  })
})
