import { useEffect, useRef, useState } from 'react'
import { useCollectors } from '../state/useCollectors'
import { COLLECTOR_ROWS } from './collectorRows'
import { LlmConfigForm, type LlmConfigHandle } from './LlmConfigForm'
import { ToolChecklist, type ToolInfo } from './ToolChecklist'
import { UsageGuide } from './UsageGuide'
import { apiFetch } from '../state/api'

interface Props {
  backendUrl: string
  onDone: () => void
}

const STEPS = ['Welcome', 'Tracing tools', 'AI model', 'Collectors']

/**
 * First-launch setup: a quick how-to, then a tracing-tool check (with install
 * hints for anything missing), optional LLM config, and collector defaults.
 */
export function FirstRunWizard({ backendUrl, onDone }: Props) {
  const [step, setStep] = useState(0)
  const [tools, setTools] = useState<ToolInfo[] | null>(null)
  const llmRef = useRef<LlmConfigHandle>(null)
  const { collectors, toggle } = useCollectors(backendUrl)

  // refresh=true bypasses the backend's TTL cache (recheck after installing a tool)
  const loadTools = (refresh = false) =>
    apiFetch(`${backendUrl}/info/tools${refresh ? '?refresh=true' : ''}`).then((r) => r.json()).then((d) => setTools(d.tools)).catch(() => {})
  useEffect(() => { void loadTools() }, [backendUrl])

  // The LLM form unmounts when leaving step 2, so persist it on any navigation
  // away (the form is refetched from the saved config if the user comes back).
  const saveLlm = async () => { await llmRef.current?.save() }

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
            <ToolChecklist tools={tools ?? []} />
          </div>
        )}

        {step === 2 && (
          <div className="modal__body">
            <p className="wizard__lead">
              Connect an AI model for plain-English run summaries. Optional — every
              run is fully analyzed by the rule engine without it.
            </p>
            <LlmConfigForm ref={llmRef} backendUrl={backendUrl} />
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
              {COLLECTOR_ROWS.map((c) => (
                <label key={c.key} className="collector">
                  <input
                    type="checkbox"
                    checked={collectors ? collectors[c.key] : false}
                    disabled={!collectors}
                    onChange={() => toggle(c.key)}
                  />
                  <span className="collector__text">
                    <span className="collector__label">{c.label}</span>
                    <span className="collector__sub">{c.sub}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>
        )}

        <div className="modal__foot">
          {step > 0 && (
            <button type="button" className="ai-btn" onClick={async () => { if (step === 2) await saveLlm(); back() }}>
              Back
            </button>
          )}
          {step === 1 && <button type="button" className="ai-btn" onClick={() => void loadTools(true)}>↻ recheck</button>}
          {step === 2 && (
            <button type="button" className="ai-btn" onClick={() => void llmRef.current?.test()}>
              Test connection
            </button>
          )}
          {step < STEPS.length - 1 ? (
            <button type="button" className="ai-btn ai-btn--primary" onClick={async () => { if (step === 2) await saveLlm(); next() }}>
              {step === 2 ? 'Next' : 'Continue'}
            </button>
          ) : (
            <button type="button" className="ai-btn ai-btn--primary" onClick={onDone}>Start tracing</button>
          )}
        </div>
      </div>
    </div>
  )
}
