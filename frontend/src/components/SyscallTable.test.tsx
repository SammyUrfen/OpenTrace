import { describe, expect, it } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { SyscallTable } from './SyscallTable'
import type { SyscallStat } from '../state/useSyscalls'

const rows: SyscallStat[] = [
  { syscall: 'openat', count: 100, total_ms: 50, avg_ms: 0.5, p50_ms: 0.4, p95_ms: 1.2, p99_ms: 2.0, errors: 3, pct_runtime: 40 },
  { syscall: 'read', count: 200, total_ms: 75, avg_ms: 0.37, p50_ms: 0.3, p95_ms: 0.9, p99_ms: 1.5, errors: 0, pct_runtime: 60 },
]

function bodyRowNames(): string[] {
  const table = screen.getByTestId('syscall-table')
  const body = table.querySelectorAll('tbody tr')
  return Array.from(body).map((tr) => within(tr as HTMLElement).getAllByRole('cell')[0].textContent || '')
}

describe('SyscallTable', () => {
  it('renders rows and defaults to total_ms descending', () => {
    render(<SyscallTable rows={rows} />)
    expect(bodyRowNames()).toEqual(['read', 'openat']) // read total 75 > openat 50
    expect(screen.getByText('100')).toBeInTheDocument()
  })

  it('clicking a header sorts by that column; clicking again flips', () => {
    render(<SyscallTable rows={rows} />)
    fireEvent.click(screen.getByText('count')) // desc by count
    expect(bodyRowNames()).toEqual(['read', 'openat']) // 200 > 100
    fireEvent.click(screen.getByText('count')) // asc
    expect(bodyRowNames()).toEqual(['openat', 'read'])
  })

  it('sorts the syscall name column alphabetically', () => {
    render(<SyscallTable rows={rows} />)
    fireEvent.click(screen.getByText('syscall')) // first click -> desc
    expect(bodyRowNames()).toEqual(['read', 'openat'])
    fireEvent.click(screen.getByText('syscall')) // asc
    expect(bodyRowNames()).toEqual(['openat', 'read'])
  })
})
