import type { ReactNode } from 'react'

export function MainTabBarPlaceholder({ rightSlot }: { rightSlot?: ReactNode }) {
  return (
    <div className="region region--main-tabs" data-placeholder="main-tab-bar">
      <span className="region__label">Main tab bar</span>
      {rightSlot && <div className="region__right-slot">{rightSlot}</div>}
    </div>
  )
}
