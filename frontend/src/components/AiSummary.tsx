import { useAiSummary } from '../state/useAiSummary'
import { Markdown } from './Markdown'

interface Props {
  backendUrl: string
  runId: string
  onOpenSettings: () => void
}

/** The AI Summary card in the Overview tab: streams a model-written analysis. */
export function AiSummary({ backendUrl, runId, onOpenSettings }: Props) {
  const { text, status, error, generate } = useAiSummary(backendUrl, runId)

  return (
    <div className="ai-summary">
      <div className="ai-summary__head">
        <span className="ai-summary__title">
          <span className={`ai-dot ${status === 'thinking' || status === 'streaming' ? 'ai-dot--live' : ''}`} />
          AI Summary
        </span>
        {(status === 'done' || status === 'error') && (
          <button type="button" className="ai-btn" onClick={() => generate(true)}>
            ↻ Re-analyze
          </button>
        )}
      </div>

      {status === 'unconfigured' && (
        <div className="ai-summary__cta">
          Connect an LLM to get an interpreted summary of this run.{' '}
          <button type="button" className="ai-link" onClick={onOpenSettings}>
            Configure in Settings →
          </button>
        </div>
      )}

      {status === 'idle' && (
        <div className="ai-summary__cta">
          <button type="button" className="ai-btn ai-btn--primary" onClick={() => generate(false)}>
            ✨ Generate AI summary
          </button>
        </div>
      )}

      {status === 'thinking' && (
        <div className="ai-summary__thinking">
          <span className="ai-spinner" /> Analyzing the trace… (the model is reasoning)
        </div>
      )}

      {status === 'error' && (
        <div className="ai-summary__error">
          AI summary failed: {error}{' '}
          <button type="button" className="ai-link" onClick={onOpenSettings}>
            Check Settings
          </button>
        </div>
      )}

      {(status === 'streaming' || status === 'done') && text && (
        <div className="ai-summary__body">
          <Markdown text={text} />
          {status === 'streaming' && <span className="ai-caret">▋</span>}
        </div>
      )}
    </div>
  )
}
