import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { RequestsTab } from './RequestsTab'
import type { Requests } from '../state/useOpenTrace'

// The tab fetches via useRunObject; stub it so tests drive purely off the `live` prop.
vi.mock('../state/useRunObject', () => ({
  useRunObject: () => ({ data: null, loading: false }),
}))

function rollup(over: Partial<Requests> = {}): Requests {
  return {
    available: true,
    reason: null,
    window_s: 20,
    engine: 'bpftrace',
    request_count: 23,
    db_span_count: 46,
    endpoints: [
      { method: 'GET', route: '/fast', count: 12, p50_ms: 3, p95_ms: 6, p99_ms: 8, err_pct: 0, db_ms_share: 0.1 },
      { method: 'GET', route: '/slow', count: 11, p50_ms: 600, p95_ms: 608, p99_ms: 610, err_pct: 0, db_ms_share: 0.99 },
      { method: 'POST', route: '/checkout', count: 4, p50_ms: 20, p95_ms: 40, p99_ms: 50, err_pct: 50, db_ms_share: 0 },
    ],
    spans: [],
    ...over,
  }
}

function routeOrder(): string[] {
  return Array.from(screen.getAllByTestId('endpoint-row')).map((tr) => tr.getAttribute('data-route') || '')
}

describe('RequestsTab', () => {
  it('renders the endpoint RED table sorted by p95 desc, with the DB share', () => {
    render(<RequestsTab backendUrl="x" runId="r" live={rollup()} />)
    expect(screen.getByTestId('endpoint-table')).toBeInTheDocument()
    expect(routeOrder()).toEqual(['/slow', '/checkout', '/fast']) // p95 608 > 40 > 6
    expect(screen.getByText('99%')).toBeInTheDocument()           // /slow db share
    expect(screen.getByText('50%')).toBeInTheDocument()           // /checkout err%
  })

  it('clicking a header re-sorts; clicking again flips direction', () => {
    render(<RequestsTab backendUrl="x" runId="r" live={rollup()} />)
    fireEvent.click(screen.getByText('count')) // desc by count
    expect(routeOrder()).toEqual(['/fast', '/slow', '/checkout']) // 12 > 11 > 4
    fireEvent.click(screen.getByText('count')) // asc
    expect(routeOrder()).toEqual(['/checkout', '/slow', '/fast'])
  })

  it('expands a row into a DB-vs-app breakdown on click', () => {
    render(<RequestsTab backendUrl="x" runId="r" live={rollup()} />)
    expect(screen.queryByTestId('endpoint-breakdown')).not.toBeInTheDocument()
    fireEvent.click(screen.getAllByTestId('endpoint-row')[0]) // /slow
    const bd = screen.getByTestId('endpoint-breakdown')
    expect(bd).toBeInTheDocument()
    expect(bd.textContent).toMatch(/99% DB/)
  })

  it('degrades the DB column to — when the run captured no DB spans', () => {
    const noDb = rollup({
      db_span_count: 0,
      endpoints: [
        { method: 'GET', route: '/x', count: 5, p50_ms: 2, p95_ms: 4, p99_ms: 5, err_pct: 0, db_ms_share: 0 },
      ],
      reason: 'DB spans unavailable — the target maps no dynamically-linked libpq.',
    })
    render(<RequestsTab backendUrl="x" runId="r" live={noDb} />)
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByText(/DB spans unavailable/)).toBeInTheDocument()
  })

  it('fail-open: no endpoints shows a friendly empty state, not a table', () => {
    const empty = rollup({
      request_count: 0, db_span_count: 0, endpoints: [],
      reason: 'No plaintext HTTP/1.x requests were observed on the target during the window.',
    })
    render(<RequestsTab backendUrl="x" runId="r" live={empty} />)
    expect(screen.queryByTestId('endpoint-table')).not.toBeInTheDocument()
    expect(screen.getByText(/No plaintext HTTP/)).toBeInTheDocument()
  })

  // --- Phase 2: off-CPU breakdown + waterfall + drill ---

  function withSpans(): Requests {
    return rollup({
      has_breakdown: true,
      endpoints: [
        { method: 'GET', route: '/slow', count: 11, p50_ms: 600, p95_ms: 608, p99_ms: 610,
          err_pct: 0, db_ms_share: 0.99,
          breakdown: { on_cpu_ms: 5, runq_ms: 1, db_wait_ms: 600, other_off_ms: 2,
            on_cpu_pct: 0.8, runq_pct: 0.2, db_wait_pct: 98.7, other_off_pct: 0.3, top_off_reason: 'sleep' } },
      ],
      spans: [
        { kind: 'http', method: 'GET', route: '/slow', name: 'GET /slow', status: 200, dur_ms: 608,
          tid: 100, db_ms: 600, start_ns: 0,
          breakdown: { on_cpu_ms: 5, runq_ms: 1, db_wait_ms: 600, other_off_ms: 2, off_reasons: { sleep: 2 } },
          db: [{ name: 'SELECT pg_sleep(?)', dur_ms: 600, start_ns: 5_000_000, statement: 'SELECT pg_sleep(?)' }] },
      ],
    })
  }

  it('shows no Requests toggle when nothing was sampled', () => {
    render(<RequestsTab backendUrl="x" runId="r" live={rollup()} />)  // spans: []
    expect(screen.queryByRole('tab', { name: 'Requests' })).not.toBeInTheDocument()
  })

  it('toggles to the per-request waterfall when requests were sampled', () => {
    render(<RequestsTab backendUrl="x" runId="r" live={withSpans()} />)
    fireEvent.click(screen.getByRole('tab', { name: 'Requests' }))
    expect(screen.getByTestId('request-waterfall')).toBeInTheDocument()
    expect(screen.getByTestId('waterfall-row')).toHaveAttribute('data-route', '/slow')
  })

  it('expands a waterfall row into breakdown + SQL + the off-CPU drill', () => {
    render(<RequestsTab backendUrl="x" runId="r" live={withSpans()} />)
    fireEvent.click(screen.getByRole('tab', { name: 'Requests' }))
    fireEvent.click(screen.getByTestId('waterfall-row'))
    const detail = screen.getByTestId('waterfall-detail')
    expect(within(detail).getByTestId('request-breakdown')).toBeInTheDocument()
    expect(detail.textContent).toMatch(/pg_sleep/)              // captured SQL shown
    expect(within(detail).getByTestId('span-flame')).toBeInTheDocument()  // drill mounted
  })

  it('endpoint expand shows the full on/off/DB/run-queue decomposition when captured', () => {
    render(<RequestsTab backendUrl="x" runId="r" live={withSpans()} />)
    fireEvent.click(screen.getAllByTestId('endpoint-row')[0])   // /slow (sorted first)
    const bd = screen.getByTestId('endpoint-breakdown')
    expect(within(bd).getByTestId('request-breakdown')).toBeInTheDocument()
    expect(bd.textContent).toMatch(/DB-wait/)                   // decomposition chip
  })
})
