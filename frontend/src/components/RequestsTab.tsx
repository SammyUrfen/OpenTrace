import { Fragment, useState } from 'react'
import { useRunObject } from '../state/useRunObject'
import type { RequestEndpoint, Requests } from '../state/useOpenTrace'
import { BreakdownBar, RequestWaterfall } from './RequestWaterfall'

interface Props {
  backendUrl: string
  runId: string
  /** live SSE rollup for a monitor run — preferred over the one-shot fetch. */
  live?: Requests | null
}

const COLUMNS: { key: keyof RequestEndpoint; label: string; numeric: boolean }[] = [
  { key: 'route', label: 'endpoint', numeric: false },
  { key: 'count', label: 'count', numeric: true },
  { key: 'p50_ms', label: 'p50', numeric: true },
  { key: 'p95_ms', label: 'p95', numeric: true },
  { key: 'p99_ms', label: 'p99', numeric: true },
  { key: 'err_pct', label: 'err%', numeric: true },
  { key: 'db_ms_share', label: '% DB', numeric: true },
]

function ms(v: number | null): string {
  if (v == null) return '—'
  return v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1)
}

/** Per-endpoint expanded detail: the full on/off/DB/run-queue decomposition when the run
 *  captured it (off-CPU breakdown), else the DB-vs-app split, else wall-time only. */
function Breakdown({ e, hasDb }: { e: RequestEndpoint; hasDb: boolean }) {
  const share = e.db_ms_share ?? 0
  const dbPct = Math.round(share * 100)
  return (
    <div data-testid="endpoint-breakdown">
      <div className="lat-pcts">
        <span>p50 <b>{ms(e.p50_ms)} ms</b></span>
        <span>p95 <b>{ms(e.p95_ms)} ms</b></span>
        <span>p99 <b>{ms(e.p99_ms)} ms</b></span>
        <span>requests <b>{e.count.toLocaleString()}</b></span>
        {hasDb && <span>DB share <b>{dbPct}%</b></span>}
      </div>
      {e.breakdown ? (
        <BreakdownBar b={e.breakdown} />
      ) : (
        <div className="lat-row">
          <span className="lat-row__range">time split</span>
          <span className="lat-row__bar">
            {hasDb && (
              <span className="lat-row__fill" style={{ width: `${dbPct}%`, background: 'var(--accent)' }} />
            )}
          </span>
          <span className="lat-row__count">
            {hasDb ? `${dbPct}% DB · ${100 - dbPct}% app` : 'wall time only'}
          </span>
        </div>
      )}
    </div>
  )
}

/**
 * Requests tab (attach request-tracing): the per-endpoint RED table (Rate / Errors /
 * Duration) + a DB-vs-app breakdown per endpoint. Fail-open to the rollup's `reason`
 * (a non-HTTP / TLS / no-privilege run shows a friendly empty state). The waterfall +
 * span→flamegraph drill are Phase 2.
 */
