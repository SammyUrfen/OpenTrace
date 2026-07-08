import { useEffect, useRef, useState } from 'react'
import type { Collectors } from '../state/useCollectors'
import { COLLECTOR_ROWS } from './collectorRows'
import { LlmConfigForm, type LlmConfigHandle } from './LlmConfigForm'
import { ToolChecklist, type ToolInfo } from './ToolChecklist'
import { UsageGuide } from './UsageGuide'

export type SettingsSection = 'general' | 'collectors' | 'ai' | 'tools' | 'guide' | 'about'

interface Props {
  backendUrl: string
  onClose: () => void
  initialSection?: SettingsSection
  themeResolved: 'dark' | 'light'
  onToggleTheme: () => void
  collectors: Collectors | null
  onToggleCollector: (k: keyof Collectors) => void
  namePrompt: boolean
  onToggleNamePrompt: () => void
}

const NAV: { key: SettingsSection; label: string }[] = [
  { key: 'general', label: 'General' },
  { key: 'collectors', label: 'Collectors' },
  { key: 'ai', label: 'AI / LLM' },
  { key: 'tools', label: 'Tracing tools' },
  { key: 'guide', label: 'Guide' },
  { key: 'about', label: 'About' },
]

export function SettingsPage({
  backendUrl, onClose, initialSection = 'general',
  themeResolved, onToggleTheme, collectors, onToggleCollector,
  namePrompt, onToggleNamePrompt,
}: Props) {
  const [section, setSection] = useState<SettingsSection>(initialSection)
  useEffect(() => setSection(initialSection), [initialSection])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="settings-backdrop" onMouseDown={onClose}>
      <div className="settings" onMouseDown={(e) => e.stopPropagation()}>
        <div className="settings__nav">
          <div className="settings__title">Settings</div>
          {NAV.map((n) => (
            <button
              key={n.key}
              type="button"
              className={`settings__navitem ${section === n.key ? 'settings__navitem--active' : ''}`}
              onClick={() => setSection(n.key)}
            >
              {n.label}
            </button>
          ))}
          <button type="button" className="settings__close" onClick={onClose}>Close ✕</button>
        </div>
        <div className="settings__content">
          {section === 'general' && (
            <GeneralPane
              backendUrl={backendUrl}
              themeResolved={themeResolved}
              onToggleTheme={onToggleTheme}
              namePrompt={namePrompt}
              onToggleNamePrompt={onToggleNamePrompt}
            />
          )}
          {section === 'collectors' && (
            <CollectorsPane collectors={collectors} onToggle={onToggleCollector} />
          )}
          {section === 'ai' && <AiPane backendUrl={backendUrl} />}
          {section === 'tools' && <ToolsPane backendUrl={backendUrl} />}
          {section === 'guide' && (
            <section className="settings__pane"><h3 className="settings__h">How to use OpenTrace</h3><UsageGuide /></section>
          )}
          {section === 'about' && <AboutPane backendUrl={backendUrl} />}
        </div>
      </div>
    </div>
  )
}

function GeneralPane({ backendUrl, themeResolved, onToggleTheme, namePrompt, onToggleNamePrompt }: {
  backendUrl: string; themeResolved: 'dark' | 'light'; onToggleTheme: () => void
  namePrompt: boolean; onToggleNamePrompt: () => void
}) {
  const [info, setInfo] = useState<Record<string, unknown> | null>(null)
  useEffect(() => {
    fetch(`${backendUrl}/info`).then((r) => r.json()).then(setInfo).catch(() => {})
  }, [backendUrl])
  return (
    <section className="settings__pane">
      <h3 className="settings__h">General</h3>
      <div className="settings__row">
        <div><div className="settings__rowlabel">Theme</div>
          <div className="settings__rowsub">Espresso (dark) / warm paper (light)</div></div>
        <button type="button" className="ai-btn" onClick={onToggleTheme}>
          {themeResolved === 'dark' ? '☾ Dark' : '☀ Light'} — switch
        </button>
      </div>
      <div className="settings__row">
        <div><div className="settings__rowlabel">Prompt to name each run</div>
          <div className="settings__rowsub">Show a quick rename bar when a run finishes (you can always rename later by double-clicking a tab)</div></div>
        <button type="button" className="ai-btn" onClick={onToggleNamePrompt}>
          {namePrompt ? 'On' : 'Off'} — toggle
        </button>
      </div>
      <h4 className="settings__h2">Data locations</h4>
      <dl className="settings__kv">
        <div><dt>Home</dt><dd>{String(info?.home ?? '…')}</dd></div>
        <div><dt>Database</dt><dd>{String(info?.db_path ?? '…')}</dd></div>
        <div><dt>Config</dt><dd>{String(info?.config_path ?? '…')}</dd></div>
        <div><dt>Sessions</dt><dd>{String(info?.sessions_dir ?? '…')}</dd></div>
      </dl>
    </section>
  )
}

