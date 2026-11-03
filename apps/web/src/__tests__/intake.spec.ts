import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import RequestForm from '../components/RequestForm.vue'
import {
  budgetText,
  canDraftRequest,
  canEditRequest,
  canSubmitRequest,
  canWithdrawRequest,
  draftText,
  moneyText,
  NO_QUEUE_FILTERS,
  type ProjectRequest,
  queueQuery,
  REQUEST_CONFLICT,
  type RequestStatus,
} from '../lib/intake'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
// jsdom has no visual viewport; Vuetify's dialogs read it when they open.
vi.stubGlobal('visualViewport', new EventTarget())

// The actions /auth/me sends for each role (the API's table in app/authz.py).
const ACTIONS: Record<string, string[]> = {
  admin: ['audit:read', 'project:read', 'standards:read', 'user:admin'],
  sponsor: ['gate:decide', 'intake:decide', 'project:read', 'proposal:approve', 'risk:accept'],
  project_manager: ['project:read', 'project:write', 'report:publish', 'risk:accept'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

function request(id: number, changes: Partial<ProjectRequest> = {}): ProjectRequest {
  return {
    id,
    title: `Request ${id}`,
    client_id: 4,
    client_name: 'Northwind Example Ltd',
    asked_for: 'A forecast',
    problem: 'Stock-outs',
    outcome: 'Fewer stock-outs',
    budget_low: '10000.00',
    budget_high: '25000.50',
    wanted_by: '2027-03-31',
    uses_ai: true,
    known_risks: null,
    requested_by: 11,
    requested_by_name: 'Pat Manager',
    status: 'draft',
    submitted_at: null,
    version: 1,
    created_at: '2026-10-01T09:00:00',
    updated_at: '2026-10-01T09:00:00',
    ...changes,
  }
}

const QUEUE = [
  request(1, { status: 'submitted', submitted_at: '2026-10-02T09:00:00' }),
  request(2, { client_id: null, client_name: null, uses_ai: false, wanted_by: null }),
  request(3, { status: 'withdrawn' }),
]

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function page(items: unknown[]) {
  return { items, total: items.length, limit: 25, offset: 0 }
}

interface FakeOptions {
  stored?: ProjectRequest
  status?: number
  // The intake agent's answer: 409 when AGENT_MODE is off.
  draftStatus?: number
}

const DRAFTED = {
  mode: 'enforce',
  dry_run: false,
  model: 'claude-sonnet-5-5',
  prompt_version: 'intake-agent-2026-11-03.1',
  cost_ceiling_usd: null,
  saved: ['value_case', 'readiness'],
  skipped: [],
  dropped: [],
  adjusted: [],
  audit_id: 40,
}

function fakeApi(role: string, options: FakeOptions = {}) {
  const user = { id: 7, email: `${role}@pmo.example`, display_name: 'Test User', role }
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input, 'http://localhost')
    const path = url.pathname
    const method = init.method ?? 'GET'
    if (path === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS[role] })
    if (path === '/api/v1/clients') return json(page([{ id: 4, name: 'Northwind Example Ltd' }]))
    if (path === '/api/v1/requests' && method === 'GET') return json(page(QUEUE))
    if (path === '/api/v1/requests' && method === 'POST') {
      return json({ ...request(9), ...JSON.parse(String(init.body)) }, 201)
    }
    const history = path.match(/^\/api\/v1\/requests\/(\d+)\/history$/)
    if (history) {
      return json(
        page([
          {
            id: 90,
            version: 1,
            action: 'create',
            actor_user_id: 11,
            actor_name: 'Pat Manager',
            before: null,
            after: { title: 'Request 1' },
            created_at: '2026-10-01T09:00:00',
          },
        ]),
      )
    }
    const move = path.match(/^\/api\/v1\/requests\/(\d+)\/(submit|withdraw)$/)
    if (move && method === 'POST') {
      if (options.status === 409) {
        return json({ detail: 'this request has changed since you read it' }, 409)
      }
      const status = move[2] === 'submit' ? 'submitted' : 'withdrawn'
      return json(request(Number(move[1]), { status, version: 2 }))
    }
    if (/^\/api\/v1\/requests\/\d+\/draft$/.test(path) && method === 'POST') {
      if (options.draftStatus === 409) {
        return json({ detail: 'the intake agent is off (AGENT_MODE=off)' }, 409)
      }
      return json(DRAFTED)
    }
    const one = path.match(/^\/api\/v1\/requests\/(\d+)$/)
    if (one && method === 'PATCH') {
      return json({ ...request(Number(one[1])), ...JSON.parse(String(init.body)), version: 2 })
    }
    if (one) return json(options.stored ?? request(Number(one[1])))
    return json({ detail: `unexpected ${method} ${path}` }, 404)
  })
}

