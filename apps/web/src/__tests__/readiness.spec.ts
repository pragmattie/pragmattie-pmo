import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import ReadinessSection from '../components/ReadinessSection.vue'
import {
  canEditReadiness,
  isOverdue,
  localDate,
  type ProjectRequest,
  ratingColor,
  ratingText,
  type Readiness,
  type RequestStatus,
  type ValueCase,
} from '../lib/intake'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('visualViewport', new EventTarget())

// The actions /auth/me sends for each role (the API's table in app/authz.py).
const ACTIONS: Record<string, string[]> = {
  admin: ['audit:read', 'project:read', 'standards:read', 'user:admin'],
  sponsor: ['gate:decide', 'intake:decide', 'project:read', 'proposal:approve', 'risk:accept'],
  project_manager: ['project:read', 'project:write', 'report:publish', 'risk:accept'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

const MISSING = ['a readiness check']
const MISSING_ALL = [
  'a rating and a note for each readiness area: problem, approach and data',
  'an action with an owner and a due date for each yellow readiness area',
  "a person's confirmation of the readiness check an agent drafted",
]

function request(changes: Partial<ProjectRequest> = {}): ProjectRequest {
  return {
    id: 1,
    title: 'Request 1',
    client_id: null,
    client_name: null,
    asked_for: 'A forecast',
    problem: 'Stock-outs',
    outcome: 'Fewer stock-outs',
    budget_low: null,
    budget_high: null,
    wanted_by: null,
    uses_ai: true,
    known_risks: null,
    requested_by: 11,
    requested_by_name: 'Pat Manager',
    status: 'draft',
    submitted_at: null,
    version: 3,
    created_at: '2026-10-01T09:00:00',
    updated_at: '2026-10-01T09:00:00',
    ...changes,
  }
}

function readiness(changes: Partial<Readiness> = {}): Readiness {
  return {
    request_id: 1,
    request_version: 3,
    problem: { rating: 'green', note: 'Stock-outs cost sales', actions: [] },
    approach: {
      rating: 'yellow',
      note: 'A reorder rule may do as well',
      actions: [
        {
          description: 'Compare with a reorder rule',
          owner_id: 11,
          owner_name: 'Pat Manager',
          due_date: '2099-12-01',
        },
      ],
    },
    data: {
      rating: 'red',
      note: 'History is thin',
      actions: [
        {
          description: 'Find older sales data',
          owner_id: 12,
          owner_name: 'Sam Analyst',
          due_date: '2020-01-15',
        },
      ],
    },
    overall: 'red',
    source: 'person',
    confirmed_by: 11,
    confirmed_by_name: 'Pat Manager',
    confirmed_at: '2026-10-02T09:00:00',
    created_at: '2026-10-02T09:00:00',
    updated_at: '2026-10-02T09:00:00',
    ...changes,
  }
}

const VALUE_CASE: Partial<ValueCase> = {
  request_id: 1,
  request_version: 3,
  benefits: [],
  cost_low: null,
  cost_high: null,
  cost_method: null,
  strategic_fit: 'Client goal',
  cost_of_not_doing: null,
  source: 'person',
  confirmed_by: 11,
  confirmed_by_name: 'Pat Manager',
  confirmed_at: '2026-10-02T09:00:00',
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface FakeOptions {
  stored?: ProjectRequest
  readiness?: Readiness | null
  submitMissing?: string[]
}

function fakeApi(role: string, options: FakeOptions = {}) {
  const user = { id: 7, email: `${role}@pmo.example`, display_name: 'Test User', role }
  let stored = options.stored ?? request()
  let current = options.readiness === undefined ? readiness() : options.readiness
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init.method ?? 'GET'
    if (path === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS[role] })
    if (path === '/api/v1/clients') return json({ items: [], total: 0, limit: 100, offset: 0 })
    if (path === '/api/v1/users') {
      const items = [
        { id: 11, display_name: 'Pat Manager' },
        { id: 12, display_name: 'Sam Analyst' },
      ]
      return json({ items, total: 2, limit: 100, offset: 0 })
    }
    if (path === '/api/v1/requests/1/history') {
      return json({ items: [], total: 0, limit: 50, offset: 0 })
    }
    if (path === '/api/v1/requests/1/value-case') return json(VALUE_CASE)
    if (path === '/api/v1/requests/1/readiness' && method === 'PUT') {
      const { version } = JSON.parse(String(init.body))
      stored = { ...stored, version: version + 1 }
      current = readiness({ request_version: version + 1, confirmed_by: 7 })
      return json(current)
    }
    if (path === '/api/v1/requests/1/readiness') {
      return current ? json(current) : json({ detail: 'no readiness check yet' }, 404)
    }
    if (path === '/api/v1/requests/1/submit' && method === 'POST') {
      if (options.submitMissing) {
        const message = "this request can't be submitted until it is complete and confirmed"
        return json({ detail: { message, missing: options.submitMissing } }, 409)
      }
      stored = { ...stored, status: 'submitted', version: stored.version + 1 }
      return json(stored)
    }
    if (path === '/api/v1/requests/1') return json(stored)
    return json({ detail: `unexpected ${method} ${path}` }, 404)
  })
}

