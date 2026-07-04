import { useEffect, useMemo, useState } from 'react'
import type { Incident } from '../state/useOpenTrace'
import { formatTime, severityColor } from '../state/format'

interface Props {
  backendUrl: string
  runId: string
  /** live incidents from the SSE store (newest first) */
  live: Incident[]
}

function peak(metrics: Incident['metrics'], key: 'cpu_pct' | 'rss_mb' | 'open_fds'): number | null {
  const vals = metrics.map((m) => m[key]).filter((v): v is number => v != null)
  return vals.length ? Math.max(...vals) : null
}

/**
 * The monitor-mode incident feed: each detected anomaly with WHEN, WHAT, WHERE
 * (the CPU hot call path captured at that moment), the leading metric context,
 * and — if continuous AI is on — a short explanation. Newest first.
 *
 * Merges the live SSE incidents with the persisted ones (fetched on open) so a
 * reopened monitor run shows its history too.
 */
export function IncidentFeed({ backendUrl, runId, live }: Props) {
  const [stored, setStored] = useState<Incident[]>([])

  useEffect(() => {
    let cancelled = false
    fetch(`${backendUrl}/runs/${runId}/incidents`)
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => { if (!cancelled) setStored(Array.isArray(d) ? d : []) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [backendUrl, runId])

  // Merge live + stored by id, live wins (has the latest hot/ai patches).
  const incidents = useMemo(() => {
    const byId = new Map<string, Incident>()
    for (const i of stored) byId.set(i.id, i)
    for (const i of live) byId.set(i.id, i)
    return [...byId.values()].sort((a, b) => b.ts - a.ts)
  }, [live, stored])

  if (incidents.length === 0) {
    return (
      <div className="incidents incidents--empty">
        No incidents yet. As the process runs, anomalies (CPU spikes, memory growth,
        fd leaks) will appear here with when · where · context.
      </div>
    )
  }

  return (
    <div className="incidents">
      <div className="incidents__count">{incidents.length} incident{incidents.length === 1 ? '' : 's'}</div>
      {incidents.map((inc) => {
        const stack = inc.hot?.stack ?? []
        const cpu = peak(inc.metrics, 'cpu_pct')
        const rss = peak(inc.metrics, 'rss_mb')
        const fds = peak(inc.metrics, 'open_fds')
        return (
          <div key={inc.id} className="incident">
            <div className="incident__head">
              <span className="incident__dot" style={{ background: severityColor(inc.severity, 'completed') }} />
              <span className="incident__title">{inc.title}</span>
              <span className="incident__time">{formatTime(inc.ts)}</span>
            </div>
            <div className="incident__where">
              <span className="incident__where-label">where</span>
              {stack.length ? (
                <code className="incident__stack">{stack.join('  →  ')}</code>
              ) : (
                <span className="incident__nowhere">
                  no CPU hot path — likely off-CPU (waiting on I/O, a lock, or the DB),
                  which a CPU profile can't attribute
                </span>
              )}
            </div>
            <div className="incident__ctx">
              {cpu != null && <span>peak CPU {cpu.toFixed(0)}%</span>}
              {rss != null && <span>peak RSS {rss.toFixed(0)}MB</span>}
              {fds != null && <span>FDs {fds}</span>}
              <span>{inc.metrics.length} samples</span>
            </div>
            {inc.ai && <div className="incident__ai">✦ {inc.ai}</div>}
          </div>
        )
      })}
    </div>
  )
}
