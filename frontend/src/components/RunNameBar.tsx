import { useState } from 'react'
import type { Run } from '../state/useOpenTrace'

interface Props {
  run: Run
  onRename: (name: string) => void
  onDismiss: () => void
}

/**
 * A non-blocking prompt that appears at the top of a run that just finished,
 * offering to rename it (the default is the `<cmd>_<timestamp>` name). It never
 * steals focus from the terminal — you can ignore it and it stays out of the
 * way, or click in to give the run a memorable name. Opt-out in Settings.
 *
 * The parent keys this by run id, so each run gets a fresh instance — no stale
 * input bleeds across runs. It deliberately does NOT autofocus: the bar must
 * never steal keystrokes away from the terminal.
 */
export function RunNameBar({ run, onRename, onDismiss }: Props) {
  const [name, setName] = useState(run.label ?? run.display_name)

  const save = () => {
    const n = name.trim()
    if (n && n !== (run.label ?? run.display_name)) onRename(n)
    onDismiss()
  }

  return (
    <div className="run-namebar">
      <span className="run-namebar__label" title={run.command}>Name this run:</span>
      <input
        className="run-namebar__input"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') save()
          else if (e.key === 'Escape') onDismiss()
        }}
        aria-label="run name"
      />
      <button type="button" className="ai-btn ai-btn--primary run-namebar__save" onClick={save}>
        Save
      </button>
      <button type="button" className="ai-btn run-namebar__skip" onClick={onDismiss}>
        Keep default
      </button>
    </div>
  )
}
