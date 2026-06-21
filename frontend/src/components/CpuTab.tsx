import type { MetricSample } from '../state/useOpenTrace'
import type { RunDetail } from '../state/useRunDetail'
import { TimeSeriesChart } from './TimeSeriesChart'

function pts(metrics: MetricSample[], key: keyof MetricSample): [number, number][] {
  return metrics
    .filter((m) => m[key] != null)
    .map((m) => [m.timestamp_ms, m[key] as number])
}

export function CpuTab({ detail }: { detail: RunDetail }) {
  const { metrics, anomalies } = detail
  const cpu = pts(metrics, 'cpu_pct')
  const syscalls = pts(metrics, 'syscall_rate')
  const cpuBound = anomalies.find((a) => a.rule_id === 'cpu_bound_no_syscalls')
  const peakCpu = cpu.length ? Math.max(...cpu.map((p) => p[1])) : null

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
