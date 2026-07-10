/**
 * Central client for the local backend API. Electron hands the renderer a
 * per-launch bearer token (window.opentrace.apiToken) when it spawned the
 * backend itself; it's empty for a dev/test run against an external backend
 * (Vite dev server, isolated test backend, manual `uvicorn`), which never got
 * a token either — so every helper here degrades to a plain unauthenticated
 * call in that case.
 */
const API_TOKEN =
  (typeof window !== 'undefined' && window.opentrace?.apiToken) || ''

/** Merge the bearer token into a headers object, if one is configured. */
export function authHeaders(extra?: HeadersInit): HeadersInit {
  if (!API_TOKEN) return extra ?? {}
  return { ...(extra as Record<string, string> | undefined), Authorization: `Bearer ${API_TOKEN}` }
}

/** fetch() with the bearer token attached — a drop-in replacement for every
 *  `fetch(\`${backendUrl}/...\`)` call site talking to the OpenTrace backend. */
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  return fetch(input, { ...init, headers: authHeaders(init.headers) })
}

/** EventSource can't set custom headers, so the token rides as a query param
 *  instead. Use for every `new EventSource(url)` call against the backend. */
export function sseUrl(url: string): string {
  if (!API_TOKEN) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(API_TOKEN)}`
}
