import { useCallback, useEffect, useState } from 'react'

/**
 * Mirror of `backend/app/sessions.Session`. Keep in sync if fields change —
 * pydantic is the source of truth.
 */
export interface Session {
  id: string
  process_name: string
  command: string
  cwd: string
  started_at: number
  ended_at: number | null
  duration_ms: number | null
  exit_code: number | null
  exit_signal: string | null
  label: string | null
  tags: string[]
  created_at: number
}

export interface SessionCreate {
  command: string
  cwd: string
  process_name?: string
  label?: string
  tags?: string[]
}

export interface SessionUpdate {
  ended_at?: number
  exit_code?: number
  exit_signal?: string
  label?: string
  tags?: string[]
}

interface Hook {
  sessions: Session[]
  loading: boolean
  error: string | null
  refresh: () => Promise<void>
  create: (data: SessionCreate) => Promise<Session | null>
  update: (id: string, data: SessionUpdate) => Promise<Session | null>
}

export function useSessions(backendUrl: string): Hook {
  const [sessions, setSessions] = useState<Session[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${backendUrl}/sessions`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const items: Session[] = await r.json()
      setSessions(items)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [backendUrl])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const create = useCallback(
    async (data: SessionCreate): Promise<Session | null> => {
      try {
        const r = await fetch(`${backendUrl}/sessions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        })
        if (!r.ok) return null
        const sess: Session = await r.json()
        setSessions((prev) => [sess, ...prev.filter((s) => s.id !== sess.id)])
        return sess
      } catch {
        return null
      }
    },
    [backendUrl],
  )

  const update = useCallback(
    async (id: string, data: SessionUpdate): Promise<Session | null> => {
      try {
        const r = await fetch(`${backendUrl}/sessions/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        })
        if (!r.ok) return null
        const sess: Session = await r.json()
        setSessions((prev) => prev.map((s) => (s.id === sess.id ? sess : s)))
        return sess
      } catch {
        return null
      }
    },
    [backendUrl],
  )

  return { sessions, loading, error, refresh, create, update }
}
