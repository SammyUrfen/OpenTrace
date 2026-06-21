import type { LiveState, Run } from '../state/useOpenTrace'
import type { RunDetail } from '../state/useRunDetail'
import type { ViewDef } from './SecondaryTabs'
import { OverviewTab } from './OverviewTab'
import { MemoryTab } from './MemoryTab'
import { CpuTab } from './CpuTab'
import { SyscallTab } from './SyscallTab'
import { IoTab } from './IoTab'
import { NetworkTab } from './NetworkTab'
import { LogsTab } from './LogsTab'
import { TimelineTab } from './TimelineTab'
import { ProcessesTab } from './ProcessesTab'

/** The set of analytics views available for a run. Extended as tabs land. */
export const RUN_VIEWS: ViewDef[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'timeline', label: 'Timeline' },
  { key: 'memory', label: 'Memory' },
  { key: 'cpu', label: 'CPU' },
  { key: 'io', label: 'I/O' },
  { key: 'network', label: 'Network' },
  { key: 'processes', label: 'Processes' },
  { key: 'syscalls', label: 'Syscalls' },
  { key: 'logs', label: 'Logs' },
]

interface Props {
  run: Run
  detail: RunDetail
  live: LiveState | null
  activeView: string
  backendUrl: string
  onOpenSettings: () => void
}

/** Main content for an open run: renders the selected analytics view. */
export function RunView({ run, detail, live, activeView, backendUrl, onOpenSettings }: Props) {
  return (
    <div className="region region--main-content run-view" data-placeholder="main-content">
      {activeView === 'overview' && (
        <OverviewTab
          run={run}
          detail={detail}
          live={live}
          backendUrl={backendUrl}
          onOpenSettings={onOpenSettings}
        />
      )}
      {activeView === 'timeline' && (
        <TimelineTab backendUrl={backendUrl} runId={run.id} detail={detail} />
      )}
      {activeView === 'memory' && <MemoryTab detail={detail} />}
      {activeView === 'cpu' && <CpuTab detail={detail} />}
      {activeView === 'io' && <IoTab backendUrl={backendUrl} runId={run.id} />}
      {activeView === 'network' && <NetworkTab backendUrl={backendUrl} runId={run.id} />}
      {activeView === 'processes' && <ProcessesTab backendUrl={backendUrl} runId={run.id} />}
      {activeView === 'syscalls' && <SyscallTab backendUrl={backendUrl} runId={run.id} />}
      {activeView === 'logs' && (
        <LogsTab backendUrl={backendUrl} runId={run.id} anomalies={detail.anomalies} />
      )}
    </div>
  )
}
