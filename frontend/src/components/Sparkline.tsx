import { downsampleValues, maxOf, minOf } from './seriesUtils'

interface Props {
  data: number[]
  width?: number
  height?: number
  color?: string
}

/** Minimal dependency-free SVG sparkline. Auto-scales to its own max; long
 *  metric series are decimated so a multi-hour run can't bloat the polyline. */
export function Sparkline({ data: raw, width = 96, height = 22, color = '#c084fc' }: Props) {
  const data = downsampleValues(raw)
  if (data.length < 2) {
    return <svg width={width} height={height} className="sparkline" />
  }
  const max = Math.max(maxOf(data, (v) => v) ?? 1, 1)
  const min = Math.min(minOf(data, (v) => v) ?? 0, 0)
  const span = max - min || 1
  const step = width / (data.length - 1)
  const points = data
    .map((v, i) => {
      const x = i * step
      const y = height - ((v - min) / span) * (height - 2) - 1
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <svg width={width} height={height} className="sparkline" preserveAspectRatio="none">
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}