async function mountAt(role: string, options: FakeOptions = {}) {
  const fetchMock = fakeApi(role, options)
  vi.stubGlobal('fetch', fetchMock)
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter(true)
  router.push('/intake/1')
  await router.isReady()
  const wrapper = mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { wrapper, fetchMock }
}

type Mounted = Awaited<ReturnType<typeof mountAt>>

function calls(fetchMock: Mounted['fetchMock'], method: string, path: string) {
  return fetchMock.mock.calls.filter(
    ([input, init]) =>
      (init?.method ?? 'GET') === method &&
      new URL(String(input), 'http://localhost').pathname === path,
  )
}

async function settle() {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve))
  await flushPromises()
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
  document.body.innerHTML = ''
})

describe('readiness helpers', () => {
  it.each<[RequestStatus, boolean]>([
    ['draft', true],
    ['submitted', false],
    ['approved', false],
    ['declined', false],
    ['withdrawn', false],
  ])('a %s request has an editable readiness check: %s', (status, expected) => {
    expect(canEditReadiness({ status }, true)).toBe(expected)
    expect(canEditReadiness({ status }, false)).toBe(false)
  })

  it('names and colours each rating', () => {
    expect([ratingColor('green'), ratingColor('yellow'), ratingColor('red')]).toEqual([
      'success',
      'warning',
      'error',
    ])
    expect(ratingColor('unrated')).toBe('grey')
    expect(ratingColor(null)).toBe('grey')
    expect(ratingText('yellow')).toBe('Yellow')
    expect(ratingText('unrated')).toBe('Unrated')
    expect(ratingText(null)).toBe('Unrated')
  })

  it('flags an action as overdue only once its due date has passed', () => {
    const now = new Date(2026, 10, 3, 15, 0)
    expect(localDate(now)).toBe('2026-11-03')
    expect(isOverdue({ due_date: '2026-11-02' }, now)).toBe(true)
    expect(isOverdue({ due_date: '2026-11-03' }, now)).toBe(false)
    expect(isOverdue({ due_date: '2026-11-04' }, now)).toBe(false)
  })
})

