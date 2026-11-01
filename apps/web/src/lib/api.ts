// Fetch wrapper for the API. It sends the session cookie, adds the CSRF header on unsafe
// methods, and hands a 401 to the registered handler (the router sends the user to login).

export const CSRF_COOKIE = 'pmo_csrf'
export const CSRF_HEADER = 'X-CSRF-Token'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler
}

export function readCookie(name: string): string | null {
  for (const part of document.cookie.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return decodeURIComponent(rest.join('='))
  }
  return null
}

export interface ApiOptions extends RequestInit {
  // False when a 401 is an expected answer (login, the first "who am I" check).
  handle401?: boolean
}

export async function apiFetch(path: string, options: ApiOptions = {}): Promise<Response> {
  const { handle401 = true, ...init } = options
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (!SAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) headers.set(CSRF_HEADER, csrf)
  }
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, { ...init, method, headers, credentials: 'same-origin' })
  if (response.status === 401 && handle401 && onUnauthorized) onUnauthorized()
  return response
}
