import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Run } from '../state/useOpenTrace'
import { ProfilingTab } from './ProfilingTab'
import { FlamegraphTab } from './FlamegraphTab'
import { runViews } from './RunView'

function mockFetchJson(payload: unknown) {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
  ) as unknown as typeof fetch
}

const PROFILE = {
  malloc: {
    supported: true, n_alloc: 282, n_free: 201,
    bytes_allocated: 1638400, bytes_freed: 983040,
    peak_live_bytes: 671744, outstanding_bytes: 655360, outstanding_blocks: 80,
    free_unmatched: 0,
    top_sizes: [{ size: 8192, count: 80 }],
    largest_live: [{ addr: '0x55a0', size: 8192 }],
  },
  hotspots: [
    { function: 'malloc', calls: 282, total_ms: 12.5, avg_ms: 0.044, errors: 0 },
    { function: 'free', calls: 201, total_ms: 5.1, avg_ms: 0.025, errors: 0 },
  ],
}

const FLAME = {
  supported: true,
  samples: 16736,
  tree: {
    name: 'all', value: 100,
    children: [
      { name: 'main', value: 100, children: [
        { name: 'work', value: 80, children: [
          { name: 'do_sin', value: 50, children: [] },
        ] },
      ] },
    ],
  },
  hotspots: [
    { function: 'do_sin', self: 50, total: 50, self_pct: 50, total_pct: 50 },
    { function: 'work', self: 30, total: 80, self_pct: 30, total_pct: 80 },
  ],
}

beforeEach(() => {
  mockFetchJson({})
})

describe('ProfilingTab', () => {
  it('renders the allocation ledger + library hotspots', async () => {
    mockFetchJson(PROFILE)
    render(<ProfilingTab backendUrl="http://x" runId="r1" />)
    expect(await screen.findByText('Allocation profile')).toBeInTheDocument()
    // stat labels + values
    expect(screen.getByText('allocations')).toBeInTheDocument()
    expect(screen.getByText('leaked (live at exit)')).toBeInTheDocument()
    expect(screen.getAllByText('282').length).toBeGreaterThanOrEqual(1) // alloc count
    expect(screen.getByText('0x55a0')).toBeInTheDocument() // largest live block
    // hotspot table rows
    expect(screen.getByText('malloc')).toBeInTheDocument()
    expect(screen.getByText('free')).toBeInTheDocument()
  })

  it('shows an empty state when the run has no ltrace profile', async () => {
    mockFetchJson({ malloc: { supported: false }, hotspots: [] })
    render(<ProfilingTab backendUrl="http://x" runId="r1" />)
    expect(await screen.findByText(/Enable the/)).toBeInTheDocument()
    expect(screen.getByText('Library calls')).toBeInTheDocument()
  })
})

describe('FlamegraphTab', () => {
  it('renders frames + the symbol hotspot table, and zooms', async () => {
    mockFetchJson(FLAME)
    render(<FlamegraphTab backendUrl="http://x" runId="r1" />)
    expect(await screen.findByText(/16,736 samples/)).toBeInTheDocument()
    // a frame label + a hotspot row both say do_sin -> at least one present
    expect(screen.getAllByText('do_sin').length).toBeGreaterThanOrEqual(1)
    // clicking a frame reveals the reset-zoom control
    fireEvent.click(screen.getAllByText('work')[0])
    expect(await screen.findByText('reset zoom')).toBeInTheDocument()
  })

  it('shows an empty state when the run has no perf capture', async () => {
    mockFetchJson({ supported: false, samples: 0, tree: null, hotspots: [], reason: 'no samples' })
    render(<FlamegraphTab backendUrl="http://x" runId="r1" />)
    expect(await screen.findByText(/no samples/)).toBeInTheDocument()
  })
})

describe('runViews', () => {
  const base: Run = {
    id: 'r', session_id: 's', terminal_id: null, display_name: 'd', command: 'c',
    command_basename: 'c', cwd: '/', started_at: 0, ended_at: null, duration_ms: null,
    exit_code: null, exit_signal: null, status: 'completed', label: null,
    max_severity: null, collector_config: null, created_at: 0,
  }
  const keys = (r: Run) => runViews(r).map((v) => v.key)

  it('hides profiling/flamegraph by default (full strace set incl. Logs)', () => {
    expect(keys(base)).not.toContain('profiling')
    expect(keys(base)).not.toContain('flamegraph')
    expect(keys(base)).toContain('logs')
    expect(keys(base)).toContain('syscalls')
  })
  it('shows Profiling for ltrace runs, but drops Logs (no write capture)', () => {
    const k = keys({ ...base, collector_config: { ltrace: true } })
    expect(k).toContain('profiling')
    expect(k).toContain('syscalls') // ltrace still yields @SYS syscalls
    expect(k).not.toContain('logs')
  })
  it('shows only metrics + Flamegraph for a perf-only run (no syscall tabs)', () => {
    const k = keys({ ...base, collector_config: { perf: true } })
    expect(k).toContain('flamegraph')
    expect(k).not.toContain('syscalls')
    expect(k).not.toContain('logs')
    expect(k).not.toContain('profiling')
  })
})
