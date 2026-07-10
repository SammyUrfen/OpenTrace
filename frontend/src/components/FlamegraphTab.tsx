import { useMemo, useState } from 'react'
import { useRunObject } from '../state/useRunObject'

interface FlameNode {
  name: string
  value: number
  children: FlameNode[]
}
interface PerfHotspot {
  function: string
  self: number
  total: number
  self_pct: number
  total_pct: number
}
interface Flamegraph {
  supported: boolean
  samples: number
  tree: FlameNode | null
  hotspots: PerfHotspot[]
  reason?: string
  unit?: string
  tid?: number
  tid_unavailable?: boolean
}

interface Props {
  backendUrl: string
  runId: string
  /** run captured an eBPF off-CPU profile → offer the On-CPU / Off-CPU toggle */
  offCpu?: boolean
  /**
   * Drill: pin to ONE request thread's off-CPU flame (the span→flamegraph drill).
   * Forces off-CPU mode, hides the On/Off toggle + the hotspot table, and fetches
   * `offcpu-flamegraph?tid=`. Falls back to the whole-process off-CPU flame with a note.
   */
  tid?: number
}

const ROW_H = 20

/** µs → a readable duration ("5.72 s" / "820 ms" / "500 µs"). */
function fmtDuration(us: number): string {
  if (us >= 1e6) return `${(us / 1e6).toFixed(2)} s`
  if (us >= 1000) return `${Math.round(us / 1000).toLocaleString()} ms`
  return `${us.toLocaleString()} µs`
}

interface Rect {
  key: string
  node: FlameNode
  x: number // percent
  w: number // percent
  depth: number
}

/** Flatten the tree into positioned rects (percent x/width) from a focus node. */
function layout(root: FlameNode): { rects: Rect[]; depth: number } {
  const rects: Rect[] = []
  let maxDepth = 0
  const walk = (node: FlameNode, x: number, w: number, depth: number, key: string) => {
    rects.push({ key, node, x, w, depth })
    maxDepth = Math.max(maxDepth, depth)
    let cx = x
    node.children.forEach((c, i) => {
      const cw = node.value ? (c.value / node.value) * w : 0
      if (cw >= 0.08) walk(c, cx, cw, depth + 1, `${key}.${i}`)
      cx += cw
    })
  }
  walk(root, 0, 100, 0, '0')
  return { rects, depth: maxDepth }
}

/** Stable warm (espresso-friendly) colour per frame, hashed from its name. */
function frameColor(name: string, depth: number): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0
  const hue = 12 + (Math.abs(h) % 40) // 12–52: red→amber→yellow
  const light = 46 + (depth % 3) * 6 // subtle per-row banding
  return `hsl(${hue} 68% ${light}%)`
}