function CollectorsPane({ collectors, onToggle }: {
  collectors: Collectors | null; onToggle: (k: keyof Collectors) => void
}) {
  return (
    <section className="settings__pane">
      <h3 className="settings__h">Collectors</h3>
      <p className="settings__note">
        strace and ltrace both use ptrace and can't trace one process at once, so
        they're mutually exclusive. perf (sampling) and resource metrics run
        alongside either.
      </p>
      {COLLECTOR_ROWS.map((c) => (
        <label key={c.key} className="settings__collector">
          <input
            type="checkbox"
            checked={collectors ? collectors[c.key] : false}
            disabled={!collectors}
            onChange={() => onToggle(c.key)}
          />
          <span>
            <span className="settings__rowlabel">{c.label}</span>
            <span className="settings__rowsub">{c.sub}</span>
          </span>
        </label>
      ))}
    </section>
  )
}

function AiPane({ backendUrl }: { backendUrl: string }) {
  const formRef = useRef<LlmConfigHandle>(null)
  const [continuous, setContinuous] = useState(false)
  const [saving, setSaving] = useState(false)

  const drive = async (action: 'save' | 'test') => {
    setSaving(true)
    try {
      await formRef.current?.[action]()
    } finally {
      setSaving(false)
    }
  }
  const toggleContinuous = () => {
    const next = !continuous
    setContinuous(next)
    // send ONLY the flag — resending empty base_url/model (before the mount GET
    // resolves, or when unset) would null out the saved LLM config.
    void fetch(`${backendUrl}/config/llm`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ continuous_summaries: next }),
    })
  }

  return (
    <section className="settings__pane">
      <h3 className="settings__h">AI / LLM</h3>
      <LlmConfigForm
        ref={formRef}
        backendUrl={backendUrl}
        onLoaded={(d) => setContinuous(!!d.continuous_summaries)}
      />
      <div className="settings__actions">
        <button type="button" className="ai-btn" onClick={() => void drive('test')} disabled={saving}>Test connection</button>
        <button type="button" className="ai-btn ai-btn--primary" onClick={() => void drive('save')} disabled={saving}>Save</button>
      </div>
      <div className="settings__row" style={{ marginTop: 14 }}>
        <div><div className="settings__rowlabel">Continuous incident summaries</div>
          <div className="settings__rowsub">In monitor mode, auto-explain each detected incident with a short AI note (where in code + likely cause). One request per incident.</div></div>
        <button type="button" className="ai-btn" onClick={toggleContinuous} disabled={saving}>
          {continuous ? 'On' : 'Off'} — toggle
        </button>
      </div>
    </section>
  )
}

function ToolsPane({ backendUrl }: { backendUrl: string }) {
  const [data, setData] = useState<{ tools: ToolInfo[]; perf_event_paranoid: number | null } | null>(null)
  // refresh=true bypasses the backend's TTL cache (recheck after installing a tool)
  const load = (refresh = false) =>
    fetch(`${backendUrl}/info/tools${refresh ? '?refresh=true' : ''}`).then((r) => r.json()).then(setData).catch(() => {})
  useEffect(() => { void load() }, [backendUrl])

  return (
    <section className="settings__pane">
      <h3 className="settings__h">Tracing tools
        <button type="button" className="ai-btn settings__refresh" onClick={() => void load(true)}>↻ recheck</button>
      </h3>
      <p className="settings__note">
        OpenTrace drives these external tools. Install any that are missing to
        unlock their collector; recheck after installing.
      </p>
      <ToolChecklist tools={data?.tools ?? []} />
      {data && (
        <div className="settings__rowsub" style={{ marginTop: 8 }}>
          perf_event_paranoid = {String(data.perf_event_paranoid)} (≤2 lets perf
          profile your own processes)
        </div>
      )}
    </section>
  )
}

function AboutPane({ backendUrl }: { backendUrl: string }) {
  const [info, setInfo] = useState<Record<string, unknown> | null>(null)
  useEffect(() => {
    fetch(`${backendUrl}/info`).then((r) => r.json()).then(setInfo).catch(() => {})
  }, [backendUrl])
  return (
    <section className="settings__pane">
      <h3 className="settings__h">About OpenTrace</h3>
      <p className="guide__lead">
        A local-first Linux observability tool. It traces the commands you run
        (strace / ltrace + psutil, optional perf), detects anomalies, and presents
        correlated findings — so you can understand program behavior without
        juggling strace, lsof, htop, and friends by hand.
      </p>
      <dl className="settings__kv">
        <div><dt>Version</dt><dd>{String(info?.version ?? '…')}</dd></div>
        <div><dt>Schema</dt><dd>{String(info?.schema_version ?? '…')}</dd></div>
        <div><dt>CPU cores</dt><dd>{String(info?.cpu_cores ?? '…')}</dd></div>
      </dl>
    </section>
  )
}
