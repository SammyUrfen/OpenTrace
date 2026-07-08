export interface ToolInfo {
  name: string
  label: string
  unlocks: string
  available: boolean
  version: string | null
  install_hint: string | null
  warning?: string
}

/** Tracing-tool check cards (installed/missing + copyable install hint),
 *  shared by the first-run wizard and the settings Tools pane. */
export function ToolChecklist({ tools }: { tools: ToolInfo[] }) {
  return (
    <>
      {tools.map((t) => (
        <div key={t.name} className={`tool ${t.available ? 'tool--ok' : 'tool--missing'}`}>
          <div className="tool__head">
            <span className="tool__name">{t.available ? '✓' : '✗'} {t.name}</span>
            <span className="tool__version">{t.available ? t.version : 'not installed'}</span>
          </div>
          <div className="tool__sub">{t.label} — {t.unlocks}</div>
          {t.warning && <div className="tool__warn">⚠ {t.warning}</div>}
          {!t.available && t.install_hint && (
            <code className="tool__hint" title="copy to clipboard"
              onClick={() => void navigator.clipboard?.writeText(t.install_hint!)}>
              {t.install_hint}
            </code>
          )}
        </div>
      ))}
    </>
  )
}
