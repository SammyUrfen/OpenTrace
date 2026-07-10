import { useEffect, useRef, useState } from 'react'
import type { Run } from '../state/useOpenTrace'
import type { ViewDef } from './SecondaryTabs'
import { useDiff, type RunBundle } from '../state/useDiff'
import { commandBasename } from '../state/text'
import { Markdown } from './Markdown'
import { MemoryDiff, CpuDiff, SyscallDiff, AnomalyDiff } from './DiffPanels'
import { sseUrl } from '../state/api'

type AiStatus = 'idle' | 'thinking' | 'streaming' | 'done' | 'error'

/** Streams an AI comparison of the two runs (no cache; regenerated on click). */
function AiDiffSummary({ backendUrl, aId, bId }: { backendUrl: string; aId: string; bId: string }) {
  const [text, setText] = useState('')
  const [status, setStatus] = useState<AiStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    setText('')
    setStatus('idle')
    setError(null)
    esRef.current?.close()
    return () => esRef.current?.close()
  }, [aId, bId])

  const generate = () => {
    esRef.current?.close()
    setText('')
    setError(null)
    setStatus('thinking')
    const es = new EventSource(sseUrl(`${backendUrl}/diff/${aId}/${bId}/ai-summary/stream`))
    esRef.current = es
    es.onmessage = (e) => {
      let m: { type: string; text?: string; message?: string }
      try {
        m = JSON.parse(e.data)
      } catch {
        return
      }
      if (m.type === 'content') {
        setStatus('streaming')
        setText((t) => t + (m.text ?? ''))
      } else if (m.type === 'error') {
        setStatus('error')
        setError(m.message ?? 'unknown error')
        es.close()
      } else if (m.type === 'done') {
        setStatus('done')
        es.close()
      }
    }
    es.onerror = () => {
      setStatus((s) => (s === 'done' || s === 'streaming' ? s : 'error'))
      es.close()
    }
  }

  return (
    <div className="ai-summary">
      <div className="ai-summary__head">
        <span className="ai-summary__title">
          <span className={`ai-dot ${status === 'thinking' || status === 'streaming' ? 'ai-dot--live' : ''}`} />
          AI Diff Summary
        </span>
        {(status === 'idle' || status === 'done' || status === 'error') && (
          <button type="button" className="ai-btn ai-btn--primary" onClick={generate}>
            {status === 'idle' ? '✨ Compare with AI' : '↻ Re-analyze'}
          </button>
        )}
      </div>
      {status === 'thinking' && (
        <div className="ai-summary__thinking"><span className="ai-spinner" /> Comparing the two runs…</div>
      )}
      {status === 'error' && <div className="ai-summary__error">AI diff failed: {error}</div>}
      {(status === 'streaming' || status === 'done') && text && (
        <div className="ai-summary__body"><Markdown text={text} />{status === 'streaming' && <span className="ai-caret">▋</span>}</div>
      )}
    </div>
  )
}

export const DIFF_VIEWS: ViewDef[] = [
  { key: 'overview', label: 'Overview Δ' },
  { key: 'memory', label: 'Memory Δ' },
  { key: 'cpu', label: 'CPU Δ' },
  { key: 'syscalls', label: 'Syscalls Δ' },
  { key: 'anomalies', label: 'Anomalies Δ' },
]

interface Props {
  backendUrl: string
  aId: string
  bId: string
  runs: Run[]
  activeView: string
}

function num(b: RunBundle, path: (s: RunBundle) => number | null | undefined): number | null {
  const v = path(b)
  return v == null ? null : v
}

/** One A | B | Δ row. lowerBetter colours an improvement green, regression red. */
function DiffRow({
  label, a, b, suffix = '', lowerBetter = true, digits = 0,
}: {
  label: string
  a: number | null
  b: number | null
  suffix?: string
  lowerBetter?: boolean
  digits?: number
}) {
  const fmt = (v: number | null) => (v == null ? '—' : `${v.toFixed(digits)}${suffix}`)
  let deltaEl = <span className="diff-delta">—</span>
  if (a != null && b != null) {
    const d = b - a
    const pct = a !== 0 ? (d / Math.abs(a)) * 100 : null
    const improved = lowerBetter ? d < 0 : d > 0
    const cls = d === 0 ? 'diff-delta' : improved ? 'diff-delta diff-delta--good' : 'diff-delta diff-delta--bad'
    const sign = d > 0 ? '+' : ''
    deltaEl = (
      <span className={cls}>
        {sign}{d.toFixed(digits)}{suffix}
        {pct != null && Math.abs(pct) >= 0.5 && <span className="diff-pct"> ({sign}{pct.toFixed(0)}%)</span>}
      </span>
    )
  }
  return (
    <div className="diff-row">
      <span className="diff-row__label">{label}</span>
      <span className="diff-row__a">{fmt(a)}</span>
      <span className="diff-row__b">{fmt(b)}</span>
      {deltaEl}
    </div>
  )
}