async function mountAt(path: string, role: string, options: FakeOptions = {}) {
  const fetchMock = fakeApi(role, options)
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

type Mounted = Awaited<ReturnType<typeof mountAt>>
type Wrapper = Mounted['wrapper']

function queueCalls(fetchMock: Mounted['fetchMock']): Record<string, string>[] {
  return fetchMock.mock.calls
    .map(([input, init]) => [new URL(String(input), 'http://localhost'), init] as const)
    .filter(([url, init]) => url.pathname === '/api/v1/requests' && init?.method === 'GET')
    .map(([url]) => Object.fromEntries(url.searchParams))
}

function calls(fetchMock: Mounted['fetchMock'], method: string, path: string) {
  return fetchMock.mock.calls.filter(
    ([input, init]) =>
      (init?.method ?? 'GET') === method &&
      new URL(String(input), 'http://localhost').pathname === path,
  )
}

function setSelect(wrapper: Wrapper, test: string, value: unknown, name = 'VSelect') {
  const select = wrapper
    .findAllComponents({ name })
    .find((c) => c.attributes('data-test') === test)
  expect(select, test).toBeTruthy()
  select!.vm.$emit('update:modelValue', value)
}

function buttons(wrapper: Wrapper) {
  return ['edit', 'submit', 'withdraw'].filter((name) =>
    wrapper.find(`[data-test="${name}"]`).exists(),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
  document.body.innerHTML = ''
})

describe('intake helpers', () => {
  it.each<[RequestStatus, string[]]>([
    ['draft', ['edit', 'submit', 'withdraw']],
    ['submitted', ['withdraw']],
    ['approved', []],
    ['declined', []],
    ['withdrawn', []],
  ])('a %s request allows %j to a project manager', (status, expected) => {
    const allowed = {
      edit: canEditRequest({ status }, true),
      submit: canSubmitRequest({ status }, true),
      withdraw: canWithdrawRequest({ status }, true),
    }
    expect(Object.keys(allowed).filter((k) => allowed[k as keyof typeof allowed])).toEqual(expected)
    expect(canEditRequest({ status }, false)).toBe(false)
    expect(canSubmitRequest({ status }, false)).toBe(false)
    expect(canWithdrawRequest({ status }, false)).toBe(false)
  })

  it('builds the queue query from the filters, sort and page', () => {
    const query = queueQuery({ ...NO_QUEUE_FILTERS, status: 'submitted', uses_ai: false }, 'wanted_by', 2, 25)
    expect(Object.fromEntries(new URLSearchParams(query))).toEqual({
      status: 'submitted',
      uses_ai: 'false',
      sort: 'wanted_by',
      limit: '25',
      offset: '25',
    })
  })

  it('offers the intake agent only on a draft, to a role that may write', () => {
    expect(canDraftRequest({ status: 'draft' }, true)).toBe(true)
    expect(canDraftRequest({ status: 'draft' }, false)).toBe(false)
    expect(canDraftRequest({ status: 'submitted' }, true)).toBe(false)
  })

  it('says what the intake agent saved, skipped, or that shadow mode saved nothing', () => {
    expect(draftText({ mode: 'enforce', saved: ['value_case'], skipped: [] })).toBe(
      'The agent drafted the value case. Check every part and save it to confirm it.',
    )
    const skipped = [{ section: 'readiness' as const, reason: 'a person has confirmed it' }]
    expect(draftText({ mode: 'enforce', saved: [], skipped })).toBe(
      'The agent saved nothing. Left alone, because a person confirmed it: readiness check.',
    )
    expect(draftText({ mode: 'shadow', saved: [], skipped: [] })).toContain('nothing was saved')
  })

  it('shows money and budget ranges', () => {
    expect(moneyText('1234567.50')).toBe('$1,234,567.50')
    expect(moneyText(null)).toBe('')
    expect(budgetText({ budget_low: '100.00', budget_high: '200.00' })).toBe('$100.00 to $200.00')
    expect(budgetText({ budget_low: null, budget_high: '200.00' })).toBe('Up to $200.00')
    expect(budgetText({ budget_low: null, budget_high: null })).toBe('Not given')
  })
})

describe('the queue', () => {
  it('lists requests with client, status, AI, wanted by and submitted', async () => {
    const { wrapper } = await mountAt('/intake', 'sponsor')
    const table = wrapper.get('[data-test="request-table"]')
    const headers = table.findAll('th').map((th) => th.text())
    expect(headers).toEqual(['Title', 'Client', 'Status', 'AI', 'Wanted by', 'Submitted'])
    expect(wrapper.get('[data-test="request-row-1"]').text()).toBe('Request 1')
    expect(wrapper.get('[data-test="status-1"]').text()).toBe('Submitted')
    expect(wrapper.get('[data-test="status-3"]').text()).toBe('Withdrawn')
    expect(wrapper.find('[data-test="ai-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="ai-2"]').exists()).toBe(false)
    const rows = table.findAll('tbody tr').map((tr) => tr.text())
    expect(rows[0]).toContain('Northwind Example Ltd')
    expect(rows[0]).toContain('2027-03-31')
    expect(rows[1]).toContain('Internal')
  })

  it('sends the filters and sort to the API', async () => {
    const { wrapper, fetchMock } = await mountAt('/intake', 'auditor')
    expect(queueCalls(fetchMock).at(-1)).toMatchObject({ sort: 'submitted_at', offset: '0' })
    setSelect(wrapper, 'filter-status', 'submitted')
    await flushPromises()
    setSelect(wrapper, 'filter-client', 4)
    await flushPromises()
    setSelect(wrapper, 'filter-ai', true)
    await flushPromises()
    setSelect(wrapper, 'sort', '-wanted_by')
    await flushPromises()
    expect(queueCalls(fetchMock).at(-1)).toEqual({
      status: 'submitted',
      client_id: '4',
      uses_ai: 'true',
      sort: '-wanted_by',
      limit: '25',
      offset: '0',
    })
  })

  it.each([
    ['project_manager', true],
    ['sponsor', false],
    ['auditor', false],
    ['admin', false],
  ])('shows New request to %s: %s', async (role, shown) => {
    const { wrapper } = await mountAt('/intake', role)
    expect(wrapper.find('[data-test="new-request"]').exists()).toBe(shown)
  })

  it('opens a request from the queue', async () => {
    const { wrapper, router } = await mountAt('/intake', 'sponsor')
    await wrapper.get('[data-test="request-row-1"]').trigger('click')
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve))
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('request')
    expect(router.currentRoute.value.params.id).toBe('1')
  })
})

