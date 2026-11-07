import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// The actions /auth/me sends for each role (the API's table in app/authz.py).
const ACTIONS: Record<string, string[]> = {
  admin: ['audit:read', 'project:read', 'standards:read', 'user:admin'],
  sponsor: [
    'gate:decide',
    'intake:decide',
    'project:read',
    'proposal:approve',
    'risk:accept',
    'standards:read',
  ],
  project_manager: [
    'project:read',
    'project:write',
    'report:publish',
    'risk:accept',
    'standards:read',
  ],
  ethics_reviewer: ['audit:read', 'gate:decide', 'project:read', 'standards:read'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function signedInAs(role: string, actions = ACTIONS[role]) {
  const user = { id: 7, email: `${role}@pmo.example`, display_name: 'Test User', role, actions }
  return vi.fn(async (path: string) => {
    if (path === '/api/v1/auth/me') return json(user)
    if (path === '/api/v1/health') return json({ status: 'ok', version: '0.1.0' })
    return json({ detail: 'not found' }, 404)
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
  return { wrapper, router }
}

function menu(wrapper: Awaited<ReturnType<typeof mountAt>>['wrapper']) {
  return wrapper
    .findAll('[data-test^="nav-"]')
    .map((item) => item.attributes('data-test')!.replace('nav-', ''))
}

describe('menu', () => {
  afterEach(() => vi.unstubAllGlobals())

  it.each([
    ['admin', ['home', 'inbox', 'board', 'projects', 'intake', 'risks', 'audit', 'standards']],
    ['sponsor', ['home', 'inbox', 'board', 'projects', 'intake', 'risks', 'standards']],
    ['project_manager', ['home', 'inbox', 'board', 'projects', 'intake', 'risks', 'standards']],
    ['ethics_reviewer', ['home', 'inbox', 'board', 'projects', 'intake', 'risks', 'audit', 'standards']],
    ['auditor', ['home', 'inbox', 'board', 'projects', 'intake', 'risks', 'audit', 'standards']],
  ])('shows %s the pages that role may use', async (role, expected) => {
    vi.stubGlobal('fetch', signedInAs(role))
    const { wrapper } = await mountAt('/')
    expect(menu(wrapper)).toEqual(expected)
  })

  it('follows the actions the API sends, not the role name', async () => {
    vi.stubGlobal('fetch', signedInAs('auditor', ['standards:read']))
    const { wrapper } = await mountAt('/')
    expect(menu(wrapper)).toEqual(['home', 'standards'])
  })

  it('is hidden when nobody is signed in', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'not signed in' }, 401)))
    const { wrapper } = await mountAt('/')
    expect(wrapper.find('[data-test="nav"]').exists()).toBe(false)
  })

  it('opens a placeholder page', async () => {
    vi.stubGlobal('fetch', signedInAs('auditor'))
    const { wrapper, router } = await mountAt('/audit')
    expect(router.currentRoute.value.name).toBe('audit')
    expect(wrapper.get('[data-test="page-title"]').text()).toBe('Audit')
  })

  it('sends a role away from a page it cannot use', async () => {
    vi.stubGlobal('fetch', signedInAs('sponsor'))
    const { router } = await mountAt('/audit')
    expect(router.currentRoute.value.name).toBe('home')
  })
})
