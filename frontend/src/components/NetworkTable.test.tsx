import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { NetworkTable, type NetConn } from './NetworkTab'

const rows: NetConn[] = [
  { family: 'AF_INET', address: '192.0.2.1', port: 12345, result: 'timed out', latency_ms: 2503, pid: 1 },
  { family: 'AF_INET', address: '10.0.0.5', port: 443, result: 'ok', latency_ms: 12.4, pid: 1 },
  { family: 'AF_UNIX', address: '/run/x.sock', port: null, result: 'ok', latency_ms: 0.3, pid: 1 },
]

describe('NetworkTable', () => {
  it('renders destinations, results, and humanized latency', () => {
    render(<NetworkTable rows={rows} />)
    expect(screen.getByText('192.0.2.1:12345')).toBeInTheDocument()
    expect(screen.getByText('10.0.0.5:443')).toBeInTheDocument()
    expect(screen.getByText('/run/x.sock')).toBeInTheDocument()
    expect(screen.getByText('timed out')).toBeInTheDocument()
    expect(screen.getByText('2.50s')).toBeInTheDocument() // >=1s shown in seconds
    expect(screen.getByText('12.4ms')).toBeInTheDocument()
  })
})