function OverviewDiff({
  a, b, backendUrl, aId, bId,
}: {
  a: RunBundle; b: RunBundle; backendUrl: string; aId: string; bId: string
}) {
  const peak = (bundle: RunBundle, k: 'cpu_pct' | 'rss_mb' | 'open_fds' | 'threads') =>
    num(bundle, (s) => s.summary?.peaks?.[k])
  const total = (bundle: RunBundle, k: 'syscall_events' | 'errors') =>
    num(bundle, (s) => s.summary?.totals?.[k])

  return (
    <div className="overview" data-testid="overview-diff">
      <div className="diff-header">
        <div className="diff-header__col">
          <div className="diff-header__tag diff-header__tag--a">A</div>
          <div className="diff-header__name" title={a.run?.command}>{a.run?.label ?? a.run?.display_name ?? a.run?.id}</div>
        </div>
        <span className="diff-header__vs">↔</span>
        <div className="diff-header__col">
          <div className="diff-header__tag diff-header__tag--b">B</div>
          <div className="diff-header__name" title={b.run?.command}>{b.run?.label ?? b.run?.display_name ?? b.run?.id}</div>
        </div>
      </div>

      <AiDiffSummary backendUrl={backendUrl} aId={aId} bId={bId} />

      <h3 className="overview__h">What changed</h3>
      <div className="diff-grid">
        <div className="diff-row diff-row--head">
          <span className="diff-row__label">metric</span>
          <span className="diff-row__a">A</span>
          <span className="diff-row__b">B</span>
          <span className="diff-delta">Δ (B−A)</span>
        </div>
        <DiffRow label="duration" a={a.run?.duration_ms ?? null} b={b.run?.duration_ms ?? null} suffix="ms" />
        <DiffRow label="peak CPU" a={peak(a, 'cpu_pct')} b={peak(b, 'cpu_pct')} suffix="%" />
        <DiffRow label="peak RSS" a={peak(a, 'rss_mb')} b={peak(b, 'rss_mb')} suffix=" MB" />
        <DiffRow label="peak FDs" a={peak(a, 'open_fds')} b={peak(b, 'open_fds')} />
        <DiffRow label="threads" a={peak(a, 'threads')} b={peak(b, 'threads')} />
        <DiffRow label="syscalls" a={total(a, 'syscall_events')} b={total(b, 'syscall_events')} />
        <DiffRow label="errors" a={total(a, 'errors')} b={total(b, 'errors')} />
        <DiffRow label="anomalies" a={a.anomalies.length} b={b.anomalies.length} />
      </div>
    </div>
  )
}

export function DiffView({ backendUrl, aId, bId, runs, activeView }: Props) {
  const { a, b, loading } = useDiff(backendUrl, aId, bId, runs)
  const title = `${commandBasename(a.run?.command ?? '?')} ↔ ${commandBasename(b.run?.command ?? '?')}`

  return (
    <div className="region region--main-content run-view" data-placeholder="main-content" aria-label={title}>
      {loading && !a.summary && !b.summary ? (
        <div className="overview"><div className="overview__muted">Loading comparison…</div></div>
      ) : (
        <>
          {activeView === 'overview' && (
            <OverviewDiff a={a} b={b} backendUrl={backendUrl} aId={aId} bId={bId} />
          )}
          {activeView === 'memory' && <MemoryDiff a={a} b={b} />}
          {activeView === 'cpu' && <CpuDiff a={a} b={b} />}
          {activeView === 'syscalls' && <SyscallDiff a={a} b={b} />}
          {activeView === 'anomalies' && <AnomalyDiff a={a} b={b} />}
        </>
      )}
    </div>
  )
}
