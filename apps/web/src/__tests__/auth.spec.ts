import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import { apiFetch, setUnauthorizedHandler } from '../lib/api'
import { safeRedirect } from '../lib/redirect'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'
import { useAuthStore } from '../stores/auth'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const USER = {
  id: 1,
  email: 'ada.reviewer@pmo.example',
  display_name: 'Ada Reviewer',
  role: 'project_manager',
  actions: ['project:read', 'project:write', 'report:publish', 'risk:accept', 'standards:read'],
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

// A fake API: `signedIn` says whether /auth/me answers with a user.
function fakeApi(state: { signedIn: boolean }) {
  return vi.fn(async (path: string, init: RequestInit = {}) => {
    if (path === '/api/v1/auth/me') {
      return state.signedIn ? json(USER) : json({ detail: 'not signed in' }, 401)
    }
    if (path === '/api/v1/auth/login') {
      state.signedIn = true
      return json(USER)
    }
    if (path === '/api/v1/auth/logout') {
      state.signedIn = false
      return new Response(null, { status: 204 })
    }
    if (path === '/api/v1/health') return json({ status: 'ok', version: '0.1.0' })
    return json({ detail: `unexpected ${init.method ?? 'GET'} ${path}` }, 404)
  })
}

async function mountAt(path: string) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter(true)
  router.push(path)
  await router.isReady()
  const wrapper = mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [pinia, router, makeVuetify()] },
  })
  await flushPromises()
  return { wrapper, router, auth: useAuthStore() }
}

function clearCookies() {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0].trim()
    if (name) document.cookie = `${name}=; max-age=0; path=/`
  }
}

describe('router guard', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('sends a signed-out visitor to login, remembering where they were going', async () => {
    vi.stubGlobal('fetch', fakeApi({ signedIn: false }))
    const { router, wrapper } = await mountAt('/')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/')
    expect(wrapper.find('[data-test="logout"]').exists()).toBe(false)
    // A first visit is not an expired session.
    expect(wrapper.find('[data-test="session-expired"]').exists()).toBe(false)
  })

  it('lets a signed-in user through', async () => {
    vi.stubGlobal('fetch', fakeApi({ signedIn: true }))
    const { router, wrapper } = await mountAt('/')
    expect(router.currentRoute.value.name).toBe('home')
    expect(wrapper.get('[data-test="user-name"]').text()).toBe('Ada Reviewer')
  })

  it('sends a signed-in user away from the login page', async () => {
    vi.stubGlobal('fetch', fakeApi({ signedIn: true }))
    const { router } = await mountAt('/login')
    expect(router.currentRoute.value.name).toBe('home')
  })

  it('only redirects to same-site paths after login', () => {
    expect(safeRedirect('/projects?x=1')).toBe('/projects?x=1')
    expect(safeRedirect('//evil.example')).toBe('/')
    expect(safeRedirect('/\\evil.example')).toBe('/')
    expect(safeRedirect('https://evil.example')).toBe('/')
    expect(safeRedirect(undefined)).toBe('/')
  })
})

describe('login page', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('signs in and returns to the page asked for', async () => {
    const fetchMock = fakeApi({ signedIn: false })
    vi.stubGlobal('fetch', fetchMock)
    const { router, wrapper, auth } = await mountAt('/')
    await wrapper.get('[data-test="email"] input').setValue('ada.reviewer@pmo.example')
    await wrapper.get('[data-test="password"] input').setValue('typed-in-test')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(auth.user).toEqual(USER)
    expect(router.currentRoute.value.name).toBe('home')
    const [, init] = fetchMock.mock.calls.find(([path]) => path === '/api/v1/auth/login')!
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      email: 'ada.reviewer@pmo.example',
      password: 'typed-in-test',
    })
  })

  it('shows the generic message when sign-in fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ detail: 'invalid email or password' }, 401)),
    )
    const { router, wrapper } = await mountAt('/login')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
    expect(wrapper.get('[data-test="login-error"]').text()).toBe('Invalid email or password.')
    // A failed sign-in is not an expired session.
    expect(wrapper.find('[data-test="session-expired"]').exists()).toBe(false)
  })
})

describe('logout', () => {
  beforeEach(() => {
    document.cookie = 'pmo_csrf=csrf-from-cookie; path=/'
  })
  afterEach(() => {
    clearCookies()
    vi.unstubAllGlobals()
  })

  it('signs out with the CSRF header and returns to login', async () => {
    const fetchMock = fakeApi({ signedIn: true })
    vi.stubGlobal('fetch', fetchMock)
    const { router, wrapper, auth } = await mountAt('/')
    await wrapper.get('[data-test="logout"]').trigger('click')
    await flushPromises()
    expect(auth.user).toBeNull()
    expect(router.currentRoute.value.name).toBe('login')
    expect(wrapper.find('[data-test="logout"]').exists()).toBe(false)
    const [, init] = fetchMock.mock.calls.find(([path]) => path === '/api/v1/auth/logout')!
    expect(init?.method).toBe('POST')
    expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('csrf-from-cookie')
  })

  it('still signs out locally if the API cannot be reached', async () => {
    const state = { signedIn: true }
    const api = fakeApi(state)
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string, init?: RequestInit) => {
        if (path === '/api/v1/auth/logout') throw new TypeError('network down')
        return api(path, init)
      }),
    )
    const { router, wrapper, auth } = await mountAt('/')
    await wrapper.get('[data-test="logout"]').trigger('click')
    await flushPromises()
    expect(auth.user).toBeNull()
    expect(router.currentRoute.value.name).toBe('login')
  })
})

describe('session expiry', () => {
  afterEach(() => {
    clearCookies()
    setUnauthorizedHandler(null)
    vi.unstubAllGlobals()
  })

  it('sends the user to login with a note when an API call returns 401', async () => {
    const state = { signedIn: true }
    vi.stubGlobal('fetch', fakeApi(state))
    const { router, wrapper, auth } = await mountAt('/')
    expect(router.currentRoute.value.name).toBe('home')

    state.signedIn = false
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'not signed in' }, 401)))
    await apiFetch('/api/v1/projects')
    await flushPromises()

    expect(auth.user).toBeNull()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query).toMatchObject({ redirect: '/', expired: '1' })
    expect(wrapper.get('[data-test="session-expired"]').text()).toContain('session expired')
  })
})

describe('apiFetch', () => {
  afterEach(() => {
    clearCookies()
    vi.unstubAllGlobals()
  })

  it('adds the CSRF header to unsafe methods only', async () => {
    document.cookie = 'pmo_csrf=abc123; path=/'
    const fetchMock = vi.fn(async () => json({}))
    vi.stubGlobal('fetch', fetchMock)
    await apiFetch('/api/v1/thing')
    await apiFetch('/api/v1/thing', { method: 'delete' })
    const headers = fetchMock.mock.calls.map(
      (call) => new Headers((call as unknown as [string, RequestInit])[1].headers),
    )
    expect(headers[0].has('X-CSRF-Token')).toBe(false)
    expect(headers[1].get('X-CSRF-Token')).toBe('abc123')
  })
})
