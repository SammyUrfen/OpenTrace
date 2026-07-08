import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'

export const GOOGLE_BASE = 'https://generativelanguage.googleapis.com/v1beta/openai'

export interface LlmConfigHandle {
  /** PUT the current base-url/model (+ API key only if one was typed). */
  save: () => Promise<void>
  /** Save, then POST /config/llm/test and show the result inline. */
  test: () => Promise<void>
}

interface Props {
  backendUrl: string
  /** Receives the fetched /config/llm payload (e.g. to read continuous_summaries). */
  onLoaded?: (cfg: Record<string, unknown>) => void
}

/**
 * LLM connection form (base URL + Gemini shortcut + model + API key + inline
 * test result), shared by the first-run wizard and the settings AI pane.
 * Save/test are exposed imperatively so each host drives them from its own
 * footer/action row.
 */
export const LlmConfigForm = forwardRef<LlmConfigHandle, Props>(function LlmConfigForm(
  { backendUrl, onLoaded },
  ref,
) {
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [hasKey, setHasKey] = useState(false)
  const [test, setTest] = useState<string | null>(null)
  const onLoadedRef = useRef(onLoaded)
  onLoadedRef.current = onLoaded

  useEffect(() => {
    fetch(`${backendUrl}/config/llm`).then((r) => r.json()).then((d) => {
      setBaseUrl(d.base_url ?? '')
      setModel(d.model ?? '')
      setHasKey(!!d.has_key)
      onLoadedRef.current?.(d)
    }).catch(() => {})
  }, [backendUrl])

  const save = async () => {
    const body: Record<string, string> = { base_url: baseUrl, model }
    if (apiKey.trim()) body.api_key = apiKey.trim()
    try {
      await fetch(`${backendUrl}/config/llm`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch { /* ignore */ }
  }

  const runTest = async () => {
    setTest('Testing…')
    await save()
    try {
      const d = await fetch(`${backendUrl}/config/llm/test`, { method: 'POST' }).then((r) => r.json())
      setTest(d.ok
        ? `✓ Connected — ${d.models_count} models${d.model_available ? '' : ' (configured model NOT found)'}`
        : `✗ ${d.error}`)
    } catch (e) {
      setTest(`✗ ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  useImperativeHandle(ref, () => ({ save, test: runTest }))

  return (
    <>
      <label className="field">
        <span>Base URL (OpenAI-compatible)</span>
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={GOOGLE_BASE} />
        <button type="button" className="ai-link" onClick={() => setBaseUrl(GOOGLE_BASE)}>use Google Gemini</button>
      </label>
      <label className="field">
        <span>Model</span>
        <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="gemma-4-26b-a4b-it · gemini-2.0-flash (faster)" />
      </label>
      <label className="field">
        <span>API key</span>
        <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)}
          placeholder={hasKey ? '•••••••• (stored — leave blank to keep)' : 'paste API key'} />
        <span className="field__hint">
          Stored locally as a permission-restricted file in OpenTrace's data
          folder (secrets/), never in config.json or git.
        </span>
      </label>
      {test && <div className="modal__test">{test}</div>}
    </>
  )
})
