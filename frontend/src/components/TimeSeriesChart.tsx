export interface Series {
  name: string
  color: string
  /** [timestampMs, value] points; nulls should be pre-filtered by the caller. */
  points: [number, number][]
  /** Draw a faint area under this line. */
  area?: boolean
}

export interface Threshold {
  value: number
  label: string
  color: string
}

interface Props {
  series: Series[]
  thresholds?: Threshold[]
  height?: number
  yMax?: number
  yUnit?: string
}

const PAD_L = 44
const PAD_R = 12
const PAD_T = 10
const PAD_B = 22

/**
 * Dependency-free multi-series time-series line chart (SVG). Auto-scales to the
 * max across all series (or an explicit `yMax`), draws optional threshold lines,
 * and labels the y-max and elapsed-time axis. Responsive width via viewBox.
 */
export function TimeSeriesChart({
  series,
  thresholds = [],
  height = 220,
  yMax,
  yUnit = '',
}: Props) {
  const W = 600
  const H = height
  const fin = (pts: [number, number][]) =>
    pts.filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]))
  // Pre-filter every series to finite points so a stray NaN/Infinity can never
  // poison the axis scaling or produce a degenerate SVG path.
  const cleaned = series.map((s) => ({ ...s, points: fin(s.points) }))
  const allPts = cleaned.flatMap((s) => s.points)
  const hasData = allPts.length >= 2

  const tMin = hasData ? Math.min(...allPts.map((p) => p[0])) : 0
  const tMax = hasData ? Math.max(...allPts.map((p) => p[0])) : 1
  const tSpan = tMax - tMin || 1
  const vMaxRaw = Math.max(
    yMax ?? 0,
    ...allPts.map((p) => p[1]),
    ...thresholds.map((t) => t.value),
    1,
  )
  const vMax = yMax ?? vMaxRaw * 1.1

  const x = (t: number) => PAD_L + ((t - tMin) / tSpan) * (W - PAD_L - PAD_R)
  const y = (v: number) => H - PAD_B - (v / vMax) * (H - PAD_T - PAD_B)

  const line = (pts: [number, number][]) =>
    pts.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')

  const elapsed = (tSpan / 1000).toFixed(1)

  return (
    <svg
      className="ts-chart"
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      preserveAspectRatio="none"
      role="img"
    >
      {/* y gridlines: 0, mid, max */}
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line
            x1={PAD_L}
            x2={W - PAD_R}
            y1={y(vMax * f)}
            y2={y(vMax * f)}
            stroke="var(--border)"
            strokeWidth={0.5}
          />
          <text x={PAD_L - 6} y={y(vMax * f) + 3} textAnchor="end" className="ts-axis">
            {(vMax * f).toFixed(0)}
          </text>
        </g>
      ))}
      {/* thresholds */}
      {hasData &&
        thresholds.map((t) => (
          <g key={t.label}>
            <line
              x1={PAD_L}
              x2={W - PAD_R}
              y1={y(t.value)}
              y2={y(t.value)}
              stroke={t.color}
              strokeWidth={1}
              strokeDasharray="4 3"
              opacity={0.7}
            />
            <text x={W - PAD_R} y={y(t.value) - 3} textAnchor="end" className="ts-axis" fill={t.color}>
              {t.label}
            </text>
          </g>
        ))}
      {/* series (each already finite-filtered; skip degenerate <2-point lines) */}
      {hasData &&
        cleaned.map((s) =>
          s.points.length < 2 ? null : (
            <g key={s.name}>
              {s.area && (
                <path
                  d={`${line(s.points)} L${x(s.points[s.points.length - 1][0]).toFixed(1)},${y(0).toFixed(1)} L${x(s.points[0][0]).toFixed(1)},${y(0).toFixed(1)} Z`}
                  fill={s.color}
                  opacity={0.12}
                />
              )}
              <path d={line(s.points)} fill="none" stroke={s.color} strokeWidth={1.6} />
            </g>
          ),
        )}
      {/* x axis labels */}
      <text x={PAD_L} y={H - 6} className="ts-axis">0s</text>
      <text x={W - PAD_R} y={H - 6} textAnchor="end" className="ts-axis">{elapsed}s</text>
      {yUnit && (
        <text x={PAD_L - 6} y={PAD_T + 2} textAnchor="end" className="ts-axis">{yUnit}</text>
      )}
      {!hasData && (
        <text x={W / 2} y={H / 2} textAnchor="middle" className="ts-axis">
          no samples
        </text>
      )}
    </svg>
  )
}
