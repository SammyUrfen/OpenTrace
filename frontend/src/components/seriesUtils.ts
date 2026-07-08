/**
 * Loop/bucket helpers for metric-derived series, shared by every chart consumer.
 *
 * Monitor-mode runs persist a 250ms sample stream indefinitely, so these series
 * can reach hundreds of thousands of points. Spread-based `Math.max(...arr)`
 * throws RangeError past ~125k arguments (blanking the app), and un-decimated
 * SVG paths freeze the renderer — use `maxOf`/`minOf` and the downsamplers
 * instead of spreads over anything metric-derived.
 */
import type { MetricSample } from '../state/useOpenTrace'

export type Point = [number, number]

/** Loop-based max over `sel(item)`; `null` for an empty array. */
export function maxOf<T>(arr: readonly T[], sel: (v: T) => number): number | null {
  let m: number | null = null
  for (const v of arr) {
    const n = sel(v)
    if (m === null || n > m) m = n
  }
  return m
}

/** Loop-based min over `sel(item)`; `null` for an empty array. */
export function minOf<T>(arr: readonly T[], sel: (v: T) => number): number | null {
  let m: number | null = null
  for (const v of arr) {
    const n = sel(v)
    if (m === null || n < m) m = n
  }
  return m
}

/** Metric rows → [timestampMs, value] points, skipping null samples. */
export function pts(metrics: MetricSample[], key: keyof MetricSample): Point[] {
  return metrics
    .filter((m) => m[key] != null)
    .map((m) => [m.timestamp_ms, m[key] as number])
}

/**
 * Bucketed min/max decimation for time-ordered points: reduces a series to at
 * most `budget` points while keeping both extremes of every bucket (in time
 * order), so spikes and leak slopes survive. No-op at or under budget.
 */
export function downsamplePoints(points: Point[], budget = 1500): Point[] {
  if (points.length <= budget) return points
  const buckets = Math.max(1, Math.floor(budget / 2))
  const step = points.length / buckets
  const out: Point[] = []
  for (let b = 0; b < buckets; b++) {
    const start = Math.floor(b * step)
    const end = b === buckets - 1 ? points.length : Math.floor((b + 1) * step)
    let lo = points[start]
    let hi = points[start]
    for (let i = start + 1; i < end; i++) {
      const p = points[i]
      if (p[1] < lo[1]) lo = p
      if (p[1] > hi[1]) hi = p
    }
    if (lo === hi) out.push(lo)
    else if (lo[0] <= hi[0]) out.push(lo, hi)
    else out.push(hi, lo)
  }
  return out
}

/** Same bucketed min/max decimation for a plain value series (sparklines). */
export function downsampleValues(values: number[], budget = 600): number[] {
  if (values.length <= budget) return values
  const buckets = Math.max(1, Math.floor(budget / 2))
  const step = values.length / buckets
  const out: number[] = []
  for (let b = 0; b < buckets; b++) {
    const start = Math.floor(b * step)
    const end = b === buckets - 1 ? values.length : Math.floor((b + 1) * step)
    let loI = start
    let hiI = start
    for (let i = start + 1; i < end; i++) {
      if (values[i] < values[loI]) loI = i
      if (values[i] > values[hiI]) hiI = i
    }
    if (loI === hiI) out.push(values[loI])
    else if (loI <= hiI) out.push(values[loI], values[hiI])
    else out.push(values[hiI], values[loI])
  }
  return out
}
