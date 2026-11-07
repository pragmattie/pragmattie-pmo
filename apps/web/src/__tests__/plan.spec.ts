import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import PlanSection from '../components/PlanSection.vue'
import {
  amountError,
  budgetFitText,
  canEditPlan,
  type Milestone,
  ownerText,
  type Plan,
  reportingText,
} from '../lib/plans'
import { makeVuetify } from '../plugins/vuetify'
import { useAuthStore } from '../stores/auth'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('visualViewport', new EventTarget())

function plan(changes: Partial<Plan> = {}): Plan {
  return {
    id: 2,
    project_id: 5,
    scope_in: 'A weekly forecast for two warehouses',
    scope_out: 'Stores',
    contingency_pct: 10,
    report_period: 'weekly',
    report_due_day: 5,
    lines_total: '12000.00',
    contingency_amount: '1200.00',
    total: '13200.00',
    budget_low: '10000.00',
    budget_high: '20000.00',
    budget_fit: 'within',
    missing: [],
    editable: true,
    version: 4,
    updated_at: '2026-11-04T09:00:00',
    ...changes,
  }
}

function milestone(changes: Partial<Milestone> = {}): Milestone {
  return {
    id: 1,
    plan_id: 2,
    name: 'Pilot',
    target_date: '2027-03-01',
    owner_user_id: 7,
    owner_name: 'Pat Manager',
    owner_active: true,
    description: 'The pilot warehouse runs on the forecast',
    contributors: 'Data team',
    source: 'person',
    version: 1,
    ...changes,
  }
}

const MILESTONES = [
  milestone(),
  milestone({ id: 3, name: 'Go live', target_date: '2027-06-01', owner_user_id: null, owner_name: null, owner_active: false, source: 'agent' }),
]
const DELIVERABLES = [
  { id: 1, plan_id: 2, name: 'Forecast model', description: null, milestone_id: 1, milestone_name: 'Pilot', source: 'agent', version: 1 },
  { id: 2, plan_id: 2, name: 'Runbook', description: 'How to run it', milestone_id: null, milestone_name: null, source: 'person', version: 1 },
]
const LINES = [
  { id: 1, plan_id: 2, description: 'Team', category: 'people', amount: '10000.00', source: 'person', version: 1 },
  { id: 2, plan_id: 2, description: 'Licence', category: 'software', amount: '2000.00', source: 'agent', version: 1 },
]

// One page of `items` as the API sends it: at most `limit` from `offset`, with the full total.
function page(items: unknown[], url = 'http://localhost/') {
  const params = new URL(url, 'http://localhost').searchParams
  const limit = Number(params.get('limit') ?? 100)
  const offset = Number(params.get('offset') ?? 0)
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeApi(found: Plan | null, lines: unknown[] = LINES) {
  return vi.fn(async (input: string) => {
    const path = new URL(input, 'http://localhost').pathname
    if (path === '/api/v1/projects/5/plan') {
      return found ? json(found) : json({ detail: 'this project has no plan' }, 404)
    }
    if (path === '/api/v1/projects/5/plan/milestones') return json(page(MILESTONES, input))
    if (path === '/api/v1/projects/5/plan/deliverables') return json(page(DELIVERABLES, input))
    if (path === '/api/v1/projects/5/plan/estimate-lines') return json(page(lines, input))
    return json({ detail: `unexpected ${path}` }, 404)
  })
}

// A signed-in user holding `actions`; none by default, so the plan reads only.
function signedIn(actions: string[] = []) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = {
    id: 7,
    email: 'pm@pragmattie.example',
    display_name: 'Pat Manager',
    role: actions.length ? 'project_manager' : 'auditor',
    actions,
  }
  return pinia
}

