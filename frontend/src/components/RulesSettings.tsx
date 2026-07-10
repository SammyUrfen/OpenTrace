import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../state/api'

interface BuiltinRule {
  id: string
  signal: string
  label: string
  description: string
  enabled: boolean
  thresholds: Record<string, number>
}

interface CustomRule {
  id: string
  name: string
  description: string
  signal: string
  expression: string
  severity: string
  enabled: boolean
  min_count: number
  duration_ms: number
  created_at: number
}

interface Validation {
  ok: boolean
  error: string | null
  fields: string[]
}

/** `slow_syscall_ms` -> "slow syscall ms" (unit inferred from the suffix). */
function thresholdLabel(name: string): { label: string; unit: string } {
  const parts = name.split('_')
  const unit =
    name.endsWith('_ms') ? 'ms' : name.endsWith('_pct') ? '%' :
    name.endsWith('_mb') ? 'MB' : name.endsWith('_bps') ? 'B/s' :
    name.endsWith('_ratio') ? 'ratio' : 'count'
  const stripped = ['ms', 'pct', 'mb', 'bps', 'ratio', 'min', 'count'].includes(parts[parts.length - 1])
    ? parts.slice(0, -1) : parts
  return { label: stripped.join(' '), unit }
}

function BuiltinRuleRow({ rule, backendUrl, onChanged }: {
  rule: BuiltinRule; backendUrl: string; onChanged: (r: BuiltinRule) => void
}) {
  const [pending, setPending] = useState<Record<string, string>>({})

  const toggle = () => {
    apiFetch(`${backendUrl}/rules/builtin/${rule.id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !rule.enabled }),
    }).then((r) => r.json()).then(onChanged).catch(() => {})
  }
  const saveThreshold = (name: string) => {
    const raw = pending[name]
    if (raw === undefined) return
    const value = Number(raw)
    if (!Number.isFinite(value)) return
    apiFetch(`${backendUrl}/rules/builtin/${rule.id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thresholds: { [name]: value } }),
    }).then((r) => r.json()).then((updated) => {
      onChanged(updated)
      setPending((p) => {
        const rest = { ...p }
        delete rest[name]
        return rest
      })
    }).catch(() => {})
  }

  return (
    <div className={`rule-row ${rule.enabled ? '' : 'rule-row--off'}`}>
      <div className="rule-row__main">
        <div className="settings__rowlabel">{rule.label}</div>
        <div className="settings__rowsub">{rule.description}</div>
        {Object.keys(rule.thresholds).length > 0 && (
          <div className="rule-row__thresholds">
            {Object.entries(rule.thresholds).map(([name, value]) => {
              const { label, unit } = thresholdLabel(name)
              return (
                <label key={name} className="rule-threshold" title={name}>
                  <span>{label}</span>
                  <input
                    type="number"
                    value={pending[name] ?? String(value)}
                    onChange={(e) => setPending((p) => ({ ...p, [name]: e.target.value }))}
                    onBlur={() => saveThreshold(name)}
                    onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                  />
                  <span className="rule-threshold__unit">{unit}</span>
                </label>
              )
            })}
          </div>
        )}
      </div>
      <button type="button" className="ai-btn" onClick={toggle}>
        {rule.enabled ? 'On' : 'Off'} — toggle
      </button>
    </div>
  )
}

