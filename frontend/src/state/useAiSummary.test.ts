import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useAiSummary } from './useAiSummary'

// Controllable EventSource: capture instances so the test can drive messages.
class MockES {
  static instances: MockES[] = []
  url: string
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  constructor(url: string) {
    this.url = url
    MockES.instances.push(this)
  }
  close() {
    this.closed = true
  }
  emit(obj: unknown) {
    this.onmessage?.({ data: JSON.stringify(obj) })
  }
}

beforeEach(() => {
  MockES.instances = []
  // @ts-expect-error test override
  globalThis.EventSource = MockES
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({ text: null, configured: true }) }),
  ) as unknown as typeof fetch
})

describe('useAiSummary', () => {
  it('keeps streaming across unmount/remount (tab switch does not abort)', async () => {
    const url = 'http://x'
    const run = 'run-switch'
    const { result, unmount } = renderHook(() => useAiSummary(url, run))
    // flush the on-mount cached-summary fetch (resolves to "no cached summary")
    await act(async () => {})

    act(() => result.current.generate(false))
    expect(result.current.status).toBe('thinking')

    const es = MockES.instances[0]
    act(() => es.emit({ type: 'content', text: 'Hello ' }))
    expect(result.current.text).toBe('Hello ')
    expect(result.current.status).toBe('streaming')

    // Switch away: the Overview tab (and this hook) unmounts.
    unmount()

    // The stream is NOT closed and keeps accumulating while nothing is mounted.
    expect(es.closed).toBe(false)
    act(() => es.emit({ type: 'content', text: 'world' }))

    // Switch back: a fresh hook for the same run sees the accumulated text + live status.
    const { result: result2 } = renderHook(() => useAiSummary(url, run))
    expect(result2.current.text).toBe('Hello world')
    expect(result2.current.status).toBe('streaming')

    act(() => es.emit({ type: 'done' }))
    expect(result2.current.status).toBe('done')
    expect(es.closed).toBe(true)
  })

  it('loads a cached summary on mount', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ json: () => Promise.resolve({ text: 'cached summary', configured: true }) }),
    ) as unknown as typeof fetch
    const { result } = renderHook(() => useAiSummary('http://x', 'run-cached'))
    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(result.current.text).toBe('cached summary')
    expect(result.current.cached).toBe(true)
  })

  it('reports unconfigured when no LLM is set up', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ json: () => Promise.resolve({ text: null, configured: false }) }),
    ) as unknown as typeof fetch
    const { result } = renderHook(() => useAiSummary('http://x', 'run-unconfig'))
    await waitFor(() => expect(result.current.status).toBe('unconfigured'))
  })
})
