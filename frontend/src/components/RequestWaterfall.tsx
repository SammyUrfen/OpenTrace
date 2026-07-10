import { Fragment, useState } from 'react'
import type { RequestBreakdown, RequestSampleSpan } from '../state/useOpenTrace'
import { FlamegraphTab } from './FlamegraphTab'

/** The four wall-time buckets, in render order, with their colour class. */
const SEGMENTS: { key: keyof Pick<RequestBreakdown,
  'on_cpu_ms' | 'runq_ms' | 'db_wait_ms' | 'other_off_ms'>; label: string; cls: string }[] = [
  { key: 'on_cpu_ms', label: 'on-CPU', cls: 'req-seg--cpu' },
  { key: 'runq_ms', label: 'run-queue', cls: 'req-seg--runq' },
  { key: 'db_wait_ms', label: 'DB-wait', cls: 'req-seg--db' },
  { key: 'other_off_ms', label: 'off-CPU', cls: 'req-seg--off' },
]

function topReason(b: RequestBreakdown): string | null {
  if (b.top_off_reason) return b.top_off_reason
  const rs = b.off_reasons
  if (!rs) return null
  const best = Object.entries(rs).sort((a, c) => c[1] - a[1])[0]
  return best ? best[0] : null
}

/**
 * The on-CPU / run-queue / DB-wait / other-off-CPU decomposition of a request (or an
 * endpoint aggregate) as a 100%-stacked bar + a labelled legend. The four buckets sum to
 * the wall time — this is the literal answer to "where did the latency go?".
 */
export function BreakdownBar({ b }: { b: RequestBreakdown }) {
  const ms = {
    on_cpu_ms: b.on_cpu_ms, runq_ms: b.runq_ms,
    db_wait_ms: b.db_wait_ms, other_off_ms: b.other_off_ms,
  }
  const total = ms.on_cpu_ms + ms.runq_ms + ms.db_wait_ms + ms.other_off_ms || 1
  const reason = topReason(b)
  return (
    <div className="req-breakdown" data-testid="request-breakdown">
      <div className="req-breakdown__bar">
        {SEGMENTS.map((s) => {
          const pct = (100 * ms[s.key]) / total
          if (pct < 0.1) return null
          const label = s.key === 'other_off_ms' && reason ? `off-CPU (${reason})` : s.label
          return (
            <span key={s.key} className={`req-seg ${s.cls}`} style={{ width: `${pct}%` }}
              title={`${label} — ${pct.toFixed(0)}%`} />
          )
        })}
      </div>
      <div className="req-breakdown__legend">
        {SEGMENTS.map((s) => {
          const pct = (100 * ms[s.key]) / total
          if (pct < 0.5) return null
          const label = s.key === 'other_off_ms' && reason ? `off-CPU · ${reason}` : s.label
          return (
            <span key={s.key} className="req-chip">
              <i className={`req-dot ${s.cls}`} />{label} <b>{Math.round(pct)}%</b>
            </span>
          )
        })}
      </div>
    </div>
  )
}

function fmtMs(v: number): string {
  return v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1)
}

/**
 * Per-request waterfall: each sampled (slowest) request as a duration track with its nested
 * DB spans positioned in time. Click a row to expand its on/off/db/run-queue breakdown, the
 * captured SQL, and the span→off-CPU-flamegraph drill (that request thread's blocked stacks).
 */
export function RequestWaterfall({ spans, backendUrl, runId }:
  { spans: RequestSampleSpan[]; backendUrl: string; runId: string }) {
  // Track the expanded row by a STABLE span identity, not its array index — a live monitor
  // rollup re-sorts `spans` every snapshot, so an index-keyed open row would silently rebind
  // (and refetch the drill for) a different request each interval.
  const [open, setOpen] = useState<string | null>(null)
  if (!spans.length) {
    return <div className="overview__muted">No individual requests were sampled for this run.</div>
  }
  return (
    <div className="waterfall" data-testid="request-waterfall">
      {spans.map((s, i) => {
        const durNs = s.dur_ms * 1e6 || 1
        const start = s.start_ns ?? 0
        const key = `${s.method}-${s.route}-${s.tid}-${s.start_ns ?? i}`
        const isOpen = open === key
        return (
          <Fragment key={key}>
            <div className="waterfall__row" data-testid="waterfall-row" data-route={s.route ?? ''}
              onClick={() => setOpen(isOpen ? null : key)}>
              <span className="waterfall__label">
                <span className="endpoint-method">{s.method}</span> {s.route}
                {s.status != null && (
                  <span className={`waterfall__status${s.status >= 500 ? ' errs' : ''}`}>{s.status}</span>
                )}
              </span>
              <span className="waterfall__track" title={`${fmtMs(s.dur_ms)} ms`}>
                <span className="waterfall__http" />
                {s.db.map((d, j) => {
                  const left = d.start_ns != null ? Math.max(0, ((d.start_ns - start) / durNs) * 100) : 0
                  const w = Math.max(0.8, Math.min(100 - left, (d.dur_ms / s.dur_ms) * 100))
                  return (
                    <span key={j} className="span-bar" data-span-kind="db"
                      style={{ left: `${left}%`, width: `${w}%` }}
                      title={`${d.statement || d.name} — ${fmtMs(d.dur_ms)} ms`} />
                  )
                })}
              </span>
              <span className="waterfall__dur">{fmtMs(s.dur_ms)} ms</span>
            </div>
            {isOpen && (
              <div className="waterfall__detail" data-testid="waterfall-detail">
                {s.breakdown && <BreakdownBar b={s.breakdown} />}
                {s.db.length > 0 && (
                  <div className="waterfall__sql">
                    {s.db.map((d, j) => (
                      <div key={j} className="waterfall__sqlrow">
                        <code>{d.statement || d.name}</code>
                        <span>{fmtMs(d.dur_ms)} ms</span>
                      </div>
                    ))}
                  </div>
                )}
                <div className="waterfall__flame" data-testid="span-flame">
                  <FlamegraphTab backendUrl={backendUrl} runId={runId} tid={s.tid} />
                </div>
              </div>
            )}
          </Fragment>
        )
      })}
    </div>
  )
}
