import { useRunObject } from '../state/useRunObject'

interface Bucket {
  lo: number
  hi: number
  count: number
}
interface Hist {
  unit: string
  buckets: Bucket[]
  total: number
  p50: number | null
  p90: number | null
  p99: number | null
  max: number | null
  error?: string
}
interface PidIo {
  count: number
  total_ms: number
  p50_ms: number | null
  p99_ms: number | null
  max_ms: number | null
  read_count: number
  write_count: number
  bytes_total: number
  error?: string
}
interface Latency {
  available: boolean
  reason: string | null
  engine?: string
  runqueue: Hist | null
  block_io: Hist | null
  block_io_pid?: PidIo | null
}
interface GcTimeline {
  available: boolean
  reason: string | null
  events: { start_s: number; duration_ms: number }[]
}

interface Props {
  backendUrl: string
  runId: string
}

function PidIoCard({ io }: { io: PidIo | null | undefined }) {
  if (!io || io.error || !io.count) {
    return (
      <div className="lat-card">
        <div className="lat-card__head">
          <h3 className="overview__h">This process's block I/O</h3>
          <span className="overview__muted">per-PID, from biosnoop</span>
        </div>
        <div className="overview__muted">
          {io?.error ? io.error : 'No block I/O attributed to this process in the window.'}
        </div>
      </div>
    )
  }
  const mb = (io.bytes_total / (1024 * 1024)).toFixed(1)
  return (
    <div className="lat-card">
      <div className="lat-card__head">
        <h3 className="overview__h">This process's block I/O</h3>
        <span className="overview__muted">per-PID, from biosnoop</span>
      </div>
      <div className="lat-pcts">
        <span>p50 <b>{io.p50_ms ?? '—'} ms</b></span>
        <span>p99 <b>{io.p99_ms ?? '—'} ms</b></span>
        <span>max <b>{io.max_ms ?? '—'} ms</b></span>
        <span>{io.count.toLocaleString()} ops <b>({io.read_count}R / {io.write_count}W)</b></span>
        <span>{mb} MB</span>
      </div>
    </div>
  )
}

function GcCard({ gc }: { gc: GcTimeline | null }) {
  if (!gc || !gc.available || !gc.events.length) {
    return (
      <div className="lat-card">
        <div className="lat-card__head">
          <h3 className="overview__h">GC pauses (USDT)</h3>
          <span className="overview__muted">Python --enable-dtrace builds</span>
        </div>
        <div className="overview__muted">{gc?.reason ?? 'No GC events captured.'}</div>
      </div>
    )
  }
  const durs = gc.events.map((e) => e.duration_ms)
  const total = durs.reduce((a, b) => a + b, 0)
  const max = Math.max(...durs)
  return (
    <div className="lat-card">
      <div className="lat-card__head">
        <h3 className="overview__h">GC pauses (USDT)</h3>
        <span className="overview__muted">garbage-collection stop-the-world time</span>
      </div>
      <div className="lat-pcts">
        <span>collections <b>{gc.events.length}</b></span>
        <span>total pause <b>{total.toFixed(1)} ms</b></span>
        <span>longest <b>{max.toFixed(2)} ms</b></span>
        <span>avg <b>{(total / gc.events.length).toFixed(2)} ms</b></span>
      </div>
    </div>
  )
}

function pctText(v: number | null, unit: string): string {
  return v === null ? '—' : `${v.toLocaleString()} ${unit}`
}

function HistCard({ title, subtitle, hist }: { title: string; subtitle: string; hist: Hist | null }) {
  if (!hist || hist.error || !hist.total) {
    return (
      <div className="lat-card">
        <div className="lat-card__head">
          <h3 className="overview__h">{title}</h3>
          <span className="overview__muted">{subtitle}</span>
        </div>
        <div className="overview__muted">
          {hist?.error ? hist.error : 'No samples captured for this window.'}
        </div>
      </div>
    )
  }
  const unit = hist.unit
  const max = hist.buckets.reduce((m, b) => Math.max(m, b.count), 0) || 1
  return (
    <div className="lat-card">
      <div className="lat-card__head">
        <h3 className="overview__h">{title}</h3>
        <span className="overview__muted">{subtitle}</span>
      </div>
      <div className="lat-pcts">
        <span>p50 <b>{pctText(hist.p50, unit)}</b></span>
        <span>p90 <b>{pctText(hist.p90, unit)}</b></span>
        <span>p99 <b>{pctText(hist.p99, unit)}</b></span>
        <span>max <b>{pctText(hist.max, unit)}</b></span>
        <span className="overview__muted">{hist.total.toLocaleString()} events</span>
      </div>
      <div className="lat-hist">
        {hist.buckets.map((b) => (
          <div className="lat-row" key={`${b.lo}-${b.hi}`}>
            <span className="lat-row__range">{b.lo.toLocaleString()}–{b.hi.toLocaleString()}</span>
            <span className="lat-row__bar">
              <span className="lat-row__fill" style={{ width: `${(b.count / max) * 100}%` }} />
            </span>
            <span className="lat-row__count">{b.count.toLocaleString()}</span>
          </div>
        ))}
      </div>
      <div className="overview__muted lat-card__unit">buckets in {unit} (power-of-2)</div>
    </div>
  )
}

/** eBPF latency: run-queue + block-I/O histograms + per-PID I/O + GC (Phase D/E). */
export function LatencyTab({ backendUrl, runId }: Props) {
  const { data, loading } = useRunObject<Latency>(backendUrl, runId, 'latency')
  const { data: gc } = useRunObject<GcTimeline>(backendUrl, runId, 'gc-timeline')

  if (loading && !data) {
    return (
      <div className="overview" data-testid="latency-tab">
        <div className="overview__muted">Loading latency…</div>
      </div>
    )
  }

  return (
    <div className="overview" data-testid="latency-tab">
      <h3 className="overview__h">
        Kernel latency (eBPF)
        {data?.engine && <span className="overview__muted" style={{ marginLeft: 8, fontWeight: 400 }}>
          via {data.engine}</span>}
      </h3>
      {data && !data.available && (
        <div className="overview__muted" style={{ marginBottom: 12 }}>
          {data.reason ?? 'eBPF latency not available for this run.'} These measure how
          long the process waited for a CPU (scheduler) and for the disk (block I/O) —
          the waits on-CPU sampling can't see.
        </div>
      )}
      <HistCard
        title="Scheduler run-queue latency"
        subtitle="how long runnable threads waited for a CPU — high = oversubscription"
        hist={data?.runqueue ?? null}
      />
      <PidIoCard io={data?.block_io_pid} />
      <HistCard
        title="Block-I/O latency"
        subtitle="how long disk I/O took (host-wide, all processes) — high = slow/contended storage"
        hist={data?.block_io ?? null}
      />
      <GcCard gc={gc ?? null} />
    </div>
  )
}
