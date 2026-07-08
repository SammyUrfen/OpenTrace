import { useMemo } from 'react'
import type { RunDetail } from '../state/useRunDetail'
import { maxOf, pts } from './seriesUtils'
import { TimeSeriesChart } from './TimeSeriesChart'

export function CpuTab({ detail }: { detail: RunDetail }) {
  const { metrics, anomalies } = detail
  const cpu = useMemo(() => pts(metrics, 'cpu_pct'), [metrics])
  const syscalls = useMemo(() => pts(metrics, 'syscall_rate'), [metrics])
  const cpuBound = anomalies.find((a) => a.rule_id === 'cpu_bound_no_syscalls')
  const peakCpu = maxOf(cpu, (p) => p[1])

  return (
    <div className="overview" data-testid="cpu-tab">
      <h3 className="overview__h">CPU usage over time</h3>
      {cpuBound && (
        <div className="banner banner--warn">⚠ {cpuBound.title}</div>
      )}
      <TimeSeriesChart
        series={[{ name: 'CPU', color: '#60a5fa', points: cpu, area: true }]}
        thresholds={[
          { value: 50, label: '50%', color: '#fbbf24' },
          { value: 90, label: '90%', color: '#f87171' },
        ]}
        yUnit="%"
        height={200}
      />
      <p className="chart-caption">
        CPU% (summed across the process tree; can exceed 100% on multiple cores).
        Peak {peakCpu == null ? '—' : `${peakCpu.toFixed(0)}%`}.
      </p>

      <h3 className="overview__h">Syscall rate — is it CPU-bound or I/O-bound?</h3>
      <TimeSeriesChart
        series={[{ name: 'syscalls', color: '#34d399', points: syscalls, area: true }]}
        yUnit="/s"
        height={160}
      />
      <p className="chart-caption">
        High CPU with a <b>low</b> syscall rate ⇒ compute-bound (a hot loop). High
        syscall rate ⇒ I/O- or syscall-bound.
      </p>
    </div>
  )
}
