import { useState } from 'react'
import type { SyscallStat } from '../state/useSyscalls'

type SortKey = keyof SyscallStat

const COLUMNS: { key: SortKey; label: string; numeric: boolean }[] = [
  { key: 'syscall', label: 'syscall', numeric: false },
  { key: 'count', label: 'count', numeric: true },
  { key: 'total_ms', label: 'total ms', numeric: true },
  { key: 'avg_ms', label: 'avg ms', numeric: true },
  { key: 'p50_ms', label: 'p50', numeric: true },
  { key: 'p95_ms', label: 'p95', numeric: true },
  { key: 'p99_ms', label: 'p99', numeric: true },
  { key: 'errors', label: 'errors', numeric: true },
  { key: 'pct_runtime', label: '% time', numeric: true },
]

function fmt(v: number | string | null): string {
  if (v == null) return '—'
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2)
  return v
}

/** Pure, sortable per-syscall table. Click a header to sort. */
export function SyscallTable({ rows }: { rows: SyscallStat[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('total_ms')
  const [asc, setAsc] = useState(false)

  const sorted = [...rows].sort((a, b) => {
    const av = a[sortKey]
    const bv = b[sortKey]
    if (typeof av === 'string' || typeof bv === 'string') {
      return asc
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av))
    }
    const an = (av as number) ?? -1
    const bn = (bv as number) ?? -1
    return asc ? an - bn : bn - an
  })

  const onSort = (key: SortKey) => {
    if (key === sortKey) setAsc((v) => !v)
    else {
      setSortKey(key)
      setAsc(false)
    }
  }

  return (
    <table className="syscall-table" data-testid="syscall-table">
      <thead>
        <tr>
          {COLUMNS.map((c) => (
            <th
              key={c.key}
              className={c.numeric ? 'num' : ''}
              onClick={() => onSort(c.key)}
              aria-sort={sortKey === c.key ? (asc ? 'ascending' : 'descending') : 'none'}
            >
              {c.label}
              {sortKey === c.key && <span className="sort-caret">{asc ? ' ▲' : ' ▼'}</span>}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((r) => (
          <tr key={r.syscall}>
            <td className="syscall-name">{r.syscall}</td>
            <td className="num">{fmt(r.count)}</td>
            <td className="num">{fmt(r.total_ms)}</td>
            <td className="num">{fmt(r.avg_ms)}</td>
            <td className="num">{fmt(r.p50_ms)}</td>
            <td className="num">{fmt(r.p95_ms)}</td>
            <td className="num">{fmt(r.p99_ms)}</td>
            <td className={`num ${r.errors > 0 ? 'errs' : ''}`}>{fmt(r.errors)}</td>
            <td className="num">{r.pct_runtime.toFixed(1)}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
