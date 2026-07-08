import type { Collectors } from '../state/useCollectors'

/** Canonical collector rows shared by the wizard, settings, and live monitor.
 *  Order matters: e2e addresses perf as the 4th checkbox. */
export const COLLECTOR_ROWS: { key: keyof Collectors; label: string; sub: string }[] = [
  { key: 'psutil', label: 'Resource metrics', sub: 'CPU · Memory · FDs · threads' },
  { key: 'strace', label: 'Syscall trace', sub: 'Syscalls · I/O · Network · Logs' },
  { key: 'ltrace', label: 'Library calls', sub: 'malloc/free · library hotspots (replaces Syscall trace)' },
  { key: 'perf', label: 'Hardware perf', sub: 'CPU flamegraph · function hotspots' },
]