describe('the new request form', () => {
  async function openForm() {
    const mounted = await mountAt('/intake', 'project_manager')
    await mounted.wrapper.get('[data-test="new-request"]').trigger('click')
    await flushPromises()
    return mounted
  }

  function form() {
    return document.querySelector('[data-test="request-form"]') as HTMLElement
  }

  // Text fields are set through the component (as setSelect does); textareas through the DOM.
  function fill(test: string, value: string, wrapper?: Wrapper) {
    if (wrapper) return setSelect(wrapper, test, value, 'VTextField')
    const input = form().querySelector(`[data-test="${test}"] input, [data-test="${test}"] textarea`)
    expect(input, test).toBeTruthy()
    ;(input as HTMLInputElement).value = value
    input!.dispatchEvent(new Event('input'))
  }

  async function save() {
    ;(form().querySelector('[data-test="form-save"]') as HTMLElement).click()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve))
    await flushPromises()
  }

  it('creates a draft from what the form saves, then opens it', async () => {
    const { wrapper, fetchMock, router } = await openForm()
    expect(form().textContent).toContain('New request')
    const input = {
      title: 'Forecast pilot',
      client_id: null,
      asked_for: 'A forecast',
      problem: 'Stock-outs',
      outcome: 'Fewer stock-outs',
      budget_low: '1000',
      budget_high: '2500.5',
      wanted_by: null,
      uses_ai: false,
      known_risks: null,
    }
    wrapper.findComponent(RequestForm).vm.$emit('save', input)
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve))
    await flushPromises()
    const posts = calls(fetchMock, 'POST', '/api/v1/requests')
    expect(posts).toHaveLength(1)
    const body = JSON.parse(String(posts[0][1]!.body))
    expect(body).toEqual(input)
    expect(body).not.toHaveProperty('requested_by')
    expect(router.currentRoute.value.name).toBe('request')
  })

  it('sends a filled-in form with empty optional fields as null', async () => {
    const vuetify = makeVuetify()
    const filled = request(1, { budget_low: ' 1000 ', budget_high: '', known_risks: '' })
    const formWrapper = mount(RequestForm, {
      props: { modelValue: true, request: filled, clients: [], busy: false, error: '' },
      global: { plugins: [vuetify] },
      attachTo: document.body,
    })
    await flushPromises()
    await save()
    const [[saved]] = formWrapper.emitted('save') as [[Record<string, unknown>]]
    expect(saved).toMatchObject({
      title: 'Request 1',
      budget_low: '1000',
      budget_high: null,
      known_risks: null,
      wanted_by: '2027-03-31',
    })
    expect(saved).not.toHaveProperty('requested_by')
    expect(saved).not.toHaveProperty('status')
  })

  it('refuses to send a form with missing fields or a budget out of order', async () => {
    const { wrapper, fetchMock } = await openForm()
    await save()
    fill('field-title', 'Forecast pilot', wrapper)
    fill('field-asked-for', 'A forecast')
    fill('field-problem', 'Stock-outs')
    fill('field-outcome', 'Fewer stock-outs')
    fill('field-budget-low', '500', wrapper)
    fill('field-budget-high', '100', wrapper)
    await save()
    expect(form().textContent).toContain('Low must not be above high')
    expect(calls(fetchMock, 'POST', '/api/v1/requests')).toHaveLength(0)
  })
})

