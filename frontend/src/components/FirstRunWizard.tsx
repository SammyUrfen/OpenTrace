import { useEffect, useState } from 'react'
import { useCollectors } from '../state/useCollectors'
import { UsageGuide } from './UsageGuide'

interface Props {
  backendUrl: string
  onDone: () => void
}

interface ToolInfo {
  name: string
  label: string
  unlocks: string
  available: boolean
  version: string | null
  install_hint: string | null
  warning?: string
}

const GOOGLE_BASE = 'https://generativelanguage.googleapis.com/v1beta/openai'
const STEPS = ['Welcome', 'Tracing tools', 'AI model', 'Collectors']

/**
 * First-launch setup: a quick how-to, then a tracing-tool check (with install
 * hints for anything missing), optional LLM config, and collector defaults.
 */
export function FirstRunWizard({ backendUrl, onDone }: Props) {
  const [step, setStep] = useState(0)
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [test, setTest] = useState<string | null>(null)
  const [tools, setTools] = useState<ToolInfo[] | null>(null)
  const { collectors, toggle } = useCollectors(backendUrl)

  useEffect(() => {
    fetch(`${backendUrl}/config/llm`).then((r) => r.json()).then((d) => {
      setBaseUrl(d.base_url ?? '')
      setModel(d.model ?? '')
    }).catch(() => {})
  }, [backendUrl])

  const loadTools = () =>
    fetch(`${backendUrl}/info/tools`).then((r) => r.json()).then((d) => setTools(d.tools)).catch(() => {})
  useEffect(() => { void loadTools() }, [backendUrl])

  const saveLlm = async () => {
    const body: Record<string, string> = { base_url: baseUrl, model }
    if (apiKey.trim()) body.api_key = apiKey.trim()
    try {
      await fetch(`${backendUrl}/config/llm`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch { /* ignore */ }
  }

  const onTest = async () => {
    setTest('Testing…')
    await saveLlm()
    try {
      const d = await fetch(`${backendUrl}/config/llm/test`, { method: 'POST' }).then((r) => r.json())
      setTest(d.ok ? `✓ Connected — ${d.models_count} models` : `✗ ${d.error}`)
    } catch (e) {
      setTest(`✗ ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const finish = async () => {
    await saveLlm()
    onDone()
  }

  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1))
  const back = () => setStep((s) => Math.max(s - 1, 0))

  return (
    <div className="modal-backdrop">
      <div className="modal modal--wizard" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h2>Welcome to OpenTrace</h2>
          <span className="wizard__step">{STEPS[step]} · {step + 1}/{STEPS.length}</span>
        </div>

        {step === 0 && (
          <div className="modal__body"><UsageGuide /></div>
        )}

        {step === 1 && (
          <div className="modal__body">
            <p className="wizard__lead">
              OpenTrace drives these tools. Installed ones are ready; install any
              you want and click recheck. You can change this any time in Settings.
            </p>
            {(tools ?? []).map((t) => (
              <div key={t.name} className={`tool ${t.available ? 'tool--ok' : 'tool--missing'}`}>
                <div className="tool__head">
                  <span className="tool__name">{t.available ? '✓' : '✗'} {t.name}</span>
                  <span className="tool__version">{t.available ? t.version : 'not installed'}</span>
                </div>
                <div className="tool__sub">{t.label} — {t.unlocks}</div>
                {t.warning && <div className="tool__warn">⚠ {t.warning}</div>}
                {!t.available && t.install_hint && (
                  <code className="tool__hint" title="copy" onClick={() => void navigator.clipboard?.writeText(t.install_hint!)}>
                    {t.install_hint}
                  </code>
                )}
              </div>
            ))}
          </div>
        )}

        {step === 2 && (
          <div className="modal__body">
            <p className="wizard__lead">
              Connect an AI model for plain-English run summaries. Optional — every
              run is fully analyzed by the rule engine without it.
            </p>
            <label className="field">
              <span>Base URL (OpenAI-compatible)</span>
              <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={GOOGLE_BASE} />
              <button type="button" className="ai-link" onClick={() => setBaseUrl(GOOGLE_BASE)}>use Google Gemini</button>
            </label>
            <label className="field">
              <span>Model</span>
              <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="gemini-2.0-flash" />
            </label>
            <label className="field">
              <span>API key</span>
              <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="paste API key (stored in the OS secret store)" />
            </label>
            {test && <div className="modal__test">{test}</div>}
          </div>
        )}

        {step === 3 && (
          <div className="modal__body">
            <p className="wizard__lead">
              Choose which collectors run when tracing is on. strace and library
              calls are mutually exclusive; perf can run with either. Change these
              any time in the Live Monitor or Settings.
            </p>
            <div className="wizard__collectors">
              {([
                ['psutil', 'Resource metrics', 'CPU · Memory · FDs'],
                ['strace', 'Syscall trace', 'Syscalls · I/O · Network · Logs'],
                ['ltrace', 'Library calls', 'malloc/free · hotspots (replaces Syscall trace)'],
                ['perf', 'Hardware perf', 'CPU flamegraph'],
              ] as const).map(([key, label, sub]) => (
                <label key={key} className="collector">
                  <input
                    type="checkbox"
                    checked={collectors ? collectors[key] : false}
                    disabled={!collectors}
                    onChange={() => toggle(key)}
                  />
                  <span className="collector__text">
                    <span className="collector__label">{label}</span>
                    <span className="collector__sub">{sub}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>
        )}

        <div className="modal__foot">
          {step > 0 && <button type="button" className="ai-btn" onClick={back}>Back</button>}
          {step === 1 && <button type="button" className="ai-btn" onClick={loadTools}>↻ recheck</button>}
          {step === 2 && <button type="button" className="ai-btn" onClick={onTest}>Test connection</button>}
          {step < STEPS.length - 1 ? (
            <button type="button" className="ai-btn ai-btn--primary" onClick={async () => { if (step === 2) await saveLlm(); next() }}>
              {step === 2 ? 'Next' : 'Continue'}
            </button>
          ) : (
            <button type="button" className="ai-btn ai-btn--primary" onClick={finish}>Start tracing</button>
          )}
        </div>
      </div>
    </div>
  )
}