export function RequestsTab({ backendUrl, runId, live }: Props) {
  const { data: fetched, loading } = useRunObject<Requests>(backendUrl, runId, 'requests')
  const data = live ?? fetched
  const [sortKey, setSortKey] = useState<keyof RequestEndpoint>('p95_ms')
  const [asc, setAsc] = useState(false)
  const [openRoute, setOpenRoute] = useState<string | null>(null)
  const [view, setView] = useState<'endpoints' | 'requests'>('endpoints')

  if (loading && !data) {
    return (
      <div className="overview" data-testid="requests-tab">
        <div className="overview__muted">Loading requests…</div>
      </div>
    )
  }

  const endpoints = data?.endpoints ?? []
  const sampleSpans = data?.spans ?? []
  // DB overlay is available iff the run captured any DB spans (a dynamically-linked libpq);
  // otherwise the '% DB' column degrades to '—' rather than a misleading 0%.
  const hasDb = (data?.db_span_count ?? 0) > 0 || endpoints.some((e) => e.db_ms_share > 0)

  const sorted = [...endpoints].sort((a, b) => {
    const av = a[sortKey]
    const bv = b[sortKey]
    if (typeof av === 'string' || typeof bv === 'string') {
      return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av))
    }
    const an = (av as number) ?? -1
    const bn = (bv as number) ?? -1
    return asc ? an - bn : bn - an
  })

  const onSort = (k: keyof RequestEndpoint) => {
    if (k === sortKey) setAsc((v) => !v)
    else {
      setSortKey(k)
      setAsc(false)
    }
  }

  return (
    <div className="overview" data-testid="requests-tab">
      <h3 className="overview__h">
        Requests (HTTP endpoints)
        {data?.engine && (
          <span className="overview__muted" style={{ marginLeft: 8, fontWeight: 400 }}>
            via {data.engine}
          </span>
        )}
      </h3>
      {data && data.request_count > 0 && (
        <div className="overview__muted" style={{ marginBottom: 8 }}>
          {data.request_count.toLocaleString()} request{data.request_count === 1 ? '' : 's'}
          {data.db_span_count > 0 ? `, ${data.db_span_count.toLocaleString()} DB queries` : ''} captured
          {data.window_s ? ` over ${data.window_s}s` : ''}.
        </div>
      )}
      {data?.reason && (
        <div className="overview__muted" style={{ marginBottom: 12 }}>{data.reason}</div>
      )}

      {sampleSpans.length > 0 && (
        <div className="req-view-toggle" role="tablist">
          <button type="button" role="tab" aria-selected={view === 'endpoints'}
            className={`req-view-toggle__btn${view === 'endpoints' ? ' req-view-toggle__btn--on' : ''}`}
            onClick={() => setView('endpoints')}>Endpoints</button>
          <button type="button" role="tab" aria-selected={view === 'requests'}
            className={`req-view-toggle__btn${view === 'requests' ? ' req-view-toggle__btn--on' : ''}`}
            onClick={() => setView('requests')}>Requests</button>
        </div>
      )}

      {view === 'requests' ? (
        <RequestWaterfall spans={sampleSpans} backendUrl={backendUrl} runId={runId} />
      ) : endpoints.length === 0 ? (
        <div className="overview__muted">
          No HTTP endpoints captured for this run. Request tracing attaches to a plaintext
          HTTP/1.x server and reports per-endpoint latency (and, when the target links libpq
          dynamically, the Postgres time inside each request).
        </div>
      ) : (
        <table className="syscall-table" data-testid="endpoint-table">
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  className={c.numeric ? 'num' : ''}
                  onClick={() => onSort(c.key)}
                  aria-sort={sortKey === c.key ? (asc ? 'ascending' : 'descending') : 'none'}
                >
                  {c.label}
                  {sortKey === c.key && <span className="sort-caret">{asc ? ' ▲' : ' ▼'}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((e) => {
              const label = `${e.method} ${e.route}`
              const open = openRoute === label
              const dbPct = Math.round((e.db_ms_share ?? 0) * 100)
              return (
                <Fragment key={label}>
                  <tr
                    data-testid="endpoint-row"
                    data-route={e.route}
                    className="endpoint-row"
                    onClick={() => setOpenRoute(open ? null : label)}
                  >
                    <td className="syscall-name">
                      <span className="endpoint-method">{e.method}</span> {e.route}
                    </td>
                    <td className="num">{e.count.toLocaleString()}</td>
                    <td className="num">{ms(e.p50_ms)}</td>
                    <td className="num">{ms(e.p95_ms)}</td>
                    <td className="num">{ms(e.p99_ms)}</td>
                    <td className={`num ${e.err_pct > 0 ? 'errs' : ''}`}>{e.err_pct}%</td>
                    <td className="num">
                      {hasDb ? (
                        <span className="endpoint-dbbar" title={`${dbPct}% of request time in DB queries`}>
                          <span className="endpoint-dbbar__fill" style={{ width: `${dbPct}%` }} />
                          <span className="endpoint-dbbar__txt">{dbPct}%</span>
                        </span>
                      ) : '—'}
                    </td>
                  </tr>
                  {open && (
                    <tr className="endpoint-breakdown-row">
                      <td colSpan={COLUMNS.length}>
                        <Breakdown e={e} hasDb={hasDb} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
