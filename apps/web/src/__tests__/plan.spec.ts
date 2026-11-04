import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import PlanSection from '../components/PlanSection.vue'
import {
  budgetFitText,
  type Milestone,
  ownerText,
  type Plan,
  reportingText,
} from '../lib/plans'
import { makeVuetify } from '../plugins/vuetify'

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
    version: 1,
    ...changes,
  }
}

const MILESTONES = [
  milestone(),
  milestone({ id: 3, name: 'Go live', target_date: '2027-06-01', owner_user_id: null, owner_name: null, owner_active: false }),
]
const DELIVERABLES = [
  { id: 1, plan_id: 2, name: 'Forecast model', description: null, milestone_id: 1, milestone_name: 'Pilot', version: 1 },
  { id: 2, plan_id: 2, name: 'Runbook', description: 'How to run it', milestone_id: null, milestone_name: null, version: 1 },
]
const LINES = [
  { id: 1, plan_id: 2, description: 'Team', category: 'people', amount: '10000.00', version: 1 },
  { id: 2, plan_id: 2, description: 'Licence', category: 'software', amount: '2000.00', version: 1 },
]

function page(items: unknown[]) {
  return { items, total: items.length, limit: 100, offset: 0 }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeApi(found: Plan | null) {
  return vi.fn(async (input: string) => {
    const path = new URL(input, 'http://localhost').pathname
    if (path === '/api/v1/projects/5/plan') {
      return found ? json(found) : json({ detail: 'this project has no plan' }, 404)
    }
    if (path === '/api/v1/projects/5/plan/milestones') return json(page(MILESTONES))
    if (path === '/api/v1/projects/5/plan/deliverables') return json(page(DELIVERABLES))
    if (path === '/api/v1/projects/5/plan/estimate-lines') return json(page(LINES))
    return json({ detail: `unexpected ${path}` }, 404)
  })
}

async function mountPlan(found: Plan | null = plan()) {
  vi.stubGlobal('fetch', fakeApi(found))
  const wrapper = mount(PlanSection, {
    props: { projectId: 5 },
    global: { plugins: [makeVuetify()] },
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

  it('shows the total the API computed against the budget range', async () => {
    await mountPlan()
    expect(findAll('plan-line')).toHaveLength(2)
    expect(find('plan-lines-total')?.textContent).toBe('12000.00')
    expect(find('plan-total')?.textContent).toBe('13200.00')
    expect(find('plan-budget-fit')?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'Within the budget range (10000.00 to 20000.00).',
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

  it('says when the project has no plan', async () => {
    await mountPlan(null)
    expect(find('no-plan')?.textContent).toContain('this project has no plan')
  })
})
