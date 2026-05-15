type BackendStatus = 'connecting' | 'ok' | 'unreachable'

const STATUS_LABEL: Record<BackendStatus, string> = {
  connecting: 'connecting…',
  ok: 'connected',
  unreachable: 'unreachable',
}

export function MainContentPlaceholder({ backendStatus }: { backendStatus: BackendStatus }) {
  return (
    <div className="region region--main-content" data-placeholder="main-content">
      <div className="region__label">Main content area</div>
      <div className={`backend-badge backend-badge--${backendStatus}`}>
        Backend: {STATUS_LABEL[backendStatus]}
      </div>
    </div>
  )
}

export type { BackendStatus }
