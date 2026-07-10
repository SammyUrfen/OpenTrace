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

interface EbpfCaps {
  available: boolean
  reason: string | null
}

// eBPF capabilities are stable within a host boot, and the backend probe spawns
// sudo subprocesses with multi-second timeouts — cache the result per backend
// for the app session instead of re-probing on every modal open. A failed
// probe is evicted so a transient startup error doesn't pin the checkbox at
// "Checking…"; the rescan button bypasses the cache (so users can re-check
// after installing bcc / adding a sudoers rule per the `reason` instructions).
const ebpfCapsCache = new Map<string, Promise<EbpfCaps | null>>()

function fetchEbpfCaps(backendUrl: string, force = false): Promise<EbpfCaps | null> {
  if (force || !ebpfCapsCache.has(backendUrl)) {
    // force also bypasses the backend's TTL cache, not just this client one
    const probe = fetch(`${backendUrl}/runs/attach/ebpf-capabilities${force ? '?refresh=true' : ''}`)
      .then((r) => (r.ok ? (r.json() as Promise<EbpfCaps>) : null))
      .catch(() => null)
      .then((caps) => {
        if (caps === null) ebpfCapsCache.delete(backendUrl)
        return caps
      })
    ebpfCapsCache.set(backendUrl, probe)
  }
  return ebpfCapsCache.get(backendUrl)!
}

// Request-tracing capability (bpftrace + privilege — a DIFFERENT, weaker gate than
// eBPF's BTF+bcc). Cached per backend like the eBPF probe, for the same reason.
const requestCapsCache = new Map<string, Promise<EbpfCaps | null>>()

function fetchRequestCaps(backendUrl: string, force = false): Promise<EbpfCaps | null> {
  if (force || !requestCapsCache.has(backendUrl)) {
    const probe = fetch(`${backendUrl}/runs/attach/request-capabilities${force ? '?refresh=true' : ''}`)
      .then((r) => (r.ok ? (r.json() as Promise<EbpfCaps>) : null))
      .catch(() => null)
      .then((caps) => {
        if (caps === null) requestCapsCache.delete(backendUrl)
        return caps
      })
    requestCapsCache.set(backendUrl, probe)
  }
  return requestCapsCache.get(backendUrl)!
}

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
  const [ebpfCaps, setEbpfCaps] = useState<EbpfCaps | null>(null)
  const [requests, setRequests] = useState(false)
  const [reqCaps, setReqCaps] = useState<EbpfCaps | null>(null)
  // in-flight attach key: `pid-<n>` for a listed row or manual PID, `port-<n>`
  // for a manual port. A single string lets rows and the manual affordance share
  // one busy gate (only one attach can be in flight).
  const [busy, setBusy] = useState<string | null>(null)
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
  const rescan = () => {
    load()
    setEbpfCaps(null)
    setReqCaps(null)
    void fetchEbpfCaps(backendUrl, true).then(setEbpfCaps)
    void fetchRequestCaps(backendUrl, true).then(setReqCaps)
  }
  useEffect(load, [backendUrl])
  useEffect(() => {
    void fetchEbpfCaps(backendUrl).then(setEbpfCaps)
    void fetchRequestCaps(backendUrl).then(setReqCaps)
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

  // The list is only the top targets by RSS, so a small/short-lived process may
  // never appear. When the filter is a bare number, offer a direct attach to it
  // as a PID or a port (the endpoint accepts either) — an escape from the picker.
  const manualId = useMemo(() => {
    const q = filter.trim()
    return /^\d+$/.test(q) ? Number(q) : null
  }, [filter])

  const postAttach = async (body: Record<string, unknown>, key: string) => {
    setBusy(key)
    setError(null)
    try {
      const r = await fetch(`${backendUrl}/runs/attach`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          window_s: windowS, monitor,
          ebpf: ebpf && !!ebpfCaps?.available,
          requests: requests && !!reqCaps?.available,
          session_id: sessionId ?? null,
          ...body,
        }),
      })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        // surface the backend's 404 (no listening process on the port) / 400 inline
        throw new Error(d.detail || `HTTP ${r.status}`)
      }
      onAttached?.()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(null)
    }
  }

  const attach = (t: Target) => postAttach({ pid: t.pid }, `pid-${t.pid}`)
  const attachManual = (kind: 'pid' | 'port', id: number) =>
    postAttach(kind === 'pid' ? { pid: id } : { port: id }, `${kind}-${id}`)

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
          <button type="button" className="ai-btn" onClick={rescan} title="rescan">↻</button>
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

        <label className={`attach__monitor${reqCaps && !reqCaps.available ? ' attach__monitor--off' : ''}`}>
          <input
            type="checkbox" checked={requests && !!reqCaps?.available}
            disabled={!reqCaps?.available}
            onChange={(e) => setRequests(e.target.checked)}
          />
          <span>
            <span className="attach__monitor-label">
              Request tracing (HTTP endpoints + DB)
              {reqCaps && !reqCaps.available && <span className="attach__ebpf-tag"> unavailable</span>}
            </span>
            <span className="attach__monitor-sub">
              {reqCaps?.available
                ? 'Adds a per-endpoint latency table (RED) for a plaintext HTTP/1.x server, and — with a dynamically-linked libpq — attributes each request’s time to its Postgres queries.'
                : (reqCaps?.reason ?? 'Checking request-tracing capabilities…')}
            </span>
          </span>
        </label>

        {error && <div className="attach__error">✗ {error}</div>}

        <div className="attach__list">
          {targets === null && <div className="attach__empty">Scanning processes…</div>}
          {targets && shown.length === 0 && (
            manualId !== null ? (
              <div
                className="attach__empty"
                data-testid="attach-manual"
                style={{ display: 'flex', gap: 8, alignItems: 'center', justifyContent: 'center', flexWrap: 'wrap' }}
              >
                <span>No matching processes. Attach directly to</span>
                <button
                  type="button" className="ai-btn" data-testid="attach-manual-pid"
                  disabled={busy !== null} onClick={() => attachManual('pid', manualId)}
                >
                  {busy === `pid-${manualId}` ? 'attaching…' : `PID ${manualId}`}
                </button>
                <button
                  type="button" className="ai-btn" data-testid="attach-manual-port"
                  disabled={busy !== null} onClick={() => attachManual('port', manualId)}
                >
                  {busy === `port-${manualId}` ? 'attaching…' : `port ${manualId}`}
                </button>
              </div>
            ) : (
              <div className="attach__empty">
                No matching processes — type a PID or port number to attach one directly.
              </div>
            )
          )}
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
              <span className="attach__hint">{busy === `pid-${t.pid}` ? 'attaching…' : t.hint}</span>
            </button>
          ))}
        </div>

        <div className="modal__foot">
          <span className="attach__foot-note">
            Samples the process for the window, then builds a flamegraph. Native/Go
            use perf; interpreted runtimes get real frames from their own samplers
            (py-spy, rbspy, async-profiler, the V8 inspector, phpspy) — see each row's
            hint. Tick the eBPF option for off-CPU + latency.
          </span>
        </div>
      </div>
    </div>
  )
}
