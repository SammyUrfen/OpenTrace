import { describe, expect, it } from 'vitest'
import type { MetricSample } from '../state/useOpenTrace'
import { downsamplePoints, downsampleValues, maxOf, minOf, pts, type Point } from './seriesUtils'

describe('maxOf/minOf', () => {
  it('returns null for empty arrays and the extreme otherwise', () => {
    expect(maxOf([], (v: number) => v)).toBeNull()
    expect(minOf([], (v: number) => v)).toBeNull()
    expect(maxOf([3, 1, 7, 2], (v) => v)).toBe(7)
    expect(minOf([3, 1, 7, 2], (v) => v)).toBe(1)
  })

  it('handles series far beyond the spread argument limit', () => {
    // Math.max(...arr) throws RangeError past ~125k args — the loop must not.
    const big = new Array<number>(300_000).fill(1)
    big[123_456] = 42
    expect(maxOf(big, (v) => v)).toBe(42)
  })
})

describe('pts', () => {
  it('maps metric rows to [t, v] and skips null samples', () => {
    const metrics = [
      { timestamp_ms: 1, cpu_pct: 10 },
      { timestamp_ms: 2, cpu_pct: null },
      { timestamp_ms: 3, cpu_pct: 30 },
    ] as unknown as MetricSample[]
    expect(pts(metrics, 'cpu_pct')).toEqual([[1, 10], [3, 30]])
  })
})

describe('downsamplePoints', () => {
  it('is a no-op at or under budget', () => {
    const small: Point[] = [[1, 1], [2, 2], [3, 3]]
    expect(downsamplePoints(small)).toBe(small)
  })

  it('bounds output size and preserves global extremes in time order', () => {
    const points: Point[] = Array.from({ length: 100_000 }, (_, i) => [i, 50])
    points[70_000] = [70_000, 999] // spike
    points[20_000] = [20_000, -7] // dip
    const out = downsamplePoints(points)
    expect(out.length).toBeLessThanOrEqual(1500)
    expect(maxOf(out, (p) => p[1])).toBe(999)
    expect(minOf(out, (p) => p[1])).toBe(-7)
    for (let i = 1; i < out.length; i++) expect(out[i][0]).toBeGreaterThanOrEqual(out[i - 1][0])
  })
})

describe('downsampleValues', () => {
  it('bounds output size and preserves extremes', () => {
    const values = new Array<number>(50_000).fill(10)
    values[31_337] = 500
    values[100] = -3
    const out = downsampleValues(values)
    expect(out.length).toBeLessThanOrEqual(600)
    expect(maxOf(out, (v) => v)).toBe(500)
    expect(minOf(out, (v) => v)).toBe(-3)
  })
})
