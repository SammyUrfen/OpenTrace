export interface ViewDef {
  key: string
  label: string
}

interface Props {
  views: ViewDef[]
  active: string
  onSelect: (key: string) => void
}

/** Secondary tab bar scoped to the active run (Overview / Memory / CPU / …). */
export function SecondaryTabs({ views, active, onSelect }: Props) {
  return (
    <div className="region region--secondary-tabs" data-placeholder="secondary-tab-bar">
      <div className="secondary-tabs">
        {views.map((v) => (
          <button
            key={v.key}
            type="button"
            className={`secondary-tab ${v.key === active ? 'secondary-tab--active' : ''}`}
            onClick={() => onSelect(v.key)}
            role="tab"
            data-view={v.key}
            aria-selected={v.key === active}
          >
            {v.label}
          </button>
        ))}
      </div>
    </div>
  )
}