describe('the readiness section', () => {
  it('shows each area as a coloured chip with its note and actions', async () => {
    const { wrapper } = await mountAt('sponsor')
    const chip = (area: string) => wrapper.get(`[data-test="readiness-${area}"] [data-test="rating-chip"]`)
    expect(chip('problem').text()).toBe('Green')
    expect(chip('problem').classes()).toContain('bg-success')
    expect(chip('approach').text()).toBe('Yellow')
    expect(chip('approach').classes()).toContain('bg-warning')
    expect(chip('data').text()).toBe('Red')
    expect(chip('data').classes()).toContain('bg-error')
    expect(wrapper.get('[data-test="readiness-overall"]').text()).toBe('Overall: Red')
    expect(wrapper.get('[data-test="readiness-data"] [data-test="rating-note"]').text()).toBe(
      'History is thin',
    )
    const action = wrapper.get('[data-test="action-approach-0"]').text()
    expect(action).toContain('Compare with a reorder rule')
    expect(action).toContain('owner Pat Manager')
    expect(action).toContain('2099-12-01')
    expect(wrapper.get('[data-test="readiness-confirmed"]').text()).toContain('Pat Manager')
  })

  it('marks an action whose due date has passed as overdue', async () => {
    const { wrapper } = await mountAt('auditor')
    expect(wrapper.get('[data-test="action-data-0"] [data-test="overdue"]').text()).toBe('Overdue')
    expect(wrapper.find('[data-test="action-approach-0"] [data-test="overdue"]').exists()).toBe(
      false,
    )
  })

  it('shows an unconfirmed agent draft as unrated overall', async () => {
    const draft = readiness({ overall: 'unrated', source: 'agent', confirmed_by: null })
    const { wrapper } = await mountAt('sponsor', { readiness: { ...draft, confirmed_by_name: null } })
    expect(wrapper.get('[data-test="readiness-overall"]').text()).toBe('Overall: Unrated')
    expect(wrapper.get('[data-test="readiness-confirmed"]').text()).toContain(
      'a person must confirm it',
    )
  })

  it('says when there is no readiness check yet', async () => {
    const { wrapper } = await mountAt('auditor', { readiness: null })
    expect(wrapper.get('[data-test="readiness-none"]').text()).toContain('No readiness check yet')
  })

  it('is not shown, or fetched, for a request that does not use AI', async () => {
    const { wrapper, fetchMock } = await mountAt('project_manager', {
      stored: request({ uses_ai: false }),
    })
    expect(wrapper.find('[data-test="value-case"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="readiness"]').exists()).toBe(false)
    expect(calls(fetchMock, 'GET', '/api/v1/requests/1/readiness')).toHaveLength(0)
  })

  it.each<[string, RequestStatus, boolean]>([
    ['project_manager', 'draft', true],
    ['project_manager', 'submitted', false],
    ['project_manager', 'withdrawn', false],
    ['sponsor', 'draft', false],
    ['auditor', 'draft', false],
    ['admin', 'draft', false],
  ])('%s on a %s request sees the edit button: %s', async (role, status, shown) => {
    const { wrapper } = await mountAt(role, { stored: request({ status }) })
    expect(wrapper.find('[data-test="readiness-edit"]').exists()).toBe(shown)
  })

  it('saves the edited check with the request version it read', async () => {
    const { wrapper, fetchMock } = await mountAt('project_manager')
    await wrapper.get('[data-test="readiness-edit"]').trigger('click')
    await settle()
    const notes = wrapper.findAll('[data-test="field-note"] textarea')
    expect(notes).toHaveLength(3)
    await notes[0].setValue('  Stock-outs cost sales every quarter  ')
    await wrapper.get('[data-test="readiness-save"]').trigger('click')
    await settle()

    const puts = calls(fetchMock, 'PUT', '/api/v1/requests/1/readiness')
    expect(puts).toHaveLength(1)
    const stored = readiness()
    expect(JSON.parse(String(puts[0][1]!.body))).toEqual({
      version: 3,
      problem: { rating: 'green', note: 'Stock-outs cost sales every quarter', actions: [] },
      approach: {
        rating: 'yellow',
        note: stored.approach.note,
        actions: [
          { description: 'Compare with a reorder rule', owner_id: 11, due_date: '2099-12-01' },
        ],
      },
      data: {
        rating: 'red',
        note: stored.data.note,
        actions: [{ description: 'Find older sales data', owner_id: 12, due_date: '2020-01-15' }],
      },
    })
    expect(wrapper.find('[data-test="readiness-form"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="message"]').text()).toContain('Readiness check saved.')
  })

  it('refuses to send a yellow area with no action', async () => {
    const { wrapper, fetchMock } = await mountAt('project_manager')
    await wrapper.get('[data-test="readiness-edit"]').trigger('click')
    await settle()
    await wrapper.get('[data-test="remove-action-approach-0"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="readiness-form-approach"] [data-test="needs-action"]').text())
      .toContain('A yellow rating needs at least one action')
    await wrapper.get('[data-test="readiness-save"]').trigger('click')
    await settle()
    expect(calls(fetchMock, 'PUT', '/api/v1/requests/1/readiness')).toHaveLength(0)
    expect(wrapper.find('[data-test="readiness-form"]').exists()).toBe(true)
  })

  it('refuses to send an action without a description', async () => {
    const { wrapper, fetchMock } = await mountAt('project_manager')
    await wrapper.get('[data-test="readiness-edit"]').trigger('click')
    await settle()
    await wrapper.get('[data-test="add-action-problem"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="action-form-problem-0"]').exists()).toBe(true)
    await wrapper.get('[data-test="readiness-save"]').trigger('click')
    await settle()
    expect(calls(fetchMock, 'PUT', '/api/v1/requests/1/readiness')).toHaveLength(0)
    expect(wrapper.get('[data-test="action-form-problem-0"]').text()).toContain('Required')
  })

  it('tells a person that saving an agent draft confirms it', async () => {
    const draft = readiness({ source: 'agent', confirmed_by: null, overall: 'unrated' })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) =>
        String(input).includes('/users')
          ? json({ items: [], total: 0, limit: 100, offset: 0 })
          : json({ ...draft, confirmed_by_name: null }),
      ),
    )
    const section = mount(ReadinessSection, {
      props: { request: request(), canWrite: true },
      global: { plugins: [makeVuetify()] },
      attachTo: document.body,
    })
    await settle()
    await section.get('[data-test="readiness-edit"]').trigger('click')
    await settle()
    expect(section.get('[data-test="readiness-form"]').text()).toContain(
      'records you as the person who confirmed it',
    )
  })
})

describe('submitting an AI request without a complete readiness check', () => {
  it.each([[MISSING], [MISSING_ALL]])('lists what is missing: %j', async (missing) => {
    const { wrapper } = await mountAt('project_manager', { submitMissing: missing })
    await wrapper.get('[data-test="submit"]').trigger('click')
    await settle()
    expect(wrapper.get('[data-test="message"]').text()).toContain("can't be submitted")
    const items = wrapper.findAll('[data-test="missing"] li').map((li) => li.text())
    expect(items).toEqual(missing)
  })

  it('submits once nothing is missing, and hides the edit button', async () => {
    const { wrapper } = await mountAt('project_manager')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await settle()
    expect(wrapper.get('[data-test="message"]').text()).toContain('Request submitted.')
    expect(wrapper.find('[data-test="readiness-edit"]').exists()).toBe(false)
  })
})
