import { useEffect, useMemo, useState } from 'react'

interface Target {
  pid: number
  name: string
  cmdline: string
  runtime: string
  runtime_label: string
  hint: string
  /** dedicated sampler available for this runtime (py-spy/rbspy/asprof), or null */
  sampler: string | null
  rss_mb: number
  container?: { container: boolean; runtime: string | null; id: string | null }
}

interface Props {
  backendUrl: string
  /** Active session so the attach run lands in the user's current project. */
  sessionId?: string | null
  onClose: () => void
  /** Called after a successful attach (the run streams in over SSE). */
  onAttached?: () => void
}

/** Runtimes perf symbolizes well today (Phase A); others get a "VM frames" caveat. */
const PERF_NATIVE = new Set(['native', 'go'])

/**
 * "Attach to a running process" picker (profiling Phase A). Lists attachable
 * PIDs from `GET /runs/attach/targets`, then `POST /runs/attach` samples the
 * chosen one with perf for a bounded window and builds a flamegraph. The run
 * appears live over SSE and auto-opens when the window elapses.
 */
export function AttachModal({ backendUrl, sessionId, onClose, onAttached }: Props) {
  const [targets, setTargets] = useState<Target[] | null>(null)
  const [filter, setFilter] = useState('')
  const [windowS, setWindowS] = useState(20)
  const [monitor, setMonitor] = useState(false)
  const [ebpf, setEbpf] = useState(false)
  const [ebpfCaps, setEbpfCaps] = useState<{ available: boolean; reason: string | null } | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = () => {
    setTargets(null)
    setError(null)
    fetch(`${backendUrl}/runs/attach/targets`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((d) => setTargets(Array.isArray(d) ? d : []))
      .catch((e) => {
        setTargets([])
        setError(`Couldn't list processes: ${e instanceof Error ? e.message : String(e)}`)
      })
  }
  useEffect(load, [backendUrl])
  useEffect(() => {
    fetch(`${backendUrl}/runs/attach/ebpf-capabilities`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setEbpfCaps(d))
      .catch(() => setEbpfCaps(null))
  }, [backendUrl])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase()
    const list = targets ?? []
    if (!q) return list
    return list.filter(
      (t) => t.cmdline.toLowerCase().includes(q) || String(t.pid).includes(q) || t.runtime.includes(q),
    )
  }, [targets, filter])

  const attach = async (t: Target) => {
    setBusy(t.pid)
    setError(null)
    try {
      const r = await fetch(`${backendUrl}/runs/attach`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pid: t.pid, window_s: windowS, monitor,
          ebpf: ebpf && !!ebpfCaps?.available, session_id: sessionId ?? null,
        }),
      })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        throw new Error(d.detail || `HTTP ${r.status}`)
      }
      onAttached?.()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(null)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal modal--wizard attach" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h2>Attach to a running process</h2>
          <button type="button" className="modal__close" onClick={onClose} aria-label="close">×</button>
        </div>

        <div className="attach__controls">
          <input
            className="attach__search"
            placeholder="Filter by command, pid, or runtime…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            autoFocus
          />
          <label className="attach__window">
            {monitor ? 'Snapshot' : 'Sample'}
            <input
              type="number" min={3} max={120} step={1} value={windowS}
              onChange={(e) => setWindowS(Math.max(3, Math.min(120, Math.round(Number(e.target.value) || 20))))}
            />
            s
          </label>
          <button type="button" className="ai-btn" onClick={load} title="rescan">↻</button>
        </div>

        <label className="attach__monitor">
          <input type="checkbox" checked={monitor} onChange={(e) => setMonitor(e.target.checked)} />
          <span>
            <span className="attach__monitor-label">Keep monitoring (live)</span>
            <span className="attach__monitor-sub">
              Runs continuously — repeating {windowS}s profiling snapshots + a live
              incident feed (each anomaly captured with when · where · leading metrics),
              until you Stop. The profiling adds overhead; metrics stay cheap.
            </span>
          </span>
        </label>

        <label className={`attach__monitor${ebpfCaps && !ebpfCaps.available ? ' attach__monitor--off' : ''}`}>
          <input
            type="checkbox" checked={ebpf && !!ebpfCaps?.available}
            disabled={!ebpfCaps?.available}
            onChange={(e) => setEbpf(e.target.checked)}
          />
          <span>
            <span className="attach__monitor-label">
              Off-CPU + latency (eBPF)
              {ebpfCaps && !ebpfCaps.available && <span className="attach__ebpf-tag"> unavailable</span>}
            </span>
            <span className="attach__monitor-sub">
              {ebpfCaps?.available
                ? 'Adds an off-CPU flamegraph (where it BLOCKS — I/O, locks, DB waits) and scheduler + block-I/O latency histograms. This is what on-CPU sampling cannot see.'
                : (ebpfCaps?.reason ?? 'Checking eBPF capabilities…')}
            </span>
          </span>
        </label>

        {error && <div className="attach__error">✗ {error}</div>}

        <div className="attach__list">
          {targets === null && <div className="attach__empty">Scanning processes…</div>}
          {targets && shown.length === 0 && <div className="attach__empty">No matching processes.</div>}
          {shown.map((t) => (
            <button
              key={t.pid}
              type="button"
              className="attach__row"
              disabled={busy !== null}
              onClick={() => attach(t)}
              title={t.cmdline}
            >
              <span className={`attach__rt ${PERF_NATIVE.has(t.runtime) || t.sampler ? 'attach__rt--native' : 'attach__rt--vm'}`}>
                {t.runtime_label}
              </span>
              <span className="attach__cmd">
                {t.cmdline}
                {t.container?.container && (
                  <span className="attach__container" title={`${t.container.runtime} container ${t.container.id}`}>
                    🐳 {t.container.runtime}:{t.container.id}
                  </span>
                )}
              </span>
              <span className="attach__meta">pid {t.pid} · {t.rss_mb} MB</span>
              <span className="attach__hint">{busy === t.pid ? 'attaching…' : t.hint}</span>
            </button>
          ))}
        </div>

        <div className="modal__foot">
          <span className="attach__foot-note">
            perf samples the process for the window, then builds a flamegraph.
            Native/Go show real symbols; interpreted runtimes show VM frames until
            their samplers land.
          </span>
        </div>
      </div>
    </div>
  )
}
