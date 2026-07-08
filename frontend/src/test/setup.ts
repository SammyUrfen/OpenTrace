import '@testing-library/jest-dom/vitest'
import { beforeEach, vi } from 'vitest'
import { clearRunCache } from '../state/runCache'

// The run fetch-cache is module-level state: reset it between tests so one
// test's mocked payload can never satisfy another test's fetch for the same
// run id.
beforeEach(() => {
  clearRunCache()
})

// jsdom has no fetch/EventSource; stub benign defaults so components that load
// data on mount don't throw during unit tests. Individual tests can override.
if (!('fetch' in globalThis)) {
  // @ts-expect-error test stub
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  )
}
if (!window.matchMedia) {
  window.matchMedia = ((q: string) => ({
    matches: false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false
    },
    onchange: null,
  })) as unknown as typeof window.matchMedia
}
if (!('EventSource' in globalThis)) {
  // @ts-expect-error test stub
  globalThis.EventSource = class {
    close() {}
    onmessage: ((e: MessageEvent) => void) | null = null
    onerror: (() => void) | null = null
    onopen: (() => void) | null = null
  }
}
