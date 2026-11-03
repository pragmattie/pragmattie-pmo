import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import ValueCaseSection from '../components/ValueCaseSection.vue'
import {
  canEditValueCase,
  confirmationText,
  costText,
  type ProjectRequest,
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

const MISSING = [
  'a benefit with a measure and a target',
  'a cost estimate: low, high and how it was estimated',
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
    budget_low: '1000.00',
    budget_high: '2000.00',
    wanted_by: null,
    uses_ai: false,
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

function valueCase(changes: Partial<ValueCase> = {}): ValueCase {
  return {
    request_id: 1,
    request_version: 3,
    benefits: [
      {
        description: 'Fewer stock-outs',
        measure: 'Stock-outs a quarter',
        current_value: '12',
        target: '6',
        target_date: '2027-12-31',
      },
    ],
    cost_low: '10000.00',
    cost_high: '25000.50',
    cost_method: 'Two weeks at the day rate',
    strategic_fit: "The client's goal of fewer stock-outs",
    cost_of_not_doing: null,
    source: 'person',
    confirmed_by: 11,
    confirmed_by_name: 'Pat Manager',
    confirmed_at: '2026-10-02T09:00:00',
    created_at: '2026-10-02T09:00:00',
    updated_at: '2026-10-02T09:00:00',
    ...changes,
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface FakeOptions {
  stored?: ProjectRequest
  valueCase?: ValueCase | null
  submitMissing?: boolean
}

function fakeApi(role: string, options: FakeOptions = {}) {
  const user = { id: 7, email: `${role}@pmo.example`, display_name: 'Test User', role }
  let stored = options.stored ?? request()
  let current = options.valueCase === undefined ? valueCase() : options.valueCase
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init.method ?? 'GET'
    if (path === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS[role] })
    if (path === '/api/v1/clients') return json({ items: [], total: 0, limit: 100, offset: 0 })
    if (path === '/api/v1/requests/1/history') {
      return json({ items: [], total: 0, limit: 50, offset: 0 })
    }
    if (path === '/api/v1/requests/1/value-case' && method === 'PUT') {
      const body = JSON.parse(String(init.body))
      const { version, ...fields } = body
      stored = { ...stored, version: version + 1 }
      current = valueCase({ ...fields, request_version: version + 1, confirmed_by: 7 })
      return json(current)
    }
    if (path === '/api/v1/requests/1/value-case') {
      return current ? json(current) : json({ detail: 'this request has no value case yet' }, 404)
    }
    if (path === '/api/v1/requests/1/submit' && method === 'POST') {
      if (options.submitMissing) {
        return json(
          {
            detail: {
              message: "this request can't be submitted until its value case is complete",
              missing: MISSING,
            },
          },
          409,
        )
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

describe('value case helpers', () => {
  it.each<[RequestStatus, boolean]>([
    ['draft', true],
    ['submitted', false],
    ['approved', false],
    ['declined', false],
    ['withdrawn', false],
  ])('a %s request has an editable value case: %s', (status, expected) => {
    expect(canEditValueCase({ status }, true)).toBe(expected)
    expect(canEditValueCase({ status }, false)).toBe(false)
  })

  it('shows the estimated cost and who confirmed it', () => {
    expect(costText({ cost_low: '100.00', cost_high: '2000.50' })).toBe('$100.00 to $2,000.50')
    expect(costText({ cost_low: null, cost_high: null })).toBe('Not estimated yet')
    const draft = valueCase({ source: 'agent', confirmed_by: null, confirmed_by_name: null })
    expect(confirmationText(draft)).toContain('a person must confirm it')
    expect(confirmationText(valueCase())).toContain('Confirmed by Pat Manager')
    expect(confirmationText(valueCase({ source: 'agent' }))).toContain('(drafted by an agent)')
  })
})

describe('the value case section', () => {
  it('shows the benefits, estimated cost, strategic fit and confirmation', async () => {
    const { wrapper } = await mountAt('sponsor')
    const section = wrapper.get('[data-test="value-case"]')
    expect(section.text()).toContain('Value case')
    expect(wrapper.get('[data-test="benefit-0"]').text()).toContain('Fewer stock-outs')
    expect(wrapper.get('[data-test="benefit-0"]').text()).toContain(
      'Measured by: Stock-outs a quarter, now 12, target 6, by 2027-12-31',
    )
    expect(wrapper.get('[data-test="value-case-cost"]').text()).toBe('$10,000.00 to $25,000.50')
    expect(wrapper.get('[data-test="value-case-fit"]').text()).toContain('fewer stock-outs')
    expect(wrapper.get('[data-test="value-case-not-doing"]').text()).toBe('Not given')
    expect(wrapper.get('[data-test="value-case-confirmed"]').text()).toContain('Pat Manager')
    // The request's own range is labelled apart from our estimate.
    expect(wrapper.get('[data-test="field-Budget available"]').text()).toBe('$1,000.00 to $2,000.00')
  })

  it('marks an agent draft with a chip until a person confirms it', async () => {
    const draft = valueCase({ source: 'agent', confirmed_by: null, confirmed_by_name: null })
    const { wrapper } = await mountAt('project_manager', { valueCase: draft })
    expect(wrapper.get('[data-test="value-case-agent-draft"]').text()).toBe(
      'Agent draft, not confirmed',
    )
  })

  it.each([
    ['person', 11],
    ['agent', 11],
  ] as const)('shows no agent chip on a confirmed %s value case', async (source, by) => {
    const { wrapper } = await mountAt('sponsor', {
      valueCase: valueCase({ source, confirmed_by: by }),
    })
    expect(wrapper.find('[data-test="value-case-agent-draft"]').exists()).toBe(false)
  })

  it('says when there is no value case yet', async () => {
    const { wrapper } = await mountAt('auditor', { valueCase: null })
    expect(wrapper.get('[data-test="value-case-none"]').text()).toContain('No value case yet')
  })

  it.each<[string, RequestStatus, boolean]>([
    ['project_manager', 'draft', true],
    ['project_manager', 'submitted', false],
    ['project_manager', 'withdrawn', false],
    ['project_manager', 'approved', false],
    ['sponsor', 'draft', false],
    ['auditor', 'draft', false],
    ['admin', 'draft', false],
  ])('%s on a %s request sees the edit button: %s', async (role, status, shown) => {
    const { wrapper } = await mountAt(role, { stored: request({ status }) })
    expect(wrapper.find('[data-test="value-case-edit"]').exists()).toBe(shown)
  })

  it('saves the edited value case with the request version it read', async () => {
    const { wrapper, fetchMock } = await mountAt('project_manager')
    await wrapper.get('[data-test="value-case-edit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="value-case-form"]').exists()).toBe(true)
    await wrapper.get('[data-test="add-benefit"]').trigger('click')
    const descriptions = wrapper.findAll('[data-test="field-benefit-description"] textarea')
    expect(descriptions).toHaveLength(2)
    await descriptions[1].setValue('  Happier staff  ')
    await wrapper.get('[data-test="value-case-save"]').trigger('click')
    await settle()

    const puts = calls(fetchMock, 'PUT', '/api/v1/requests/1/value-case')
    expect(puts).toHaveLength(1)
    expect(JSON.parse(String(puts[0][1]!.body))).toEqual({
      version: 3,
      benefits: [
        valueCase().benefits[0],
        {
          description: 'Happier staff',
          measure: null,
          current_value: null,
          target: null,
          target_date: null,
        },
      ],
      cost_low: '10000.00',
      cost_high: '25000.50',
      cost_method: 'Two weeks at the day rate',
      strategic_fit: "The client's goal of fewer stock-outs",
      cost_of_not_doing: null,
    })
    expect(wrapper.find('[data-test="value-case-form"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="message"]').text()).toContain('Value case saved.')
    expect(wrapper.find('[data-test="benefit-1"]').exists()).toBe(true)
  })

  it('adds a first value case from a blank form', async () => {
    const { wrapper, fetchMock } = await mountAt('project_manager', { valueCase: null })
    const edit = wrapper.get('[data-test="value-case-edit"]')
    expect(edit.text()).toBe('Add value case')
    await edit.trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="field-benefit-description"] textarea').setValue('Fewer stock-outs')
    await wrapper.get('[data-test="field-strategic-fit"] textarea').setValue('Client goal')
    await wrapper.get('[data-test="value-case-save"]').trigger('click')
    await settle()
    const [[, init]] = calls(fetchMock, 'PUT', '/api/v1/requests/1/value-case')
    expect(JSON.parse(String(init!.body))).toMatchObject({
      version: 3,
      benefits: [{ description: 'Fewer stock-outs', measure: null, target: null }],
      cost_low: null,
      cost_high: null,
      strategic_fit: 'Client goal',
    })
  })

  it('refuses to send a form with a cost out of order or no strategic fit', async () => {
    const { wrapper, fetchMock } = await mountAt('project_manager')
    await wrapper.get('[data-test="value-case-edit"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="field-cost-low"] input').setValue('500')
    await wrapper.get('[data-test="field-cost-high"] input').setValue('100')
    await wrapper.get('[data-test="field-strategic-fit"] textarea').setValue('')
    await wrapper.get('[data-test="value-case-save"]').trigger('click')
    await settle()
    expect(wrapper.get('[data-test="value-case-form"]').text()).toContain(
      'Low must not be above high',
    )
    expect(calls(fetchMock, 'PUT', '/api/v1/requests/1/value-case')).toHaveLength(0)
  })

  it('tells a person that saving an agent draft confirms it', async () => {
    const draft = valueCase({ source: 'agent', confirmed_by: null, confirmed_by_name: null })
    vi.stubGlobal('fetch', vi.fn(async () => json(draft)))
    const section = mount(ValueCaseSection, {
      props: { request: request(), canWrite: true },
      global: { plugins: [makeVuetify()] },
      attachTo: document.body,
    })
    await settle()
    expect(section.get('[data-test="value-case-confirmed"]').text()).toContain(
      'a person must confirm it',
    )
    await section.get('[data-test="value-case-edit"]').trigger('click')
    await flushPromises()
    expect(section.get('[data-test="value-case-form"]').text()).toContain(
      'records you as the person who confirmed it',
    )
  })
})

describe('submitting without a complete value case', () => {
  it('lists what is missing', async () => {
    const { wrapper } = await mountAt('project_manager', { submitMissing: true })
    await wrapper.get('[data-test="submit"]').trigger('click')
    await settle()
    const message = wrapper.get('[data-test="message"]')
    expect(message.text()).toContain("can't be submitted")
    const items = wrapper.findAll('[data-test="missing"] li').map((li) => li.text())
    expect(items).toEqual(MISSING)
  })

  it('submits once nothing is missing, and hides the edit button', async () => {
    const { wrapper } = await mountAt('project_manager')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await settle()
    expect(wrapper.get('[data-test="message"]').text()).toContain('Request submitted.')
    expect(wrapper.find('[data-test="missing"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="value-case-edit"]').exists()).toBe(false)
  })
})
