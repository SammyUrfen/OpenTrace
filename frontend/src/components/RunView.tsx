import type { LiveState, Run } from '../state/useOpenTrace'
import type { RunDetail } from '../state/useRunDetail'
import type { ViewDef } from './SecondaryTabs'
import { OverviewTab } from './OverviewTab'
import { MemoryTab } from './MemoryTab'
import { CpuTab } from './CpuTab'
import { SyscallTab } from './SyscallTab'

/** The set of analytics views available for a run. Extended as tabs land. */
export const RUN_VIEWS: ViewDef[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'memory', label: 'Memory' },
  { key: 'cpu', label: 'CPU' },
  { key: 'syscalls', label: 'Syscalls' },
]

interface Props {
  run: Run
  detail: RunDetail
  live: LiveState | null
  activeView: string
  backendUrl: string
}

/** Main content for an open run: renders the selected analytics view. */
export function RunView({ run, detail, live, activeView, backendUrl }: Props) {
  return (
    <div className="region region--main-content run-view" data-placeholder="main-content">
      {activeView === 'overview' && (
        <OverviewTab run={run} detail={detail} live={live} />
      )}
      {activeView === 'memory' && <MemoryTab detail={detail} />}
      {activeView === 'cpu' && <CpuTab detail={detail} />}
      {activeView === 'syscalls' && <SyscallTab backendUrl={backendUrl} runId={run.id} />}
    </div>
  )
}
