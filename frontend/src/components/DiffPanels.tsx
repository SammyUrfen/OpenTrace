import type { MetricSample } from '../state/useOpenTrace'
import type { RunBundle } from '../state/useDiff'
import type { SyscallStat } from '../state/useSyscalls'
import { SEVERITY_COLOR } from '../state/format'
import { TimeSeriesChart } from './TimeSeriesChart'

const COLOR_A = '#ff8c42'
const COLOR_B = '#5fb3d5'

/** Normalize a metric series to elapsed-ms from the run's first sample so two
 *  runs with different absolute timestamps overlay from a shared zero. */
function elapsed(metrics: MetricSample[], key: keyof MetricSample): [number, number][] {
  if (!metrics.length) return []
  const t0 = metrics[0].timestamp_ms
  return metrics
    .filter((m) => m[key] != null)
    .map((m) => [m.timestamp_ms - t0, m[key] as number])
}

function peak(pts: [number, number][]): number | null {
  return pts.length ? Math.max(...pts.map((p) => p[1])) : null
}

function Legend() {
  return (
    <div className="chart-legend">
      <span><i style={{ background: COLOR_A }} /> A</span>
      <span><i style={{ background: COLOR_B }} /> B</span>
    </div>
  )
}

function metricDiff(a: RunBundle, b: RunBundle, key: keyof MetricSample, unit: string, label: string) {
  const pa = elapsed(a.metrics, key)
  const pb = elapsed(b.metrics, key)
  const peakA = peak(pa)
  const peakB = peak(pb)
  const delta = peakA != null && peakB != null ? peakB - peakA : null
  return { pa, pb, peakA, peakB, delta, unit, label }
}

function MetricDiffPanel({
  a, b, mkey, unit, label, testid,
}: {
  a: RunBundle; b: RunBundle; mkey: keyof MetricSample; unit: string; label: string; testid: string
}) {
  const d = metricDiff(a, b, mkey, unit, label)
  return (
    <div className="overview" data-testid={testid}>
      <h3 className="overview__h">{label} over time — A vs B (aligned at t=0)</h3>
      <TimeSeriesChart
        series={[
          { name: 'A', color: COLOR_A, points: d.pa },
          { name: 'B', color: COLOR_B, points: d.pb },
        ]}
        yUnit={unit}
        height={240}
      />
      <Legend />
      <div className="diff-grid">
        <div className="diff-row diff-row--head">
          <span className="diff-row__label">peak {label}</span>
          <span className="diff-row__a">A</span>
          <span className="diff-row__b">B</span>
          <span className="diff-delta">Δ</span>
        </div>
        <div className="diff-row">
          <span className="diff-row__label">peak {label}</span>
          <span className="diff-row__a">{d.peakA == null ? '—' : d.peakA.toFixed(0) + unit}</span>
          <span className="diff-row__b">{d.peakB == null ? '—' : d.peakB.toFixed(0) + unit}</span>
          <span className={`diff-delta ${d.delta == null ? '' : d.delta < 0 ? 'diff-delta--good' : d.delta > 0 ? 'diff-delta--bad' : ''}`}>
            {d.delta == null ? '—' : `${d.delta > 0 ? '+' : ''}${d.delta.toFixed(0)}${unit}`}
          </span>
        </div>
      </div>
    </div>
  )
}

export function MemoryDiff({ a, b }: { a: RunBundle; b: RunBundle }) {
  return <MetricDiffPanel a={a} b={b} mkey="rss_mb" unit=" MB" label="Memory" testid="memory-diff" />
}
export function CpuDiff({ a, b }: { a: RunBundle; b: RunBundle }) {
  return <MetricDiffPanel a={a} b={b} mkey="cpu_pct" unit="%" label="CPU" testid="cpu-diff" />
}

export function SyscallDiff({ a, b }: { a: RunBundle; b: RunBundle }) {
  const byName = new Map<string, { a?: SyscallStat; b?: SyscallStat }>()
  for (const s of a.syscalls) byName.set(s.syscall, { ...byName.get(s.syscall), a: s })
  for (const s of b.syscalls) byName.set(s.syscall, { ...byName.get(s.syscall), b: s })
  const rows = [...byName.entries()]
    .map(([name, { a: sa, b: sb }]) => {
      const ca = sa?.count ?? 0
      const cb = sb?.count ?? 0
      return { name, ca, cb, dc: cb - ca, ma: sa?.avg_ms ?? null, mb: sb?.avg_ms ?? null }
    })
    .sort((x, y) => Math.abs(y.dc) - Math.abs(x.dc))

  return (
    <div className="overview" data-testid="syscall-diff">
      <h3 className="overview__h">Syscall counts — Δ between A and B</h3>
      <table className="syscall-table">
        <thead>
          <tr>
            <th>syscall</th>
            <th className="num">A count</th>
            <th className="num">B count</th>
            <th className="num">Δ count</th>
            <th className="num">A avg ms</th>
            <th className="num">B avg ms</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.name}>
              <td className="syscall-name">{r.name}</td>
              <td className="num">{r.ca.toLocaleString()}</td>
              <td className="num">{r.cb.toLocaleString()}</td>
              <td className={`num ${r.dc < 0 ? 'diff-delta--good' : r.dc > 0 ? 'diff-delta--bad' : ''}`}>
                {r.dc > 0 ? '+' : ''}{r.dc.toLocaleString()}
              </td>
              <td className="num">{r.ma == null ? '—' : r.ma.toFixed(2)}</td>
              <td className="num">{r.mb == null ? '—' : r.mb.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function AnomalyDiff({ a, b }: { a: RunBundle; b: RunBundle }) {
  const aIds = new Set(a.anomalies.map((x) => x.rule_id))
  const bIds = new Set(b.anomalies.map((x) => x.rule_id))
  const onlyA = a.anomalies.filter((x) => !bIds.has(x.rule_id))
  const onlyB = b.anomalies.filter((x) => !aIds.has(x.rule_id))
  const both = a.anomalies.filter((x) => bIds.has(x.rule_id))

  const Col = ({ title, items, cls }: { title: string; items: typeof a.anomalies; cls: string }) => (
    <div className={`anomdiff-col ${cls}`}>
      <div className="anomdiff-col__head">{title} ({items.length})</div>
      {items.length === 0 ? (
        <div className="overview__muted">none</div>
      ) : (
        items.map((x) => (
          <div key={x.id} className="anomdiff-item" style={{ borderLeftColor: SEVERITY_COLOR[x.severity] }}>
            <span className="anomdiff-item__sev" style={{ color: SEVERITY_COLOR[x.severity] }}>
              {x.severity.toUpperCase()}
            </span>{' '}
            {x.title}
          </div>
        ))
      )}
    </div>
  )

  return (
    <div className="overview" data-testid="anomaly-diff">
      <h3 className="overview__h">Anomalies — only in A · in both · only in B</h3>
      <div className="anomdiff">
        <Col title="Only in A" items={onlyA} cls="anomdiff-col--a" />
        <Col title="In both" items={both} cls="anomdiff-col--both" />
        <Col title="Only in B" items={onlyB} cls="anomdiff-col--b" />
      </div>
    </div>
  )
}
