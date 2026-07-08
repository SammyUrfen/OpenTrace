/** Labelled stat tile used by the Overview/Memory/Profiling grids. */
export function StatCell({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="stat-cell">
      <div className={`stat-cell__value${warn ? ' stat-cell__value--warn' : ''}`}>{value}</div>
      <div className="stat-cell__label">{label}</div>
    </div>
  )
}
