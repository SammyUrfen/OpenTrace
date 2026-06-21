import { useRef } from 'react'

export interface TlEvent {
  t: number
  kind: 'error' | 'signal' | 'slow' | 'exec'
  label: string
}
export interface TlAnomaly {
  t0: number
  t1: number
  color: string
  title: string
}

interface Props {
  t0: number
  t1: number
  domain: [number, number]
  onDomain: (d: [number, number]) => void
  rss: [number, number][]
  cpu: [number, number][]
  syscalls: [number, number][]
  events: TlEvent[]
  anomalies: TlAnomaly[]
  onHover: (e: TlEvent | null, x: number, y: number) => void
}

const GUTTER = 66
const PAD_R = 14
const PAD_T = 6
const PAD_B = 20

// lane order + heights (px)
const LANES = [
  { key: 'anomaly', label: 'Anomalies', h: 16 },
  { key: 'memory', label: 'Memory', h: 46 },
  { key: 'cpu', label: 'CPU', h: 46 },
  { key: 'syscalls', label: 'Syscalls/s', h: 40 },
  { key: 'events', label: 'Events', h: 30 },
] as const

const EVENT_COLOR: Record<TlEvent['kind'], string> = {
  error: '#f87171',
  signal: '#fbbf24',
  slow: '#60a5fa',
  exec: '#34d399',
}

const W = 900
const H = PAD_T + LANES.reduce((s, l) => s + l.h, 0) + PAD_B

export function TimelineChart(props: Props) {
  const { t0, t1, domain, onDomain, rss, cpu, syscalls, events, anomalies, onHover } = props
  const [d0, d1] = domain
  const span = d1 - d0 || 1
  const plotW = W - GUTTER - PAD_R
  const pan = useRef<{ startX: number; d0: number; d1: number } | null>(null)

  const x = (t: number) => GUTTER + ((t - d0) / span) * plotW
  const invX = (px: number) => d0 + ((px - GUTTER) / plotW) * span

  // lane vertical offsets
  const tops: Record<string, number> = {}
  let y = PAD_T
  for (const l of LANES) {
    tops[l.key] = y
    y += l.h
  }
  const bottomOfEvents = tops.events + LANES[LANES.length - 1].h

  const line = (series: [number, number][], laneKey: string) => {
    const lane = LANES.find((l) => l.key === laneKey)!
    const top = tops[laneKey]
    const vmax = Math.max(1, ...series.map((p) => p[1]))
    const pts = series
      .filter((p) => Number.isFinite(p[1]))
      .map((p) => `${x(p[0]).toFixed(1)},${(top + lane.h - (p[1] / vmax) * (lane.h - 2)).toFixed(1)}`)
    return { d: pts.length ? `M${pts.join(' L')}` : '', vmax }
  }

  const mem = line(rss, 'memory')
  const cpuL = line(cpu, 'cpu')
  const sysL = line(syscalls, 'syscalls')

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault()
    const tc = invX(e.nativeEvent.offsetX)
    const factor = e.deltaY < 0 ? 0.8 : 1.25
    let n0 = tc - (tc - d0) * factor
    let n1 = tc + (d1 - tc) * factor
    n0 = Math.max(t0, n0)
    n1 = Math.min(t1, n1)
    if (n1 - n0 > 5) onDomain([n0, n1])
  }

  return (
    <svg
      className="tl-chart"
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      preserveAspectRatio="none"
      onWheel={onWheel}
      onMouseDown={(e) => {
        pan.current = { startX: e.clientX, d0, d1 }
      }}
      onMouseMove={(e) => {
        if (!pan.current) return
        const dxPx = e.clientX - pan.current.startX
        const dt = (dxPx / plotW) * (pan.current.d1 - pan.current.d0)
        let n0 = pan.current.d0 - dt
        let n1 = pan.current.d1 - dt
        if (n0 < t0) {
          n1 += t0 - n0
          n0 = t0
        }
        if (n1 > t1) {
          n0 -= n1 - t1
          n1 = t1
        }
        onDomain([Math.max(t0, n0), Math.min(t1, n1)])
      }}
      onMouseUp={() => (pan.current = null)}
      onMouseLeave={() => {
        pan.current = null
        onHover(null, 0, 0)
      }}
    >
      {/* anomaly windows shaded across all lanes (behind everything) */}
      {anomalies.map((a, i) => (
        <rect
          key={i}
          x={x(a.t0)}
          y={PAD_T}
          width={Math.max(1.5, x(a.t1) - x(a.t0))}
          height={bottomOfEvents - PAD_T}
          fill={a.color}
          opacity={0.13}
        >
          <title>{a.title}</title>
        </rect>
      ))}

      {/* lane labels + separators */}
      {LANES.map((l) => (
        <g key={l.key}>
          <text x={GUTTER - 8} y={tops[l.key] + l.h / 2 + 3} textAnchor="end" className="tl-label">
            {l.label}
          </text>
          <line x1={GUTTER} x2={W - PAD_R} y1={tops[l.key] + l.h} y2={tops[l.key] + l.h}
            stroke="var(--border)" strokeWidth={0.5} />
        </g>
      ))}

      {/* anomaly lane: bars */}
      {anomalies.map((a, i) => (
        <rect key={`ab-${i}`} x={x(a.t0)} y={tops.anomaly + 3}
          width={Math.max(2, x(a.t1) - x(a.t0))} height={LANES[0].h - 6}
          fill={a.color} rx={2} />
      ))}

      {/* metric lanes */}
      <path d={mem.d} fill="none" stroke="#c084fc" strokeWidth={1.4} />
      <path d={cpuL.d} fill="none" stroke="#60a5fa" strokeWidth={1.4} />
      <path d={sysL.d} fill="none" stroke="#34d399" strokeWidth={1.4} />

      {/* events lane: markers */}
      {events.map((ev, i) => {
        if (ev.t < d0 || ev.t > d1) return null
        const cx = x(ev.t)
        const cy = tops.events + LANES[4].h / 2
        return (
          <circle
            key={i}
            cx={cx}
            cy={cy}
            r={3.2}
            fill={EVENT_COLOR[ev.kind]}
            className="tl-event"
            onMouseEnter={(e) => onHover(ev, e.clientX, e.clientY)}
            onMouseLeave={() => onHover(null, 0, 0)}
          />
        )
      })}

      {/* x axis */}
      <text x={GUTTER} y={H - 5} className="tl-axis">0.0s</text>
      <text x={W - PAD_R} y={H - 5} textAnchor="end" className="tl-axis">
        {((d1 - t0) / 1000).toFixed(1)}s
      </text>
      <text x={(GUTTER + W - PAD_R) / 2} y={H - 5} textAnchor="middle" className="tl-axis">
        {((d0 - t0) / 1000).toFixed(1)}s –
      </text>
    </svg>
  )
}
