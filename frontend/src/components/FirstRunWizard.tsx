import { useEffect, useState } from 'react'
import { useCollectors } from '../state/useCollectors'

interface Props {
  backendUrl: string
  onDone: () => void
}

const GOOGLE_BASE = 'https://generativelanguage.googleapis.com/v1beta/openai'

/**
 * First-launch setup: optional LLM config, then tracing-collector defaults.
 * Reuses the same `/config/*` endpoints the Settings modal + Live Monitor use.
 */
export function FirstRunWizard({ backendUrl, onDone }: Props) {
  const [step, setStep] = useState(0)
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [test, setTest] = useState<string | null>(null)
  const { collectors, toggle } = useCollectors(backendUrl)

  useEffect(() => {
    fetch(`${backendUrl}/config/llm`)
      .then((r) => r.json())
      .then((d) => {
        setBaseUrl(d.base_url ?? '')
        setModel(d.model ?? '')
      })
      .catch(() => {})
  }, [backendUrl])

  const saveLlm = async () => {
    const body: Record<string, string> = { base_url: baseUrl, model }
    if (apiKey.trim()) body.api_key = apiKey.trim()
    try {
      await fetch(`${backendUrl}/config/llm`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch {
      /* ignore */
    }
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

  return (
    <div className="modal-backdrop">
      <div className="modal modal--wizard" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h2>Welcome to OpenTrace</h2>
          <span className="wizard__step">Step {step + 1} of 2</span>
        </div>

        {step === 0 && (
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

        {step === 1 && (
          <div className="modal__body">
            <p className="wizard__lead">
              Choose which collectors run when tracing is on. You can change these
              any time in the Live Monitor.
            </p>
            <div className="wizard__collectors">
              {([
                ['psutil', 'Resource metrics', 'CPU · Memory · FDs', true],
                ['strace', 'Syscall trace', 'Syscalls · I/O · Network · Processes', true],
                ['ltrace', 'Library calls', 'malloc/free — Phase 6', false],
                ['perf', 'Hardware perf', 'flamegraph — Phase 6', false],
              ] as const).map(([key, label, sub, enabled]) => (
                <label key={key} className={`collector ${enabled ? '' : 'collector--disabled'}`}>
                  <input
                    type="checkbox"
                    checked={collectors ? collectors[key] : false}
                    disabled={!enabled || !collectors}
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
          {step === 0 ? (
            <>
              <button type="button" className="ai-btn" onClick={() => setStep(1)}>Skip AI</button>
              <button type="button" className="ai-btn" onClick={onTest}>Test connection</button>
              <button type="button" className="ai-btn ai-btn--primary" onClick={async () => { await saveLlm(); setStep(1) }}>Next</button>
            </>
          ) : (
            <>
              <button type="button" className="ai-btn" onClick={() => setStep(0)}>Back</button>
              <button type="button" className="ai-btn ai-btn--primary" onClick={finish}>Start tracing</button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