function CustomRuleForm({ backendUrl, initial, onSaved, onCancel, onDelete }: {
  backendUrl: string
  initial: CustomRule | null
  onSaved: (r: CustomRule) => void
  onCancel: () => void
  onDelete?: () => void
}) {
  const [name, setName] = useState(initial?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [signal, setSignal] = useState<'events' | 'metrics'>((initial?.signal as 'events' | 'metrics') ?? 'metrics')
  const [expression, setExpression] = useState(initial?.expression ?? '')
  const [severity, setSeverity] = useState(initial?.severity ?? 'medium')
  const [minCount, setMinCount] = useState(String(initial?.min_count ?? 5))
  const [durationMs, setDurationMs] = useState(String(initial?.duration_ms ?? 5000))
  const [validation, setValidation] = useState<Validation | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current)
    debounce.current = setTimeout(() => {
      if (!expression.trim()) { setValidation(null); return }
      apiFetch(`${backendUrl}/rules/custom/validate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ signal, expression }),
      }).then((r) => r.json()).then(setValidation).catch(() => setValidation(null))
    }, 300)
    return () => { if (debounce.current) clearTimeout(debounce.current) }
  }, [backendUrl, signal, expression])

  const canSave = name.trim().length > 0 && validation?.ok === true

  const save = () => {
    setSaving(true)
    setSaveError(null)
    const body = {
      name: name.trim(), description, signal, expression,
      severity, min_count: Number(minCount) || 1, duration_ms: Number(durationMs) || 1000,
    }
    const req = initial
      ? apiFetch(`${backendUrl}/rules/custom/${initial.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        })
      : apiFetch(`${backendUrl}/rules/custom`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        })
    req.then(async (r) => {
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        throw new Error(d.detail || `HTTP ${r.status}`)
      }
      return r.json()
    }).then((r: CustomRule) => onSaved(r))
      .catch((e) => setSaveError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSaving(false))
  }

  return (
    <div className="rule-form">
      <div className="field">
        <span>Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Slow downstream retries" />
      </div>
      <div className="field">
        <span>Description (optional)</span>
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Shown as the anomaly's explanation" />
      </div>
      <div className="rule-form__row">
        <div className="field">
          <span>Signal</span>
          <select value={signal} onChange={(e) => setSignal(e.target.value as 'events' | 'metrics')}>
            <option value="metrics">Metrics (CPU/RSS/IO — sustained condition)</option>
            <option value="events">Events (syscalls — count of matches)</option>
          </select>
        </div>
        <div className="field">
          <span>Severity</span>
          <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="critical">Critical</option>
          </select>
        </div>
        {signal === 'metrics' ? (
          <div className="field">
            <span>Held for at least (ms)</span>
            <input type="number" value={durationMs} onChange={(e) => setDurationMs(e.target.value)} />
          </div>
        ) : (
          <div className="field">
            <span>Fires after N matches</span>
            <input type="number" value={minCount} onChange={(e) => setMinCount(e.target.value)} />
          </div>
        )}
      </div>
      <div className="field">
        <span>Expression</span>
        <textarea
          className="rule-form__expr"
          rows={2}
          value={expression}
          onChange={(e) => setExpression(e.target.value)}
          placeholder={signal === 'metrics' ? 'cpu_pct > 90 and syscall_rate < 5' : "syscall == 'openat' and error == 'ENOENT'"}
          spellCheck={false}
        />
        <span className="field__hint">
          Boolean expression only — comparisons, and/or/not, arithmetic, 'in'. No function calls.
          Available fields: {(validation?.fields ?? []).join(', ') || '…'}
        </span>
        {validation && (
          <span className={validation.ok ? 'rule-validate--ok' : 'rule-validate--err'}>
            {validation.ok ? '✓ valid expression' : `✗ ${validation.error}`}
          </span>
        )}
      </div>
      {saveError && <div className="rule-validate--err">{saveError}</div>}
      <div className="rule-form__actions">
        {initial && onDelete && (
          <button type="button" className="ai-btn ai-btn--danger" onClick={onDelete} style={{ marginRight: 'auto' }}>
            Delete
          </button>
        )}
        <button type="button" className="ai-btn" onClick={onCancel}>Cancel</button>
        <button type="button" className="ai-btn ai-btn--primary" onClick={save} disabled={!canSave || saving}>
          {initial ? 'Save' : 'Create'}
        </button>
      </div>
    </div>
  )
}

