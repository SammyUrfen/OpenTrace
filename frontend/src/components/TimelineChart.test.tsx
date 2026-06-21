import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import { TimelineChart, type TlEvent, type TlAnomaly } from './TimelineChart'

const events: TlEvent[] = [
  { t: 100, kind: 'error', label: 'openat → ENOENT' },
  { t: 500, kind: 'signal', label: 'SIGCHLD' },
  { t: 900, kind: 'slow', label: 'fsync took 1200ms' },
]
const anomalies: TlAnomaly[] = [{ t0: 200, t1: 800, color: '#f87171', title: 'HIGH: leak' }]

describe('TimelineChart', () => {
  it('renders metric paths, event markers, and anomaly bands', () => {
    const { container } = render(
      <TimelineChart
        t0={0}
        t1={1000}
        domain={[0, 1000]}
        onDomain={vi.fn()}
        rss={[[0, 50], [500, 120], [1000, 200]]}
        cpu={[[0, 10], [500, 80], [1000, 5]]}
        syscalls={[[0, 100], [500, 0]]}
        events={events}
        anomalies={anomalies}
        onHover={vi.fn()}
      />,
    )
    expect(container.querySelector('svg.tl-chart')).toBeInTheDocument()
    // 3 metric line paths
    expect(container.querySelectorAll('path').length).toBe(3)
    // 3 event circles
    expect(container.querySelectorAll('circle.tl-event').length).toBe(3)
    // anomaly band(s): shaded rect + lane bar => at least 2 rects with the color
    const fillRects = Array.from(container.querySelectorAll('rect')).filter(
      (r) => r.getAttribute('fill') === '#f87171',
    )
    expect(fillRects.length).toBeGreaterThanOrEqual(2)
  })

  it('zooms in on wheel-up (shrinks the domain)', () => {
    const onDomain = vi.fn()
    const { container } = render(
      <TimelineChart t0={0} t1={1000} domain={[0, 1000]} onDomain={onDomain}
        rss={[[0, 1], [1000, 2]]} cpu={[]} syscalls={[]} events={[]} anomalies={[]} onHover={vi.fn()} />,
    )
    const svg = container.querySelector('svg.tl-chart')!
    svg.dispatchEvent(new WheelEvent('wheel', { deltaY: -100, bubbles: true }))
    expect(onDomain).toHaveBeenCalled()
    const [n0, n1] = onDomain.mock.calls[0][0]
    expect(n1 - n0).toBeLessThan(1000) // zoomed in
  })
})
