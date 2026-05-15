interface Props {
  enabled: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
}

export function TracingToggle({ enabled, onChange, disabled }: Props) {
  return (
    <button
      type="button"
      className={`tracing-toggle tracing-toggle--${enabled ? 'on' : 'off'}`}
      onClick={() => onChange(!enabled)}
      disabled={disabled}
      aria-pressed={enabled}
    >
      <span className="tracing-toggle__dot" aria-hidden />
      OpenTrace {enabled ? 'ON' : 'OFF'}
    </button>
  )
}
