import { useMemo, useState } from 'react'
import type { RunDetail } from '../state/useRunDetail'
import { useRunResource } from '../state/useRunResource'
import { SEVERITY_COLOR } from '../state/format'
import { downsamplePoints, pts } from './seriesUtils'
import { TimelineChart, type TlAnomaly, type TlEvent } from './TimelineChart'

interface EventRow {
  id: string
  timestamp_ms: number
  event_type: string
  pid: number | null
  syscall: string | null
  error: string | null
  latency_ms: number | null
}

function classify(e: EventRow): TlEvent | null {
  if (e.error) return { t: e.timestamp_ms, kind: 'error', label: `${e.syscall ?? ''} → ${e.error}` }
  if (e.latency_ms != null && e.latency_ms > 100)
    return { t: e.timestamp_ms, kind: 'slow', label: `${e.syscall} took ${e.latency_ms.toFixed(0)}ms` }
  if (e.event_type === 'signal') return { t: e.timestamp_ms, kind: 'signal', label: e.syscall ?? 'signal' }
  if (e.syscall && e.syscall.startsWith('exec'))
    return { t: e.timestamp_ms, kind: 'exec', label: e.syscall }
  return null
}

interface Props {
  backendUrl: string
  runId: string
  detail: RunDetail
}

export function TimelineTab({ backendUrl, runId, detail }: Props) {
  const { rows: rawEvents } = useRunResource<EventRow>(backendUrl, runId, 'events')
  const { metrics, anomalies } = detail

  // Decimated in these memos so a multi-hour monitor run's 100k+-sample series
  // never reaches the SVG path builders (spikes survive via min/max buckets).
  const rss = useMemo(() => downsamplePoints(pts(metrics, 'rss_mb')), [metrics])
  const cpu = useMemo(() => downsamplePoints(pts(metrics, 'cpu_pct')), [metrics])
  const sys = useMemo(() => downsamplePoints(pts(metrics, 'syscall_rate')), [metrics])
  const events = useMemo(
    () => rawEvents.map(classify).filter((e): e is TlEvent => e !== null).slice(0, 800),
    [rawEvents],
  )
  const tlAnoms: TlAnomaly[] = useMemo(
    () =>
      anomalies
        .filter((a) => a.first_seen_ms != null && a.last_seen_ms != null)
        .map((a) => ({
          t0: a.first_seen_ms!,
          t1: a.last_seen_ms!,
          color: SEVERITY_COLOR[a.severity] ?? '#60a5fa',
          title: `${a.severity.toUpperCase()}: ${a.title}`,
        })),
    [anomalies],
  )

  const [t0, t1] = useMemo(() => {
    let lo = Infinity
    let hi = -Infinity
    for (const p of rss) {
      if (p[0] < lo) lo = p[0]
      if (p[0] > hi) hi = p[0]
    }
    for (const e of events) {
      if (e.t < lo) lo = e.t
      if (e.t > hi) hi = e.t
    }
    for (const a of tlAnoms) {
      if (a.t0 < lo) lo = a.t0
      if (a.t1 > hi) hi = a.t1
    }
    if (!Number.isFinite(lo)) return [0, 1] as [number, number]
    return [lo, hi > lo ? hi : lo + 1] as [number, number]
  }, [rss, events, tlAnoms])

  const [domain, setDomain] = useState<[number, number] | null>(null)
  const dom: [number, number] = domain ?? [t0, t1]
  const [tip, setTip] = useState<{ e: TlEvent; x: number; y: number } | null>(null)

  const zoomed = dom[0] > t0 + 1 || dom[1] < t1 - 1
  const hasData = rss.length > 0 || events.length > 0

  return (
    <div className="overview" data-testid="timeline-tab">
      <div className="overview__h tl-head">
        <span>Timeline</span>
        <span className="tl-legend">
          <i style={{ background: '#c084fc' }} /> mem
          <i style={{ background: '#60a5fa' }} /> cpu
          <i style={{ background: '#34d399' }} /> syscalls
          <i style={{ background: '#f87171' }} /> error
          <i style={{ background: '#fbbf24' }} /> signal
          {zoomed && (
            <button type="button" className="ai-btn tl-reset" onClick={() => setDomain(null)}>
              reset zoom
            </button>
          )}
        </span>
      </div>
      {!hasData ? (
        <div className="overview__muted">No timeline data for this run.</div>
      ) : (
        <div className="tl-wrap">
          <TimelineChart
            t0={t0}
            t1={t1}
            domain={dom}
            onDomain={setDomain}
            rss={rss}
            cpu={cpu}
            syscalls={sys}
            events={events}
            anomalies={tlAnoms}
            onHover={(e, x, y) => setTip(e ? { e, x, y } : null)}
          />
          <p className="chart-caption">Scroll to zoom, drag to pan. Shaded bands are anomaly windows.</p>
          {tip && (
            <div className="tl-tooltip" style={{ left: tip.x + 12, top: tip.y + 12 }}>
              {tip.e.label}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
