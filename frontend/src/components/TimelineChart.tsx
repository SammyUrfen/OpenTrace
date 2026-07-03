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
    const xy = series
      .filter((p) => Number.isFinite(p[1]))
      .map((p) => [x(p[0]), top + lane.h - (p[1] / vmax) * (lane.h - 3)] as [number, number])
    const d = xy.length ? `M${xy.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' L')}` : ''
    const base = top + lane.h
    const area =
      xy.length >= 2
        ? `${d} L${xy[xy.length - 1][0].toFixed(1)},${base} L${xy[0][0].toFixed(1)},${base} Z`
        : ''
    return { d, area, vmax }
  }

  const mem = line(rss, 'memory')
  const cpuL = line(cpu, 'cpu')
  const sysL = line(syscalls, 'syscalls')

  // Per-lane peak value, shown under each lane label so the shapes read quantitatively.
  const kfmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : n.toFixed(0))
  const peak: Record<string, string> = {
    memory: rss.length ? `${mem.vmax.toFixed(0)} MB` : '',
    cpu: cpu.length ? `${cpuL.vmax.toFixed(0)}%` : '',
    syscalls: syscalls.length ? `${kfmt(sysL.vmax)}/s` : '',
  }

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
      {/* clip each metric lane so lines/areas never bleed into neighbours */}
      <defs>
        {(['memory', 'cpu', 'syscalls'] as const).map((k) => (
          <clipPath key={k} id={`tl-clip-${k}`}>
            <rect x={GUTTER} y={tops[k]} width={plotW} height={LANES.find((l) => l.key === k)!.h} />
          </clipPath>
        ))}
      </defs>

      {/* faint lane bands so each row reads as a distinct lane */}
      {LANES.map((l, i) =>
        i % 2 === 1 ? (
          <rect
            key={`band-${l.key}`}
            x={GUTTER}
            y={tops[l.key]}
            width={plotW}
            height={l.h}
            className="tl-band"
          />
        ) : null,
      )}

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

      {/* lane labels (+ peak value) + separators */}
      {LANES.map((l) => (
        <g key={l.key}>
          <text
            x={GUTTER - 8}
            y={tops[l.key] + l.h / 2 + (peak[l.key] ? -1 : 3)}
            textAnchor="end"
            className="tl-label"
          >
            {l.label}
          </text>
          {peak[l.key] && (
            <text x={GUTTER - 8} y={tops[l.key] + l.h / 2 + 11} textAnchor="end" className="tl-peak">
              {peak[l.key]}
            </text>
          )}
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

      {/* metric lanes (area + line), clipped so nothing bleeds across lanes */}
      <g clipPath="url(#tl-clip-memory)">
        <path d={mem.area} fill="#c084fc" opacity={0.1} />
        <path d={mem.d} fill="none" stroke="#c084fc" strokeWidth={1.4} />
      </g>
      <g clipPath="url(#tl-clip-cpu)">
        <path d={cpuL.area} fill="#60a5fa" opacity={0.1} />
        <path d={cpuL.d} fill="none" stroke="#60a5fa" strokeWidth={1.4} />
      </g>
      <g clipPath="url(#tl-clip-syscalls)">
        <path d={sysL.area} fill="#34d399" opacity={0.1} />
        <path d={sysL.d} fill="none" stroke="#34d399" strokeWidth={1.4} />
      </g>

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

      {/* x axis: left = visible-window start, middle = midpoint, right = end (all relative to t0) */}
      <text x={GUTTER} y={H - 5} className="tl-axis">
        {((d0 - t0) / 1000).toFixed(1)}s
      </text>
      <text x={(GUTTER + W - PAD_R) / 2} y={H - 5} textAnchor="middle" className="tl-axis">
        {(((d0 + d1) / 2 - t0) / 1000).toFixed(1)}s
      </text>
      <text x={W - PAD_R} y={H - 5} textAnchor="end" className="tl-axis">
        {((d1 - t0) / 1000).toFixed(1)}s
      </text>
    </svg>
  )
}
