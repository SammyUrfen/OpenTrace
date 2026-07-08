/** Path basename for display: `/usr/bin/python3` → `python3`, null → `?`.
 *  (Distinct from `state/text.ts::commandBasename`, which extracts the first
 *  token of a full command line.) */
export function basename(p: string | null): string {
  if (!p) return '?'
  const i = p.lastIndexOf('/')
  return i >= 0 ? p.slice(i + 1) || p : p
}
