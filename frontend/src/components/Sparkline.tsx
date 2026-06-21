interface Props {
  data: number[]
  width?: number
  height?: number
  color?: string
}

/** Minimal dependency-free SVG sparkline. Auto-scales to its own max. */
export function Sparkline({ data, width = 96, height = 22, color = '#c084fc' }: Props) {
  if (data.length < 2) {
    return <svg width={width} height={height} className="sparkline" />
  }
  const max = Math.max(...data, 1)
  const min = Math.min(...data, 0)
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