export function FlamegraphTab({ backendUrl, runId, offCpu, tid }: Props) {
  const drill = tid != null
  const [mode, setMode] = useState<'on' | 'off'>(drill ? 'off' : 'on')
  const resource = drill
    ? `offcpu-flamegraph?tid=${tid}`
    : mode === 'off' ? 'offcpu-flamegraph' : 'flamegraph'
  const { data, loading } = useRunObject<Flamegraph>(backendUrl, runId, resource)
  const [focus, setFocus] = useState<FlameNode | null>(null)

  const isOff = drill || mode === 'off'
  // Format from the LOADED data's unit, not the toggle — while a switch is in
  // flight `data` is still the previous resource, so keying off `isOff` would
  // briefly render on-CPU counts through fmtDuration (and mislabel cells).
  const dataIsOff = data?.unit === 'usec'
  const cellUnit = dataIsOff ? 'µs' : 'samples'  // browser tooltip — not CSS-uppercased
  const toggle = offCpu && !drill ? (
    <div className="flame-toggle" role="tablist">
      <button type="button" role="tab" aria-selected={!isOff}
        className={`flame-toggle__btn${!isOff ? ' flame-toggle__btn--on' : ''}`}
        onClick={() => { setMode('on'); setFocus(null) }}>On-CPU</button>
      <button type="button" role="tab" aria-selected={isOff}
        className={`flame-toggle__btn${isOff ? ' flame-toggle__btn--on' : ''}`}
        onClick={() => { setMode('off'); setFocus(null) }}>Off-CPU</button>
    </div>
  ) : null

  const root = data?.tree ?? null
  const view = focus ?? root
  const { rects, depth } = useMemo(
    () => (view ? layout(view) : { rects: [], depth: 0 }),
    [view],
  )

  if ((loading && !data) || (loading && isOff !== dataIsOff)) {
    return (
      <div className="overview" data-testid="flamegraph-tab">
        {toggle}
        <div className="overview__muted">Loading {isOff ? 'off-CPU' : ''} flamegraph…</div>
      </div>
    )
  }

  if (drill && (!data?.supported || !root)) {
    return (
      <div className="overview" data-testid="flamegraph-tab">
        <div className="overview__muted">
          No off-CPU stacks were captured for thread {tid} — it spent the request on-CPU, or
          blocked too briefly to sample.
        </div>
      </div>
    )
  }
  if (!data?.supported || !root) {
    // The launch advice ("enable a collector and re-run") is wrong for an attach
    // run — there's no command to re-run, you re-attach. Detect attach from the
    // eBPF prop or the backend reason (these phrases come only from the attach
    // profiler path), and de-nest the parenthesised reason with an em-dash.
    const reason = data?.reason ?? ''
    const isAttachRun =
      !!offCpu || /captured no samples|target exited|resource timeline only|could not start/i.test(reason)
    return (
      <div className="overview" data-testid="flamegraph-tab">
        <h3 className="overview__h">{isOff ? 'Off-CPU flamegraph' : 'CPU flamegraph'}{toggle}</h3>
        <div className="overview__muted">
          {isOff ? (
            <>No off-CPU profile{reason ? ` — ${reason}` : ''}. Off-CPU
              needs the <b>eBPF</b> option at attach time and elevated privileges;
              when present it shows where the process is <b>blocked</b> (I/O, locks, waits).</>
          ) : (
            <>No CPU profile for this run{reason ? ` — ${reason}` : '.'}{' '}
              {isAttachRun ? (
                <>Re-attach with the <b>perf</b> or a language sampler (py-spy, rbspy,
                  async-profiler) collector while the target is doing work — a process
                  that stays idle for the window produces no stacks.</>
              ) : (
                <>Enable the <b>Hardware perf</b> collector and re-run a CPU-bound program.</>
              )}</>
          )}
        </div>
      </div>
    )
  }

  const total = root.value || 1
  const hotspots = data.hotspots ?? []

  return (
    <div className="overview" data-testid="flamegraph-tab">
      <h3 className="overview__h">
        {drill
          ? data.tid_unavailable
            ? `Off-CPU flamegraph — ${fmtDuration(data.samples)} blocked (whole process)`
            : `Off-CPU flamegraph — thread ${tid}, ${fmtDuration(data.samples)} blocked`
          : dataIsOff
          ? `Off-CPU flamegraph — ${fmtDuration(data.samples)} blocked`
          : `CPU flamegraph — ${data.samples.toLocaleString()} samples`}
        <span className="overview__muted" style={{ marginLeft: 10, fontWeight: 400 }}>
          {drill && data.tid_unavailable
            ? 'per-thread capture unavailable — showing the whole-process off-CPU flame'
            : isOff ? 'time spent blocked, not on CPU' : 'click a frame to zoom'}
        </span>
        {toggle}
        {focus && (
          <button type="button" className="ai-btn tl-reset" onClick={() => setFocus(null)}>
            reset zoom
          </button>
        )}
      </h3>

      <div className="flame" style={{ height: (depth + 1) * ROW_H + 2 }}>
        {rects.map((r) => {
          const pct = ((r.node.value / total) * 100).toFixed(1)
          return (
            <div
              key={r.key}
              className="flame-cell"
              style={{
                left: `${r.x}%`,
                width: `${r.w}%`,
                top: r.depth * ROW_H,
                height: ROW_H - 1,
                background: frameColor(r.node.name, r.depth),
              }}
              title={`${r.node.name} — ${r.node.value.toLocaleString()} ${cellUnit} (${pct}%)`}
              onClick={() => setFocus(r.node)}
            >
              <span className="flame-cell__label">{r.node.name}</span>
            </div>
          )
        })}
      </div>

      {drill ? null : <><h3 className="overview__h">Function hotspots — {hotspots.length} symbols</h3>
      {hotspots.length === 0 ? (
        <div className="overview__muted">No resolved symbols.</div>
      ) : (
        <table className="syscall-table" data-testid="perf-hotspot-table">
          <thead>
            <tr>
              <th>function</th>
              <th className="num">self</th>
              <th className="num">self %</th>
              <th className="num">total</th>
              <th className="num">total %</th>
            </tr>
          </thead>
          <tbody>
            {hotspots.map((h) => (
              <tr key={h.function}>
                <td className="syscall-name">{h.function}</td>
                <td className="num">{h.self.toLocaleString()}</td>
                <td className="num">{h.self_pct.toFixed(1)}%</td>
                <td className="num">{h.total.toLocaleString()}</td>
                <td className="num">{h.total_pct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}</>}
    </div>
  )
}