async function mountPlan(found: Plan | null = plan(), actions: string[] = []) {
  vi.stubGlobal('fetch', fakeApi(found))
  const wrapper = mount(PlanSection, {
    props: { projectId: 5 },
    global: { plugins: [signedIn(actions), makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function find(testId: string) {
  return document.body.querySelector(`[data-test="${testId}"]`)
}

function findAll(testId: string) {
  return Array.from(document.body.querySelectorAll(`[data-test="${testId}"]`))
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the plan helpers', () => {
  it('words the reporting plan by its period', () => {
    expect(reportingText('weekly', 5)).toBe('Weekly, due on Friday')
    expect(reportingText('fortnightly', 1)).toBe('Fortnightly, due on Monday')
    expect(reportingText('monthly', 1)).toBe('Monthly, due on the 1st')
    expect(reportingText('monthly', 22)).toBe('Monthly, due on the 22nd')
    expect(reportingText('monthly', 13)).toBe('Monthly, due on the 13th')
    expect(reportingText('weekly', null)).toBe('Not set')
    expect(reportingText(null, null)).toBe('Not set')
  })

  it('words the budget fit', () => {
    expect(budgetFitText('within')).toBe('Within the budget range')
    expect(budgetFitText('below')).toBe('Below the budget range')
    expect(budgetFitText('above')).toBe('Above the budget range')
    expect(budgetFitText('no_range')).toBe('The brief has no budget range')
  })

  it('checks an amount typed as text', () => {
    expect(amountError('25000')).toBeNull()
    expect(amountError(' 25000.5 ')).toBeNull()
    expect(amountError('0.01')).toBeNull()
    expect(amountError('')).toBe('Required')
    expect(amountError('12,000')).toBe('An amount such as 25000 or 25000.50')
    expect(amountError('1.234')).toBe('An amount such as 25000 or 25000.50')
    expect(amountError('-5')).toBe('An amount such as 25000 or 25000.50')
    expect(amountError('12345678901')).toBe('An amount such as 25000 or 25000.50')
    expect(amountError('0.00')).toBe('The amount must be above zero')
  })

  it('lets only project:write edit, and only an editable plan', () => {
    expect(canEditPlan(plan(), true)).toBe(true)
    expect(canEditPlan(plan(), false)).toBe(false)
    expect(canEditPlan(plan({ editable: false }), true)).toBe(false)
    expect(canEditPlan(null, true)).toBe(false)
  })

  it('names an owner, a missing one and one no longer active', () => {
    expect(ownerText(milestone())).toBe('Pat Manager')
    expect(ownerText(milestone({ owner_user_id: null, owner_name: null }))).toBe('No owner')
    expect(ownerText(milestone({ owner_active: false }))).toBe('Pat Manager (no longer active)')
  })
})

describe('the plan section', () => {
  it('shows scope, deliverables, milestones in the order the API sends and the reporting plan', async () => {
    await mountPlan()
    expect(find('plan-scope-in')?.textContent).toBe('A weekly forecast for two warehouses')
    const deliverables = findAll('plan-deliverable').map((row) => row.textContent)
    expect(deliverables[0]).toContain('Pilot')
    expect(deliverables[1]).toContain('No milestone')
    const milestones = findAll('plan-milestone').map((row) => row.textContent)
    expect(milestones[0]).toContain('2027-03-01')
    expect(milestones[0]).toContain('Pat Manager')
    expect(milestones[0]).toContain('Data team')
    expect(milestones[1]).toContain('No owner')
    expect(find('plan-reporting')?.textContent).toContain('Weekly, due on Friday')
    expect(find('plan-stakeholders-link')?.getAttribute('href')).toBe('#stakeholders')
  })

  it('marks the items the planner agent drafted, and only those', async () => {
    await mountPlan()
    const marked = (testId: string) =>
      findAll(testId).map((row) => row.querySelector('[data-test="plan-agent-drafted"]')?.textContent?.trim() ?? null)
    expect(marked('plan-milestone')).toEqual([null, 'Agent draft'])
    expect(marked('plan-deliverable')).toEqual(['Agent draft', null])
    expect(marked('plan-line')).toEqual([null, 'Agent draft'])
  })

  it('shows the total the API computed against the budget range', async () => {
    await mountPlan()
    expect(findAll('plan-line')).toHaveLength(2)
    expect(find('plan-lines-total')?.textContent).toBe('12000.00')
    expect(find('plan-total')?.textContent).toBe('13200.00')
    expect(find('plan-budget-fit')?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'Within the budget range (10000.00 to 20000.00).',
    )
  })

  it('compares against a low bound alone', async () => {
    await mountPlan(plan({ budget_fit: 'below', budget_low: '20000.00', budget_high: null }))
    expect(find('plan-budget-fit')?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'Below the budget range (from 20000.00).',
    )
  })

  it('compares against a high bound alone', async () => {
    await mountPlan(plan({ budget_fit: 'above', budget_low: null, budget_high: '10000.00' }))
    expect(find('plan-budget-fit')?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'Above the budget range (up to 10000.00).',
    )
  })

  it('says when the brief has no budget range', async () => {
    await mountPlan(plan({ budget_fit: 'no_range', budget_low: null, budget_high: null }))
    expect(find('plan-budget-fit')?.textContent?.trim()).toBe('The brief has no budget range.')
  })

  it('lists what is missing at the top', async () => {
    const missing = ["Milestone 'Go live' has no owner", "Deliverable 'Runbook' has no milestone"]
    await mountPlan(plan({ missing }))
    expect(findAll('plan-gap').map((item) => item.textContent)).toEqual(missing)
    expect(find('plan-complete')).toBeNull()
    const section = find('plan')
    const first = section?.querySelector('[data-test="plan-missing"]')
    expect(first?.compareDocumentPosition(find('plan-scope-in')!)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
  })

  it('says a complete plan misses nothing', async () => {
    await mountPlan()
    expect(find('plan-missing')).toBeNull()
    expect(find('plan-complete')).not.toBeNull()
    expect(find('plan-baselined')).toBeNull()
  })

  it('says a plan outside planning is changed through change requests', async () => {
    await mountPlan(plan({ editable: false }))
    expect(find('plan-baselined')).not.toBeNull()
  })

  it('reads every page of a long list, not only the first 100', async () => {
    const many = Array.from({ length: 230 }, (_, i) => ({
      id: i + 1,
      plan_id: 2,
      description: `Line ${i + 1}`,
      category: 'other',
      amount: '1.00',
      version: 1,
    }))
    const api = fakeApi(plan(), many)
    vi.stubGlobal('fetch', api)
    mount(PlanSection, {
      props: { projectId: 5 },
      global: { plugins: [signedIn(), makeVuetify()] },
      attachTo: document.body,
    })
    await flushPromises()
    const rows = findAll('plan-line')
    expect(rows).toHaveLength(230)
    expect(rows[229].textContent).toContain('Line 230')
    const linePages = api.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes('/estimate-lines'))
    expect(linePages).toEqual([
      '/api/v1/projects/5/plan/estimate-lines?limit=100&offset=0',
      '/api/v1/projects/5/plan/estimate-lines?limit=100&offset=100',
      '/api/v1/projects/5/plan/estimate-lines?limit=100&offset=200',
    ])
  })

  it('says when the project has no plan', async () => {
    await mountPlan(null)
    expect(find('no-plan')?.textContent).toContain('this project has no plan')
  })
})

// --- Editing the plan (item 6.6)

const WRITE = ['project:read', 'project:write']
const STALE = 'This plan has changed since you loaded it. Reload and try again.'
const PLAN_PATH = '/api/v1/projects/5/plan'

type Answer = { status: number; body?: unknown }

// The read routes as above, plus every write: each answers `answers[method path]` if set (a 409,
// say), else a success. `fetch` records every call so a test can check what was sent.
function writableApi(answers: Record<string, Answer> = {}) {
  const read = fakeApi(plan())
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET'
    const path = new URL(input, 'http://localhost').pathname
    if (method === 'GET') {
      if (path === '/api/v1/users') return json(page([{ id: 7, display_name: 'Pat Manager' }], input))
      return read(input)
    }
    const answer = answers[`${method} ${path}`]
    if (answer) return json(answer.body ?? {}, answer.status)
    if (method === 'DELETE') return new Response(null, { status: 204 })
    return json(path.endsWith('/plan') || path.endsWith('/confirm') ? plan({ version: 5 }) : {}, method === 'POST' && !path.endsWith('/confirm') ? 201 : 200)
  })
}

type Fetch = ReturnType<typeof writableApi>

function calls(api: Fetch, method: string, path: string) {
  return api.mock.calls.filter(
    ([input, init]) =>
      (init?.method ?? 'GET') === method && new URL(input, 'http://localhost').pathname === path,
  )
}

function sent(api: Fetch, method: string, path: string) {
  const found = calls(api, method, path)
  expect(found).toHaveLength(1)
  const body = found[0][1]?.body
  return body ? JSON.parse(String(body)) : null
}

async function settle() {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve))
  await flushPromises()
}

