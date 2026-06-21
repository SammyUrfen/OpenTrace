import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ProcessTable, type ProcRow } from './ProcessesTab'

const rows: ProcRow[] = [
  { pid: 100, parent_pid: null, command: '/usr/bin/python3', syscalls: 5000, first_ms: 0, last_ms: 3000, duration_ms: 3000, exited: false, ephemeral: false },
  { pid: 200, parent_pid: 100, command: '/bin/true', syscalls: 12, first_ms: 100, last_ms: 150, duration_ms: 50, exited: true, ephemeral: true },
]

describe('ProcessTable', () => {
  it('renders command basenames, parent, and ephemeral flag', () => {
    render(<ProcessTable rows={rows} />)
    expect(screen.getByText('python3')).toBeInTheDocument()
    expect(screen.getByText('true')).toBeInTheDocument()
    // 100 appears as pid (row 1) and parent (row 2)
    expect(screen.getAllByText('100').length).toBe(2)
    expect(screen.getByText(/ephemeral/)).toBeInTheDocument()
  })
})
