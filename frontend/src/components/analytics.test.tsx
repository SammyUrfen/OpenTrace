import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Run } from '../state/useOpenTrace'
import type { RunDetail } from '../state/useRunDetail'
import { OverviewTab } from './OverviewTab'
import { MainTabs } from './MainTabs'
import { MemoryTab } from './MemoryTab'
import { CpuTab } from './CpuTab'
import { TimeSeriesChart } from './TimeSeriesChart'

function mkRun(over: Partial<Run> = {}): Run {
  return {
    id: 'r1', session_id: 's1', terminal_id: 't1',
    display_name: 'python_20260621_120000', command: 'python3 train.py',
    command_basename: 'python3', cwd: '/home/u/demo', started_at: 1_700_000_000_000,
    ended_at: 1_700_000_002_500, duration_ms: 2500, exit_code: 0, exit_signal: null,
    status: 'completed', label: null, max_severity: 'high',
    collector_config: null, created_at: 1_700_000_000_000,
    ...over,
  }
}

const baseDetail: RunDetail = {
  loading: false,
  metrics: [
    { timestamp_ms: 1, cpu_pct: 40, rss_mb: 80, vms_mb: 200, open_fds: 10, threads: 2, syscall_rate: 100, io_read_bps: 0, io_write_bps: 0 },
    { timestamp_ms: 2, cpu_pct: 90, rss_mb: 250, vms_mb: 400, open_fds: 14, threads: 3, syscall_rate: 50, io_read_bps: 0, io_write_bps: 0 },
  ],
  anomalies: [
    {
      id: 'a1', rule_id: 'monotonic_memory_growth', severity: 'high', severity_score: 78,
      title: 'Memory grew 80MB → 250MB', description: 'RSS climbed monotonically.',
      evidence_ids: [], first_seen_ms: 1, last_seen_ms: 2, occurrence_count: 170,
    },
  ],
  summary: {
    totals: { syscall_events: 12345, errors: 3, signals: 1, metric_samples: 2, top_syscalls: [['openat', 500], ['read', 300]] },
    peaks: { rss_mb: 250, cpu_pct: 90, open_fds: 14, threads: 3 },
    averages: { cpu_pct: 65, rss_mb: 165 },
    anomalies: [], max_severity: 'high',
  },
}

describe('OverviewTab', () => {
  it('renders command, findings, and the execution snapshot', () => {
    render(<OverviewTab run={mkRun()} detail={baseDetail} live={null} backendUrl="http://x" onOpenSettings={() => {}} />)
    expect(screen.getByText('python3 train.py')).toBeInTheDocument()
    // anomaly card
    expect(screen.getByText('Memory grew 80MB → 250MB')).toBeInTheDocument()
    expect(screen.getByText('HIGH')).toBeInTheDocument()
    expect(screen.getByText('×170')).toBeInTheDocument()
    // snapshot stats
    expect(screen.getByText('90%')).toBeInTheDocument() // peak CPU
    expect(screen.getByText('250 MB')).toBeInTheDocument() // peak RSS
    expect(screen.getByText('12,345')).toBeInTheDocument() // syscalls
    // top syscalls
    expect(screen.getByText('openat')).toBeInTheDocument()
  })

  it('shows a clean message when there are no anomalies', () => {
    const clean: RunDetail = { ...baseDetail, anomalies: [], summary: { ...baseDetail.summary!, anomalies: [] } }
    render(<OverviewTab run={mkRun({ max_severity: 'clean' })} detail={clean} live={null} backendUrl="http://x" onOpenSettings={() => {}} />)
    expect(screen.getByText(/no anomalies detected/i)).toBeInTheDocument()
  })
})

describe('MainTabs', () => {
  const tabs = [
    { key: 'run:r1', label: 'python_20260621_120000', dotColor: '#fb923c' },
    { key: 'diff:r1:r2', label: 'python ↔ node', diff: true },
  ]

  it('renders run + diff tabs and fires select/close by key', () => {
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<MainTabs tabs={tabs} activeKey="run:r1" onSelect={onSelect} onClose={onClose} />)
    expect(screen.getByText('python_20260621_120000')).toBeInTheDocument()
    expect(screen.getByText('python ↔ node')).toBeInTheDocument()
    fireEvent.click(screen.getByText('python ↔ node'))
    expect(onSelect).toHaveBeenCalledWith('diff:r1:r2')
    fireEvent.click(screen.getAllByLabelText('close tab')[0])
    expect(onClose).toHaveBeenCalledWith('run:r1')
  })

  it('shows a hint when no tabs are open', () => {
    render(<MainTabs tabs={[]} activeKey={null} onSelect={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByText(/click a run in the sidebar/i)).toBeInTheDocument()
  })
})

describe('MemoryTab', () => {
  it('renders a chart and a leak banner when growth is detected', () => {
    const { container } = render(<MemoryTab detail={baseDetail} />)
    expect(container.querySelector('svg.ts-chart')).toBeInTheDocument()
    expect(screen.getByText(/possible leak/i)).toBeInTheDocument()
    expect(screen.getByText('250 MB')).toBeInTheDocument() // peak RSS stat
  })

  it('omits the banner when there is no memory-growth anomaly', () => {
    const noGrowth: RunDetail = { ...baseDetail, anomalies: [] }
    render(<MemoryTab detail={noGrowth} />)
    expect(screen.queryByText(/possible leak/i)).not.toBeInTheDocument()
  })
})

describe('TimeSeriesChart robustness', () => {
  it('shows "no samples" for empty/single-point data without crashing', () => {
    const { rerender, container } = render(<TimeSeriesChart series={[{ name: 'x', color: '#f00', points: [] }]} />)
    expect(screen.getByText('no samples')).toBeInTheDocument()
    rerender(<TimeSeriesChart series={[{ name: 'x', color: '#f00', points: [[1, 5]] }]} />)
    expect(container.querySelector('svg')).toBeInTheDocument()
  })

  it('filters NaN/Infinity points so paths never contain NaN', () => {
    const { container } = render(
      <TimeSeriesChart
        series={[{ name: 'x', color: '#f00', area: true, points: [[1, 10], [NaN, 20], [3, Infinity], [4, 40]] }]}
      />,
    )
    const paths = Array.from(container.querySelectorAll('path'))
    for (const p of paths) {
      expect(p.getAttribute('d') || '').not.toMatch(/NaN|Infinity/)
    }
  })
})

describe('CpuTab', () => {
  it('renders CPU + syscall charts with threshold lines', () => {
    const { container } = render(<CpuTab detail={baseDetail} />)
    const charts = container.querySelectorAll('svg.ts-chart')
    expect(charts.length).toBe(2) // CPU + syscall-rate
    expect(screen.getByText('90%')).toBeInTheDocument() // threshold label
    expect(screen.getByText(/compute-bound/i)).toBeInTheDocument()
  })
})
