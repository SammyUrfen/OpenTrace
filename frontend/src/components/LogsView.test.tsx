import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LogsView, type OutputChunk } from './LogsTab'
import type { Anomaly } from '../state/useRunDetail'

const chunks: OutputChunk[] = [
  { timestamp_ms: 100, stream: 'stdout', text: 'starting up\n' },
  { timestamp_ms: 500, stream: 'stderr', text: 'a warning\n' },
  { timestamp_ms: 900, stream: 'stdout', text: 'inside the leak window\n' },
]

const anomalies: Anomaly[] = [
  {
    id: 'a', rule_id: 'monotonic_memory_growth', severity: 'high', severity_score: 78,
    title: 'leak', description: 'd', evidence_ids: [],
    first_seen_ms: 800, last_seen_ms: 1000, occurrence_count: 1,
  },
]

describe('LogsView', () => {
  it('renders output, tints stderr, and marks anomaly-window lines', () => {
    const { container } = render(<LogsView chunks={chunks} anomalies={anomalies} />)
    expect(screen.getByText(/starting up/)).toBeInTheDocument()
    expect(container.querySelector('.logs__chunk--err')?.textContent).toContain('a warning')
    const anomalyChunk = container.querySelector('.logs__chunk--anomaly')
    expect(anomalyChunk?.textContent).toContain('inside the leak window')
    // the stdout line outside the window is not marked
    expect(container.querySelectorAll('.logs__chunk--anomaly').length).toBe(1)
  })
})
