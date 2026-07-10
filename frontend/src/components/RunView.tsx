import { memo, type ReactNode } from 'react'
import type { Incident, Requests, Run } from '../state/useOpenTrace'
import type { RunDetail } from '../state/useRunDetail'
import { useLiveMetrics } from '../state/liveMetrics'
import type { ViewDef } from './SecondaryTabs'
import { ErrorBoundary } from './ErrorBoundary'
import { IncidentFeed } from './IncidentFeed'
import { TabGuide } from './TabGuide'
import { OverviewTab } from './OverviewTab'
import { MemoryTab } from './MemoryTab'
import { CpuTab } from './CpuTab'
import { SyscallTab } from './SyscallTab'
import { IoTab } from './IoTab'
import { NetworkTab } from './NetworkTab'
import { LogsTab } from './LogsTab'
import { TimelineTab } from './TimelineTab'
import { ProcessesTab } from './ProcessesTab'
import { ProfilingTab } from './ProfilingTab'
import { FlamegraphTab } from './FlamegraphTab'
import { LatencyTab } from './LatencyTab'
import { RequestsTab } from './RequestsTab'
import { FilesTab } from './FilesTab'

/** Views for a specific run, reflecting which collectors actually ran:
 *  - psutil  -> Overview / Timeline / Memory / CPU (always shown)
 *  - strace|ltrace -> the syscall-derived tabs (I/O, Network, Processes, Syscalls)
 *  - strace only   -> Logs (reconstructed from strace's write-data; ltrace can't)
 *  - ltrace  -> Profiling   ·   perf -> Flamegraph
 * Runs with no recorded collectors (older runs) fall back to the full strace set. */
export function runViews(run: Run | null): ViewDef[] {
  const c = run?.collector_config ?? {}
  const known = Object.keys(c).length > 0
  const hasSyscalls = !known || !!c.strace || !!c.ltrace
  const hasStrace = !known || !!c.strace
  const views: ViewDef[] = [
    { key: 'overview', label: 'Overview' },
  ]
  if (c.monitor) views.push({ key: 'incidents', label: 'Incidents' })
  if (c.requests) views.push({ key: 'requests', label: 'Requests' })
  views.push(
    { key: 'timeline', label: 'Timeline' },
    { key: 'memory', label: 'Memory' },
    { key: 'cpu', label: 'CPU' },
  )
  if (hasSyscalls)
    views.push(
      { key: 'io', label: 'I/O' },
      { key: 'network', label: 'Network' },
      { key: 'processes', label: 'Processes' },
      { key: 'syscalls', label: 'Syscalls' },
    )
  if (hasStrace) views.push({ key: 'logs', label: 'Logs' })
  if (c.ltrace) views.push({ key: 'profiling', label: 'Profiling' })
  if (c.perf) views.push({ key: 'flamegraph', label: 'Flamegraph' })
  if (c.ebpf) views.push({ key: 'latency', label: 'Latency' })
  views.push({ key: 'files', label: 'Files' })
  return views
}

interface Props {
  run: Run
  detail: RunDetail
  activeView: string
  backendUrl: string
  onOpenSettings: () => void
  /** Optional banner rendered above the view (e.g. the name-this-run prompt). */
  topSlot?: ReactNode
  /** monitor-mode incidents (live from SSE) + a stop control for a live monitor run. */
  incidents?: Incident[]
  /** live request-tracing rollup (SSE) for a monitor run — preferred by the Requests tab. */
  requestsLive?: Requests | null
  onStopMonitor?: () => void
}

/** Overview with the live metric subscription scoped to this subtree, so the
 *  4Hz SSE sample stream re-renders only the visible Overview of a live run. */
function LiveOverview({
  run, detail, backendUrl, onOpenSettings, incidents,
}: Pick<Props, 'run' | 'detail' | 'backendUrl' | 'onOpenSettings' | 'incidents'>) {
  const live = useLiveMetrics(run.status === 'running' ? run.id : null)
  return (
    <OverviewTab
      run={run}
      detail={detail}
      live={live}
      backendUrl={backendUrl}
      onOpenSettings={onOpenSettings}
      incidents={incidents}
    />
  )
}

/** Main content for an open run: renders the selected analytics view. */
export const RunView = memo(function RunView({
  run, detail, activeView, backendUrl, onOpenSettings, topSlot, incidents, requestsLive,
  onStopMonitor,
}: Props) {
  const isLiveMonitor = !!run.collector_config?.monitor && run.status === 'running'
  return (
    <div className="region region--main-content run-view" data-placeholder="main-content">
      {isLiveMonitor && (
        <div className="monitor-bar">
          <span className="monitor-bar__dot" />
          <span className="monitor-bar__label">Monitoring live — capturing incidents</span>
          <button type="button" className="ai-btn monitor-bar__stop" onClick={onStopMonitor}>
            Stop
          </button>
        </div>
      )}
      {topSlot}
      {/* one bad view renders an inline error + Retry instead of blanking the app;
          switching tab or run resets the boundary */}
      <ErrorBoundary resetKey={`${run.id}:${activeView}`}>
      {activeView === 'incidents' && (
        <IncidentFeed backendUrl={backendUrl} runId={run.id} live={incidents ?? []} />
      )}
      {activeView === 'overview' && (
        <LiveOverview
          run={run}
          detail={detail}
          backendUrl={backendUrl}
          onOpenSettings={onOpenSettings}
          incidents={incidents}
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
      {activeView === 'profiling' && <ProfilingTab backendUrl={backendUrl} runId={run.id} />}
      {activeView === 'flamegraph' && (
        <FlamegraphTab backendUrl={backendUrl} runId={run.id} offCpu={!!run.collector_config?.ebpf} />
      )}
      {activeView === 'latency' && <LatencyTab backendUrl={backendUrl} runId={run.id} />}
      {activeView === 'requests' && (
        <RequestsTab backendUrl={backendUrl} runId={run.id} live={requestsLive} />
      )}
      {activeView === 'files' && <FilesTab backendUrl={backendUrl} runId={run.id} />}
      </ErrorBoundary>
      <TabGuide view={activeView} />
    </div>
  )
})
