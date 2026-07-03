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
}

interface Props {
  backendUrl: string
  runId: string
}

const ROW_H = 20

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

export function FlamegraphTab({ backendUrl, runId }: Props) {
  const { data, loading } = useRunObject<Flamegraph>(backendUrl, runId, 'flamegraph')
  const [focus, setFocus] = useState<FlameNode | null>(null)

  const root = data?.tree ?? null
  const view = focus ?? root
  const { rects, depth } = useMemo(
    () => (view ? layout(view) : { rects: [], depth: 0 }),
    [view],
  )

  if (loading && !data) {
    return (
      <div className="overview" data-testid="flamegraph-tab">
        <div className="overview__muted">Loading flamegraph…</div>
      </div>
    )
  }

  if (!data?.supported || !root) {
    return (
      <div className="overview" data-testid="flamegraph-tab">
        <h3 className="overview__h">CPU flamegraph</h3>
        <div className="overview__muted">
          No CPU profile for this run{data?.reason ? ` (${data.reason})` : ''}. Enable the{' '}
          <b>Hardware perf</b> collector (Live Monitor) and re-run a CPU-bound program.
        </div>
      </div>
    )
  }

  const total = root.value || 1
  const hotspots = data.hotspots ?? []

  return (
    <div className="overview" data-testid="flamegraph-tab">
      <h3 className="overview__h">
        CPU flamegraph — {data.samples.toLocaleString()} samples
        <span className="overview__muted" style={{ marginLeft: 10, fontWeight: 400 }}>
          click a frame to zoom
        </span>
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
              title={`${r.node.name} — ${r.node.value.toLocaleString()} samples (${pct}%)`}
              onClick={() => setFocus(r.node)}
            >
              <span className="flame-cell__label">{r.node.name}</span>
            </div>
          )
        })}
      </div>

      <h3 className="overview__h">Function hotspots — {hotspots.length} symbols</h3>
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
      )}
    </div>
  )
}
