import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { IoTable, type IoRow } from './IoTab'

const rows: IoRow[] = [
  { path: '/data/big.log', opens: 1, closes: 0, reads: 0, writes: 500, read_bytes: 0, write_bytes: 5 * 1024 * 1024, leaked: 1 },
  { path: '/etc/config.yaml', opens: 3, closes: 3, reads: 6, writes: 0, read_bytes: 2048, write_bytes: 0, leaked: 0 },
]

describe('IoTable', () => {
  it('renders file basenames, byte totals, and a leak marker', () => {
    render(<IoTable rows={rows} />)
    expect(screen.getByText('big.log')).toBeInTheDocument()
    expect(screen.getByText('config.yaml')).toBeInTheDocument()
    expect(screen.getByText('5.0 MB')).toBeInTheDocument() // write bytes
    expect(screen.getByText('2.0 KB')).toBeInTheDocument() // read bytes
    // leaked file shows the ⊘ marker
    expect(screen.getByTitle('1 fd(s) never closed')).toBeInTheDocument()
  })
})