describe('the request page', () => {
  it('shows the fields and the history', async () => {
    const { wrapper } = await mountAt('/intake/1', 'sponsor')
    expect(wrapper.get('[data-test="page-title"]').text()).toBe('Request 1')
    expect(wrapper.get('[data-test="field-Client"]').text()).toBe('Northwind Example Ltd')
    expect(wrapper.get('[data-test="field-Budget available"]').text()).toBe('$10,000.00 to $25,000.50')
    expect(wrapper.get('[data-test="field-Uses AI"]').text()).toBe('Yes')
    expect(wrapper.get('[data-test="field-Requested by"]').text()).toBe('Pat Manager')
    expect(wrapper.get('[data-test="history"]').text()).toContain('Create (version 1) by Pat Manager')
  })

  it.each<[string, RequestStatus, string[]]>([
    ['project_manager', 'draft', ['edit', 'submit', 'withdraw']],
    ['project_manager', 'submitted', ['withdraw']],
    ['project_manager', 'approved', []],
    ['project_manager', 'declined', []],
    ['project_manager', 'withdrawn', []],
    ['sponsor', 'draft', []],
    ['sponsor', 'submitted', []],
    ['auditor', 'draft', []],
    ['admin', 'submitted', []],
  ])('shows %s on a %s request the buttons %j', async (role, status, expected) => {
    const { wrapper } = await mountAt('/intake/1', role, { stored: request(1, { status }) })
    expect(buttons(wrapper)).toEqual(expected)
  })

  it.each<[string, RequestStatus, boolean]>([
    ['project_manager', 'draft', true],
    ['project_manager', 'submitted', false],
    ['project_manager', 'withdrawn', false],
    ['sponsor', 'draft', false],
    ['auditor', 'draft', false],
    ['admin', 'draft', false],
  ])('shows %s on a %s request the agent button: %s', async (role, status, expected) => {
    const { wrapper } = await mountAt('/intake/1', role, { stored: request(1, { status }) })
    expect(wrapper.find('[data-test="draft-agent"]').exists()).toBe(expected)
  })

  it('runs the intake agent, then reloads the request and says what it drafted', async () => {
    const { wrapper, fetchMock } = await mountAt('/intake/1', 'project_manager')
    const button = wrapper.get('[data-test="draft-agent"]')
    expect(button.text()).toBe('Draft with the agent')
    const reads = calls(fetchMock, 'GET', '/api/v1/requests/1').length
    await button.trigger('click')
    await flushPromises()
    const [[, init]] = calls(fetchMock, 'POST', '/api/v1/requests/1/draft')
    expect(init!.body).toBeUndefined()
    expect(calls(fetchMock, 'GET', '/api/v1/requests/1').length).toBe(reads + 1)
    expect(wrapper.get('[data-test="message"]').text()).toContain(
      'The agent drafted the value case and readiness check.',
    )
  })

  it('explains when the intake agent is off', async () => {
    const { wrapper } = await mountAt('/intake/1', 'project_manager', { draftStatus: 409 })
    await wrapper.get('[data-test="draft-agent"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="message"]').text()).toContain('AGENT_MODE=off')
  })

  it('submits a draft with the version it read', async () => {
    const { wrapper, fetchMock } = await mountAt('/intake/1', 'project_manager')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    const [[, init]] = calls(fetchMock, 'POST', '/api/v1/requests/1/submit')
    expect(JSON.parse(String(init!.body))).toEqual({ version: 1 })
    expect(wrapper.get('[data-test="message"]').text()).toContain('Request submitted.')
    expect(buttons(wrapper)).toEqual(['withdraw'])
  })

  it('withdraws after a confirmation', async () => {
    const { wrapper, fetchMock } = await mountAt('/intake/1', 'project_manager', {
      stored: request(1, { status: 'submitted' }),
    })
    await wrapper.get('[data-test="withdraw"]').trigger('click')
    await flushPromises()
    expect(calls(fetchMock, 'POST', '/api/v1/requests/1/withdraw')).toHaveLength(0)
    ;(document.querySelector('[data-test="confirm-withdraw-yes"]') as HTMLElement).click()
    await flushPromises()
    expect(calls(fetchMock, 'POST', '/api/v1/requests/1/withdraw')).toHaveLength(1)
    expect(buttons(wrapper)).toEqual([])
  })

  it('reloads and explains when someone else changed the request', async () => {
    const { wrapper, fetchMock } = await mountAt('/intake/1', 'project_manager', { status: 409 })
    const reads = calls(fetchMock, 'GET', '/api/v1/requests/1').length
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="message"]').text()).toContain(REQUEST_CONFLICT)
    expect(calls(fetchMock, 'GET', '/api/v1/requests/1').length).toBe(reads + 1)
  })

  it('edits a draft with the version it read', async () => {
    const { wrapper, fetchMock } = await mountAt('/intake/1', 'project_manager')
    await wrapper.get('[data-test="edit"]').trigger('click')
    await flushPromises()
    const form = document.querySelector('[data-test="request-form"]') as HTMLElement
    expect(form.textContent).toContain('Edit request')
    const title = form.querySelector('[data-test="field-title"] input') as HTMLInputElement
    expect(title.value).toBe('Request 1')
    setSelect(wrapper, 'field-title', 'Renamed', 'VTextField')
    ;(form.querySelector('[data-test="form-save"]') as HTMLElement).click()
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve))
    await flushPromises()
    const [[, init]] = calls(fetchMock, 'PATCH', '/api/v1/requests/1')
    expect(JSON.parse(String(init!.body))).toMatchObject({ title: 'Renamed', version: 1 })
    expect(wrapper.get('[data-test="page-title"]').text()).toBe('Renamed')
  })
})
