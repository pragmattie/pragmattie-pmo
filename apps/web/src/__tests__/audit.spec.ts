import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  auditLabel,
  type AuditRow,
  auditQuery,
  facetItems,
  NO_AUDIT_FILTERS,
  recorded,
  whoText,
} from '../lib/audit'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

// The audit log: read-only, for roles with audit:read, newest first, paged and filtered by the
// API, with a drawer showing everything a row recorded.

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('visualViewport', new EventTarget())

const ACTIONS: Record<string, string[]> = {
  admin: ['audit:read', 'project:read', 'standards:read', 'user:admin'],
  sponsor: ['gate:decide', 'intake:decide', 'project:read', 'proposal:approve', 'risk:accept'],
  project_manager: ['project:read', 'project:write', 'report:publish', 'risk:accept'],
  ethics_reviewer: ['audit:read', 'gate:decide', 'project:read', 'standards:read'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

function row(id: number, changes: Partial<AuditRow> = {}): AuditRow {
  return {
    id,
    agent: 'risk_agent',
    trigger: 'agent',
    subject_type: 'risk',
    subject_id: '6',
    version: 1,
    attempt: 1,
    status: 'ok',
    tier: null,
    signals: { project: 3 },
    output: { probability: 2, impact: 3 },
    action_taken: { action: 'suggest' },
    human_override: null,
    model: 'a-model',
    prompt_version: 'v2',
    input_tokens: 1200,
    output_tokens: 300,
    cost_usd: '0.02',
    error: null,
    supersedes_id: null,
    actor_user_id: null,
    actor_name: null,
    created_at: '2026-11-07T09:00:00',
    ...changes,
  }
}

const ROWS = [
  row(12, {
    agent: 'risk',
    trigger: 'human',
    actor_user_id: 11,
    actor_name: 'Pat Manager',
    model: null,
    human_override: { reason: 'scored it myself' },
  }),
  row(11),
]

// What the log holds; a deactivated person (Dee Former) still appears.
const FACETS = {
  agents: ['auth', 'change', 'risk_agent'],
  subject_types: ['change_request', 'login', 'risk'],
  statuses: ['ok', 'rejected'],
  people: [
    { id: 14, display_name: 'Dee Former' },
    { id: 11, display_name: 'Pat Manager' },
  ],
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeApi(role: string) {
  const user = { id: 7, email: `${role}@pmo.example`, display_name: 'Test User', role }
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input, 'http://localhost')
    const method = init.method ?? 'GET'
    if (url.pathname === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS[role] })
    if (url.pathname === '/api/v1/inbox/count') return json({ count: 0 })
    if (url.pathname === '/api/v1/health') return json({ status: 'ok', version: '0.1.0' })
    if (url.pathname === '/api/v1/audit/facets') {
      if (!ACTIONS[role].includes('audit:read')) return json({ detail: 'forbidden' }, 403)
      return json(FACETS)
    }
    if (url.pathname === '/api/v1/audit' && method === 'GET') {
      if (!ACTIONS[role].includes('audit:read')) return json({ detail: 'forbidden' }, 403)
      return json({ items: ROWS, total: 60, limit: 25, offset: 0 })
    }
    return json({ detail: `unexpected ${method} ${url.pathname}` }, 404)
  })
}

async function mountAt(path: string, role: string) {
  const fetchMock = fakeApi(role)
  vi.stubGlobal('fetch', fetchMock)
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter(true)
  router.push(path)
  await router.isReady()
  const wrapper = mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { wrapper, router, fetchMock }
}

type Wrapper = Awaited<ReturnType<typeof mountAt>>['wrapper']

function auditCalls(fetchMock: ReturnType<typeof fakeApi>) {
  return fetchMock.mock.calls
    .map(([input, init]) => ({ url: new URL(String(input), 'http://localhost'), init }))
    .filter(({ url }) => url.pathname === '/api/v1/audit')
}