async function mountWritable(answers: Record<string, Answer> = {}) {
  const api = writableApi(answers)
  vi.stubGlobal('fetch', api)
  const wrapper = mount(PlanSection, {
    props: { projectId: 5 },
    global: { plugins: [signedIn(WRITE), makeVuetify()] },
    attachTo: document.body,
  })
  await settle()
  return { wrapper, api }
}

// The button with `testId` in the `index`th row with `rowTestId`.
function rowButton(rowTestId: string, index: number, testId: string) {
  const button = findAll(rowTestId)[index]?.querySelector(`[data-test="${testId}"]`)
  expect(button).not.toBeNull()
  return button as HTMLElement
}

async function click(element: Element | null) {
  ;(element as HTMLElement).click()
  await settle()
}

const CONTROLS = [
  'plan-edit',
  'plan-add-deliverable',
  'plan-add-milestone',
  'plan-add-line',
  'plan-item-edit',
  'plan-item-delete',
  'plan-item-confirm',
  'plan-confirm-all',
]

describe('editing the plan', () => {
  it('shows no edit controls to a role without project:write', async () => {
    await mountPlan(plan({ missing: ['No reporting plan'] }), ['project:read'])
    for (const control of CONTROLS) expect(findAll(control)).toHaveLength(0)
    expect(findAll('plan-gap').map((gap) => gap.textContent)).toEqual(['No reporting plan'])
    // Agent items are still marked.
    expect(findAll('plan-agent-drafted')).toHaveLength(3)
  })

  it('shows no edit controls once the project has left planning, even to project:write', async () => {
    await mountPlan(plan({ editable: false, missing: ['No reporting plan'] }), WRITE)
    for (const control of CONTROLS) expect(findAll(control)).toHaveLength(0)
    expect(find('plan-baselined')).not.toBeNull()
    expect(findAll('plan-gap').map((gap) => gap.textContent)).toEqual(['No reporting plan'])
  })

  it('shows project:write the edit controls in planning, with Confirm on agent items only', async () => {
    await mountWritable()
    expect(find('plan-edit')).not.toBeNull()
    expect(find('plan-confirm-all')).not.toBeNull()
    expect(findAll('plan-item-edit')).toHaveLength(6)
    expect(findAll('plan-item-delete')).toHaveLength(6)
    const confirmable = (rows: string) =>
      findAll(rows).map((row) => !!row.querySelector('[data-test="plan-item-confirm"]'))
    expect(confirmable('plan-milestone')).toEqual([false, true])
    expect(confirmable('plan-deliverable')).toEqual([true, false])
    expect(confirmable('plan-line')).toEqual([false, true])
  })

  it('confirms one agent item with its own version and reloads the plan', async () => {
    const { api } = await mountWritable()
    await click(rowButton('plan-deliverable', 0, 'plan-item-confirm'))
    expect(sent(api, 'POST', `${PLAN_PATH}/confirm`)).toEqual({
      kind: 'deliverable',
      id: 1,
      version: 1,
    })
    expect(calls(api, 'GET', PLAN_PATH)).toHaveLength(2)
  })

  it('confirms every agent item with the plan version', async () => {
    const { api } = await mountWritable()
    await click(find('plan-confirm-all'))
    expect(sent(api, 'POST', `${PLAN_PATH}/confirm`)).toEqual({ version: 4 })
    expect(calls(api, 'GET', PLAN_PATH)).toHaveLength(2)
  })

  it('edits scope, contingency and reporting with the plan version', async () => {
    const { wrapper, api } = await mountWritable()
    await click(find('plan-edit'))
    await wrapper.get('[data-test="plan-field-scope-out"] textarea').setValue('  Stores  ')
    await wrapper.get('[data-test="plan-field-contingency"] input').setValue('15')
    await click(find('plan-save'))
    expect(sent(api, 'PATCH', PLAN_PATH)).toEqual({
      version: 4,
      scope_in: 'A weekly forecast for two warehouses',
      scope_out: 'Stores',
      contingency_pct: 15,
      report_period: 'weekly',
      report_due_day: 5,
    })
    expect(find('plan-form')).toBeNull()
  })

  it('refuses a contingency out of range in the browser', async () => {
    const { wrapper, api } = await mountWritable()
    await click(find('plan-edit'))
    await wrapper.get('[data-test="plan-field-contingency"] input').setValue('60')
    await click(find('plan-save'))
    expect(find('plan-form')?.textContent).toContain('A whole number from 0 to 50')
    expect(calls(api, 'PATCH', PLAN_PATH)).toHaveLength(0)
  })

  it('checks an amount in the browser before adding an estimate line', async () => {
    const { wrapper, api } = await mountWritable()
    await click(find('plan-add-line'))
    await wrapper.get('[data-test="plan-field-description"] input').setValue('Cloud hosting')
    await wrapper.get('[data-test="plan-field-amount"] input').setValue('12,000')
    await click(find('plan-item-save'))
    expect(find('plan-item-form')?.textContent).toContain('An amount such as 25000 or 25000.50')
    expect(calls(api, 'POST', `${PLAN_PATH}/estimate-lines`)).toHaveLength(0)

    await wrapper.get('[data-test="plan-field-amount"] input').setValue('1500.50')
    await click(find('plan-item-save'))
    expect(sent(api, 'POST', `${PLAN_PATH}/estimate-lines`)).toEqual({
      version: 4,
      description: 'Cloud hosting',
      category: 'people',
      amount: '1500.50',
    })
  })

  it("shows the API's refusal of an amount and keeps the form", async () => {
    const detail = [{ msg: 'Input should be greater than 0' }]
    const { wrapper } = await mountWritable({
      [`PATCH ${PLAN_PATH}/estimate-lines/2`]: { status: 422, body: { detail } },
    })
    await click(rowButton('plan-line', 1, 'plan-item-edit'))
    await wrapper.get('[data-test="plan-field-amount"] input').setValue('2500')
    await click(find('plan-item-save'))
    expect(find('plan-form-error')?.textContent).toContain('Input should be greater than 0')
    expect(find('plan-item-form')).not.toBeNull()
  })

  it('edits a milestone with its own version', async () => {
    const { wrapper, api } = await mountWritable()
    await click(rowButton('plan-milestone', 1, 'plan-item-edit'))
    await wrapper.get('[data-test="plan-field-name"] input').setValue('Go live in both warehouses')
    await click(find('plan-item-save'))
    expect(sent(api, 'PATCH', `${PLAN_PATH}/milestones/3`)).toEqual({
      version: 1,
      name: 'Go live in both warehouses',
      target_date: '2027-06-01',
      owner_user_id: null,
      description: 'The pilot warehouse runs on the forecast',
      contributors: 'Data team',
    })
  })

  it('adds a deliverable with the plan version', async () => {
    const { wrapper, api } = await mountWritable()
    await click(find('plan-add-deliverable'))
    await wrapper.get('[data-test="plan-field-name"] input').setValue('Training')
    await click(find('plan-item-save'))
    expect(sent(api, 'POST', `${PLAN_PATH}/deliverables`)).toEqual({
      version: 4,
      name: 'Training',
      description: null,
      milestone_id: null,
    })
  })

  it('deletes an item with its version after asking', async () => {
    const { api } = await mountWritable()
    await click(rowButton('plan-deliverable', 1, 'plan-item-delete'))
    expect(find('plan-delete-confirm')?.textContent).toContain('Delete the deliverable "Runbook"?')
    expect(calls(api, 'DELETE', `${PLAN_PATH}/deliverables/2`)).toHaveLength(0)
    await click(find('plan-delete-yes'))
    const [[url]] = calls(api, 'DELETE', `${PLAN_PATH}/deliverables/2`)
    expect(url).toBe(`${PLAN_PATH}/deliverables/2?version=1`)
    expect(find('plan-delete-confirm')).toBeNull()
  })

  it('shows the conflict message and reloads when a version is stale', async () => {
    const { wrapper, api } = await mountWritable({
      [`PATCH ${PLAN_PATH}`]: { status: 409, body: { detail: STALE } },
    })
    await click(find('plan-edit'))
    await wrapper.get('[data-test="plan-field-contingency"] input').setValue('5')
    await click(find('plan-save'))
    expect(find('plan-conflict')?.textContent).toContain(STALE)
    expect(find('plan-form')).toBeNull()
    expect(calls(api, 'GET', PLAN_PATH)).toHaveLength(2)
  })

  it('shows the conflict message and reloads when a confirmation is stale', async () => {
    const { api } = await mountWritable({
      [`POST ${PLAN_PATH}/confirm`]: { status: 409, body: { detail: STALE } },
    })
    await click(rowButton('plan-line', 1, 'plan-item-confirm'))
    expect(find('plan-conflict')?.textContent).toContain(STALE)
    expect(calls(api, 'GET', PLAN_PATH)).toHaveLength(2)
  })
})
