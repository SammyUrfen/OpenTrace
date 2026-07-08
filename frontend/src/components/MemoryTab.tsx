import { useMemo } from 'react'
import type { RunDetail } from '../state/useRunDetail'
import { maxOf, pts } from './seriesUtils'
import { StatCell } from './StatCell'
import { TimeSeriesChart } from './TimeSeriesChart'

export function MemoryTab({ detail }: { detail: RunDetail }) {
  const { metrics, anomalies } = detail
  const rss = useMemo(() => pts(metrics, 'rss_mb'), [metrics])
  const vms = useMemo(() => pts(metrics, 'vms_mb'), [metrics])
  const growth = anomalies.find((a) => a.rule_id === 'monotonic_memory_growth')
  const peakRss = maxOf(rss, (p) => p[1])
  const peakVms = maxOf(vms, (p) => p[1])
  const avgRss = rss.length ? rss.reduce((s, p) => s + p[1], 0) / rss.length : null

  return (
    <div className="overview" data-testid="memory-tab">
      <h3 className="overview__h">Memory over time</h3>
      {growth && (
        <div className="banner banner--warn">⚠ {growth.title} — possible leak</div>
      )}
      <TimeSeriesChart
        series={[
          { name: 'RSS', color: '#c084fc', points: rss, area: true },
          { name: 'VMS', color: '#60a5fa', points: vms },
        ]}
        yUnit="MB"
        height={240}
      />
      <div className="chart-legend">
        <span><i style={{ background: '#c084fc' }} /> RSS (resident)</span>
        <span><i style={{ background: '#60a5fa' }} /> VMS (virtual)</span>
      </div>
      <div className="stat-grid stat-grid--3">
        <StatCell label="peak RSS" value={peakRss == null ? '—' : `${peakRss.toFixed(0)} MB`} />
        <StatCell label="peak VMS" value={peakVms == null ? '—' : `${peakVms.toFixed(0)} MB`} />
        <StatCell label="avg RSS" value={avgRss == null ? '—' : `${avgRss.toFixed(0)} MB`} />
      </div>
    </div>
  )
}