function select(wrapper: Wrapper, test: string) {
  const found = wrapper
    .findAllComponents({ name: 'VSelect' })
    .find((c) => c.attributes('data-test') === test)
  expect(found, test).toBeTruthy()
  return found!
}

function setSelect(wrapper: Wrapper, test: string, value: unknown) {
  const select = wrapper
    .findAllComponents({ name: 'VSelect' })
    .find((c) => c.attributes('data-test') === test)
  expect(select, test).toBeTruthy()
  select!.vm.$emit('update:modelValue', value)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
  document.body.innerHTML = ''
})

describe('the Audit page', () => {
  it.each(['auditor', 'admin', 'ethics_reviewer'])('opens for the %s', async (role) => {
    const { wrapper, router } = await mountAt('/audit', role)
    expect(router.currentRoute.value.name).toBe('audit')
    expect(wrapper.get('[data-test="page-title"]').text()).toBe('Audit')
    expect(wrapper.find('[data-test="nav-audit"]').exists()).toBe(true)
  })

  it.each(['sponsor', 'project_manager'])(
    'is not in the menu for the %s, whose address leads home',
    async (role) => {
      const { wrapper, router, fetchMock } = await mountAt('/audit', role)
      expect(wrapper.find('[data-test="nav-audit"]').exists()).toBe(false)
      expect(router.currentRoute.value.name).toBe('home')
      expect(auditCalls(fetchMock)).toHaveLength(0)
    },
  )

  it('lists rows newest first as the API sends them, naming people and agents', async () => {
    const { wrapper, fetchMock } = await mountAt('/audit', 'auditor')
    const [first] = auditCalls(fetchMock)
    // Sign-ins are hidden from the start.
    expect(Object.fromEntries(first.url.searchParams)).toEqual({
      hide_sign_ins: 'true',
      limit: '25',
      offset: '0',
    })
    const text = wrapper.get('[data-test="audit-table"]').text()
    expect(text.indexOf('Pat Manager')).toBeLessThan(text.indexOf('risk_agent (agent)'))
    expect(text).toContain('Risk 6')
  })

  it('fills the agent, subject type and status lists from the facets route, in plain words', async () => {
    const { wrapper, fetchMock } = await mountAt('/audit', 'auditor')
    const paths = fetchMock.mock.calls.map(([input]) => new URL(String(input), 'http://localhost').pathname)
    expect(paths).toContain('/api/v1/audit/facets')
    expect(paths).not.toContain('/api/v1/users')
    const items = (test: string) => select(wrapper, test).props('items')
    expect(items('filter-agent')).toEqual([
      { title: 'Sign-in and accounts', value: 'auth' },
      { title: 'Change', value: 'change' },
      { title: 'Risk agent', value: 'risk_agent' },
    ])
    expect(items('filter-subject-type')).toEqual([
      { title: 'Change request', value: 'change_request' },
      { title: 'Login', value: 'login' },
      { title: 'Risk', value: 'risk' },
    ])
    expect(items('filter-status')).toEqual([
      { title: 'OK', value: 'ok' },
      { title: 'Rejected', value: 'rejected' },
    ])
    // Every list starts at Any.
    for (const test of ['filter-agent', 'filter-subject-type', 'filter-status', 'filter-person']) {
      expect(select(wrapper, test).props('modelValue')).toBeNull()
      expect(select(wrapper, test).props('placeholder')).toBe('Any')
    }
  })

  it('lists every person in the log for the person filter, deactivated people included', async () => {
    const { wrapper } = await mountAt('/audit', 'auditor')
    expect(select(wrapper, 'filter-person').props('items')).toEqual(FACETS.people)
  })

  it('filters by agent, subject type, status and person on the server', async () => {
    const { wrapper, fetchMock } = await mountAt('/audit', 'auditor')
    setSelect(wrapper, 'filter-agent', 'change')
    setSelect(wrapper, 'filter-subject-type', 'change_request')
    setSelect(wrapper, 'filter-status', 'rejected')
    setSelect(wrapper, 'filter-person', 14)
    await flushPromises()
    const last = Object.fromEntries(auditCalls(fetchMock).at(-1)!.url.searchParams)
    expect(last).toEqual({
      agent: 'change',
      subject_type: 'change_request',
      status: 'rejected',
      actor_user_id: '14',
      hide_sign_ins: 'true',
      limit: '25',
      offset: '0',
    })
  })

  it('shows sign-ins once Hide sign-ins is switched off, and Clear hides them again', async () => {
    const { wrapper, fetchMock } = await mountAt('/audit', 'auditor')
    const toggle = wrapper.get('[data-test="filter-hide-sign-ins"] input')
    expect((toggle.element as HTMLInputElement).checked).toBe(true)
    await toggle.setValue(false)
    await flushPromises()
    expect(auditCalls(fetchMock).at(-1)!.url.searchParams.has('hide_sign_ins')).toBe(false)
    await wrapper.get('[data-test="filter-clear"]').trigger('click')
    await flushPromises()
    expect(auditCalls(fetchMock).at(-1)!.url.searchParams.get('hide_sign_ins')).toBe('true')
  })

  it('opens a drawer with everything the row recorded', async () => {
    const { wrapper } = await mountAt('/audit', 'auditor')
    await wrapper.get('[data-test="audit-row-11"]').trigger('click')
    await flushPromises()
    const field = (name: string) =>
      document.querySelector(`[data-test="audit-field-${name}"]`)?.textContent
    expect(field('Who')).toBe('risk_agent (agent)')
    expect(field('Agent')).toBe('Risk agent')
    expect(field('Status')).toBe('OK')
    expect(field('Inputs')).toContain('"project": 3')
    expect(field('Output')).toContain('"impact": 3')
    expect(field('Action taken')).toContain('suggest')
    expect(field('Override')).toBe('—')
    expect(field('Model')).toBe('a-model')
    expect(field('Input tokens')).toBe('1200')
    expect(field('Output tokens')).toBe('300')
    expect(field('Cost (USD)')).toBe('0.02')
    expect(field('Error')).toBe('—')
    expect(field('When')).toBeTruthy()
  })

  it('never sends anything but a read', async () => {
    const { wrapper, fetchMock } = await mountAt('/audit', 'auditor')
    await wrapper.get('[data-test="audit-row-12"]').trigger('click')
    await flushPromises()
    const writes = fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') !== 'GET')
    expect(writes).toHaveLength(0)
    expect(document.querySelector('[data-test="audit-detail"]')?.textContent).toContain(
      'scored it myself',
    )
  })
})

