import { useEffect, useState } from 'react'

interface Props {
  backendUrl: string
  onClose: () => void
}

interface LlmSettings {
  base_url: string | null
  model: string | null
  configured: boolean
  has_key: boolean
}

const GOOGLE_BASE = 'https://generativelanguage.googleapis.com/v1beta/openai'

export function SettingsModal({ backendUrl, onClose }: Props) {
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [hasKey, setHasKey] = useState(false)
  const [test, setTest] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    fetch(`${backendUrl}/config/llm`)
      .then((r) => r.json())
      .then((d: LlmSettings) => {
        setBaseUrl(d.base_url ?? '')
        setModel(d.model ?? '')
        setHasKey(d.has_key)
      })
      .catch(() => {})
  }, [backendUrl])

  const save = async (): Promise<boolean> => {
    setSaving(true)
    const body: Record<string, string> = { base_url: baseUrl, model }
    if (apiKey.trim()) body.api_key = apiKey.trim()
    try {
      const r = await fetch(`${backendUrl}/config/llm`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      return r.ok
    } finally {
      setSaving(false)
    }
  }

  const onTest = async () => {
    setTest('Testing…')
    await save()
    try {
      const r = await fetch(`${backendUrl}/config/llm/test`, { method: 'POST' })
      const d = await r.json()
      setTest(
        d.ok
          ? `✓ Connected — ${d.models_count} models${d.model_available ? '' : ' (configured model NOT found)'}`
          : `✗ ${d.error}`,
      )
    } catch (e) {
      setTest(`✗ ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const onSave = async () => {
    if (await save()) onClose()
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h2>Settings — LLM</h2>
          <button type="button" className="modal__close" onClick={onClose} aria-label="close">×</button>
        </div>
        <div className="modal__body">
          <label className="field">
            <span>Base URL (OpenAI-compatible)</span>
            <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={GOOGLE_BASE} />
            <button type="button" className="ai-link" onClick={() => setBaseUrl(GOOGLE_BASE)}>
              use Google Gemini
            </button>
          </label>
          <label className="field">
            <span>Model</span>
            <input value={model} onChange={(e) => setModel(e.target.value)}
              placeholder="gemma-4-26b-a4b-it · gemini-2.0-flash (faster)" />
          </label>
          <label className="field">
            <span>API key</span>
            <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)}
              placeholder={hasKey ? '•••••••• (stored — leave blank to keep)' : 'paste API key'} />
            <span className="field__hint">Stored in your OS-local secret store, never in config.</span>
          </label>
          {test && <div className="modal__test">{test}</div>}
        </div>
        <div className="modal__foot">
          <button type="button" className="ai-btn" onClick={onTest} disabled={saving}>Test connection</button>
          <button type="button" className="ai-btn ai-btn--primary" onClick={onSave} disabled={saving}>Save</button>
        </div>
      </div>
    </div>
  )
}
