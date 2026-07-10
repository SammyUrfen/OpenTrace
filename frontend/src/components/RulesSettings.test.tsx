import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { RulesSettings } from './RulesSettings'

const BUILTIN = {
  id: 'failed_file_opens', signal: 'events', label: 'Failed file opens',
  description: 'App-level missing/forbidden files.', enabled: true,
  thresholds: { failed_open_min: 5 },
}
const BUILTIN_METRIC = {
  id: 'cpu_bound_metric', signal: 'metrics', label: 'Cpu bound metric',
  description: 'High CPU, no I/O.', enabled: true, thresholds: {},
}
const CUSTOM = {
  id: 'c1', name: 'Existing rule', description: '', signal: 'metrics',
  expression: 'cpu_pct > 90', severity: 'medium', enabled: true,
  min_count: 5, duration_ms: 5000, created_at: 0,
}

function jsonResponse(body: unknown, ok = true) {
  return Promise.resolve({ ok, status: ok ? 200 : 400, json: () => Promise.resolve(body) } as Response)
}

function installFetchMock(opts: { custom?: typeof CUSTOM[] } = {}) {
  const calls: { url: string; method: string; body: unknown }[] = []
  let custom = opts.custom ?? [CUSTOM]
  globalThis.fetch = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    calls.push({ url, method, body })

    if (url.endsWith('/rules') && method === 'GET') {
      return jsonResponse({ builtin: [BUILTIN, BUILTIN_METRIC], custom })
    }
    if (url.includes('/rules/builtin/') && method === 'PUT') {
      const id = url.split('/').pop()!
      const base = id === BUILTIN.id ? BUILTIN : BUILTIN_METRIC
      return jsonResponse({ ...base, ...body, thresholds: { ...base.thresholds, ...(body.thresholds ?? {}) } })
    }
    if (url.endsWith('/rules/custom/validate') && method === 'POST') {
      const expr = body.expression as string
      if (expr.includes('__class__') || expr.includes('(')) {
        return jsonResponse({ ok: false, error: "'Call' is not allowed here", fields: ['cpu_pct', 'syscall_rate'] })
      }
      return jsonResponse({ ok: true, error: null, fields: ['cpu_pct', 'syscall_rate'] })
    }
    if (url.endsWith('/rules/custom') && method === 'POST') {
      const created = { ...body, id: 'new-id', created_at: 1 }
      custom = [...custom, created]
      return jsonResponse(created)
    }
    if (url.includes('/rules/custom/') && method === 'PUT') {
      const id = url.split('/').pop()!
      const existing = custom.find((c) => c.id === id)!
      const updated = { ...existing, ...body }
      custom = custom.map((c) => (c.id === id ? updated : c))
      return jsonResponse(updated)
    }
    if (url.includes('/rules/custom/') && method === 'DELETE') {
      const id = url.split('/').pop()!
      custom = custom.filter((c) => c.id !== id)
      return jsonResponse({ deleted: true })
    }
    return jsonResponse({})
  }) as unknown as typeof fetch
  return calls
}

beforeEach(() => {
  installFetchMock()
})

describe('RulesSettings', () => {
  it('loads and groups built-in rules by signal, and lists custom rules', async () => {
    render(<RulesSettings backendUrl="http://x" />)
    await waitFor(() => expect(screen.getByText('Failed file opens')).toBeTruthy())
    expect(screen.getByText('Cpu bound metric')).toBeTruthy()
    expect(screen.getByText('Existing rule')).toBeTruthy()
    expect(screen.getByText('cpu_pct > 90')).toBeTruthy()
  })

  it('toggling a built-in rule PUTs enabled=false and flips the button', async () => {
    const calls = installFetchMock()
    render(<RulesSettings backendUrl="http://x" />)
    await waitFor(() => expect(screen.getByText('Failed file opens')).toBeTruthy())

    const row = screen.getByText('Failed file opens').closest<HTMLElement>('.rule-row')!
    fireEvent.click(within(row).getByText(/On — toggle/))

    await waitFor(() => expect(within(row).getByText(/Off — toggle/)).toBeTruthy())
    const put = calls.find((c) => c.method === 'PUT' && c.url.includes('failed_file_opens'))
    expect(put?.body).toEqual({ enabled: false })
  })

  it('editing a threshold saves on blur', async () => {
    const calls = installFetchMock()
    render(<RulesSettings backendUrl="http://x" />)
    await waitFor(() => expect(screen.getByText('Failed file opens')).toBeTruthy())

    const input = screen.getByTitle('failed_open_min') // label wraps the input; title lives on the <label>
    const numberInput = within(input).getByRole('spinbutton')
    fireEvent.change(numberInput, { target: { value: '9' } })
    fireEvent.blur(numberInput)

    await waitFor(() => {
      const put = calls.find((c) => c.method === 'PUT' && c.url.includes('failed_file_opens'))
      expect(put?.body).toEqual({ thresholds: { failed_open_min: 9 } })
    })
  })

  it('new custom rule form blocks Create until the expression validates', async () => {
    render(<RulesSettings backendUrl="http://x" />)
    await waitFor(() => expect(screen.getByText('Existing rule')).toBeTruthy())

    fireEvent.click(screen.getByText('+ new rule'))
    fireEvent.change(screen.getByPlaceholderText('e.g. Slow downstream retries'), { target: { value: 'My rule' } })

    const expr = screen.getByPlaceholderText('cpu_pct > 90 and syscall_rate < 5')
    fireEvent.change(expr, { target: { value: '().__class__' } })
    await waitFor(() => expect(screen.getByText(/not allowed here/)).toBeTruthy())
    expect(screen.getByText('Create')).toBeDisabled()

    fireEvent.change(expr, { target: { value: 'cpu_pct > 90' } })
    await waitFor(() => expect(screen.getByText('✓ valid expression')).toBeTruthy())
    expect(screen.getByText('Create')).not.toBeDisabled()
  })

  it('creating a valid custom rule POSTs and adds it to the list', async () => {
    const calls = installFetchMock({ custom: [] })
    render(<RulesSettings backendUrl="http://x" />)
    await waitFor(() => expect(screen.getByText('No custom rules yet.')).toBeTruthy())

    fireEvent.click(screen.getByText('+ new rule'))
    fireEvent.change(screen.getByPlaceholderText('e.g. Slow downstream retries'), { target: { value: 'My rule' } })
    const expr = screen.getByPlaceholderText('cpu_pct > 90 and syscall_rate < 5')
    fireEvent.change(expr, { target: { value: 'cpu_pct > 90' } })
    await waitFor(() => expect(screen.getByText('✓ valid expression')).toBeTruthy())

    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(screen.getByText('My rule')).toBeTruthy())
    const post = calls.find((c) => c.method === 'POST' && c.url.endsWith('/rules/custom'))
    expect(post?.body).toMatchObject({ name: 'My rule', expression: 'cpu_pct > 90', signal: 'metrics' })
  })

  it('deleting a custom rule removes it from the list', async () => {
    const calls = installFetchMock()
    render(<RulesSettings backendUrl="http://x" />)
    await waitFor(() => expect(screen.getByText('Existing rule')).toBeTruthy())

    fireEvent.click(screen.getByText('Existing rule')) // opens the edit form
    fireEvent.click(await screen.findByText('Delete'))

    await waitFor(() => expect(screen.queryByText('Existing rule')).toBeNull())
    expect(calls.some((c) => c.method === 'DELETE' && c.url.includes('/rules/custom/c1'))).toBe(true)
  })
})