describe('the audit helpers', () => {
  it('builds the query with only the filters set', () => {
    expect(auditQuery(NO_AUDIT_FILTERS, 2, 25)).toBe('hide_sign_ins=true&limit=25&offset=25')
    expect(
      auditQuery(
        {
          ...NO_AUDIT_FILTERS,
          agent: '  risk ',
          subject_type: '',
          actor_user_id: 4,
          hide_sign_ins: false,
        },
        1,
        10,
      ),
    ).toBe('agent=risk&actor_user_id=4&limit=10&offset=0')
  })

  it('names agents, subject types and statuses in plain words', () => {
    expect(auditLabel('change_request')).toBe('Change request')
    expect(auditLabel('auth')).toBe('Sign-in and accounts')
    expect(auditLabel('ok')).toBe('OK')
    expect(facetItems(['risk_agent'])).toEqual([{ title: 'Risk agent', value: 'risk_agent' }])
  })

  it('names the person, or the agent', () => {
    expect(whoText(row(1))).toBe('risk_agent (agent)')
    expect(whoText(row(1, { actor_user_id: 3, actor_name: 'Ada' }))).toBe('Ada')
    expect(whoText(row(1, { actor_user_id: 3 }))).toBe('Person 3')
  })

  it('shows a recorded value as text', () => {
    expect(recorded(null)).toBe('—')
    expect(recorded('plain')).toBe('plain')
    expect(recorded({ a: 1 })).toBe('{\n  "a": 1\n}')
  })
})