function CustomRuleRow({ rule, backendUrl, onChanged, onDeleted }: {
  rule: CustomRule; backendUrl: string
  onChanged: (r: CustomRule) => void; onDeleted: () => void
}) {
  const [editing, setEditing] = useState(false)

  const toggle = () => {
    apiFetch(`${backendUrl}/rules/custom/${rule.id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !rule.enabled }),
    }).then((r) => r.json()).then(onChanged).catch(() => {})
  }
  const del = () => {
    apiFetch(`${backendUrl}/rules/custom/${rule.id}`, { method: 'DELETE' }).then(() => onDeleted()).catch(() => {})
  }

  if (editing) {
    return (
      <CustomRuleForm
        backendUrl={backendUrl}
        initial={rule}
        onSaved={(r) => { onChanged(r); setEditing(false) }}
        onCancel={() => setEditing(false)}
        onDelete={del}
      />
    )
  }
  return (
    <div className={`rule-row custom-rule ${rule.enabled ? '' : 'rule-row--off'}`}>
      <div className="rule-row__main" onClick={() => setEditing(true)} role="button" tabIndex={0}>
        <div className="settings__rowlabel">
          {rule.name} <span className="rule-badge">{rule.signal}</span> <span className="rule-badge">{rule.severity}</span>
        </div>
        {rule.description && <div className="settings__rowsub">{rule.description}</div>}
        <code className="rule-form__expr-preview">{rule.expression}</code>
      </div>
      <div className="rule-row__actions">
        <button type="button" className="ai-btn" onClick={toggle}>{rule.enabled ? 'On' : 'Off'} — toggle</button>
      </div>
    </div>
  )
}

/** Settings -> Rules: toggle/tune the ~23 built-in anomaly rules, and author
 *  fully custom rules as a safe boolean expression over event/metric fields
 *  (validated live, sandboxed server-side — no function calls, no attribute
 *  access, so nothing beyond the listed fields is ever reachable). */
export function RulesSettings({ backendUrl }: { backendUrl: string }) {
  const [builtin, setBuiltin] = useState<BuiltinRule[] | null>(null)
  const [custom, setCustom] = useState<CustomRule[] | null>(null)
  const [creating, setCreating] = useState(false)

  const load = () => {
    apiFetch(`${backendUrl}/rules`).then((r) => r.json()).then((d) => {
      setBuiltin(d.builtin)
      setCustom(d.custom)
    }).catch(() => {})
  }
  useEffect(load, [backendUrl])

  const patchBuiltin = (updated: BuiltinRule) =>
    setBuiltin((prev) => prev?.map((r) => (r.id === updated.id ? updated : r)) ?? prev)
  const patchCustom = (updated: CustomRule) =>
    setCustom((prev) => prev?.map((r) => (r.id === updated.id ? updated : r)) ?? prev)
  const removeCustom = (id: string) =>
    setCustom((prev) => prev?.filter((r) => r.id !== id) ?? prev)

  const events = builtin?.filter((r) => r.signal === 'events') ?? []
  const metrics = builtin?.filter((r) => r.signal === 'metrics') ?? []

  return (
    <section className="settings__pane" style={{ maxWidth: 780 }}>
      <h3 className="settings__h">Rules</h3>
      <p className="settings__note">
        Every run is scored by the rule engine below. Turn individual rules off
        or retune their thresholds, and author your own from event/metric
        fields — a safe expression, not arbitrary code.
      </p>

      <h4 className="settings__h2">Built-in — events (syscall-based)</h4>
      {events.map((r) => <BuiltinRuleRow key={r.id} rule={r} backendUrl={backendUrl} onChanged={patchBuiltin} />)}

      <h4 className="settings__h2">Built-in — metrics (CPU/RSS/IO-based)</h4>
      {metrics.map((r) => <BuiltinRuleRow key={r.id} rule={r} backendUrl={backendUrl} onChanged={patchBuiltin} />)}

      <h4 className="settings__h2">
        Custom rules
        {!creating && (
          <button type="button" className="ai-btn settings__refresh" onClick={() => setCreating(true)}>+ new rule</button>
        )}
      </h4>
      {custom?.length === 0 && !creating && (
        <div className="overview__muted">No custom rules yet.</div>
      )}
      {custom?.map((r) => (
        <CustomRuleRow key={r.id} rule={r} backendUrl={backendUrl} onChanged={patchCustom} onDeleted={() => removeCustom(r.id)} />
      ))}
      {creating && (
        <CustomRuleForm
          backendUrl={backendUrl}
          initial={null}
          onSaved={(r) => { setCustom((prev) => [...(prev ?? []), r]); setCreating(false) }}
          onCancel={() => setCreating(false)}
        />
      )}
    </section>
  )
}
