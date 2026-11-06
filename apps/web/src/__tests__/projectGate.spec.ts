import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  type Baseline,
  canDecideGate,
  type GateCriterion,
  outcomeChoices,
  type ProjectGate,
} from '../lib/gates'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

// The go-ahead gate on the project page (item 5.4): the Gate section for each role, its
// decisions, and the baseline once the gate is passed.

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
  ethics_reviewer: ['audit:read', 'gate:decide', 'project:read', 'standards:read'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

const USER_ID = 2
const PM_ID = 7
const REASON = 'The plan and its risks are ready to start'

const PROJECT = {
  id: 5,
  client_id: null,
  client_name: null,
  name: 'Warehouse demand forecast',
  stage: 'planning',
  status: 'on_track',
  sponsor: 'Sam Sponsor',
  sponsor_user_id: USER_ID,
  pm: 'Pat Manager',
  pm_user_id: PM_ID,
  request_id: null,
  created_at: '2026-11-03T09:00:00',
}

function criterion(id: string, kind: 'check' | 'person', met: boolean | null): GateCriterion {
  return {
    id,
    text: `Criterion ${id}`,
    kind,
    check: kind === 'check' ? id : null,
    met,
    detail: kind === 'check' ? (met ? 'fine' : 'not yet') : null,
    note: null,
  }
}

const BASELINE: Baseline = {
  id: 3,
  project_id: 5,
  number: 1,
  gate_decision_id: 1,
  plan: {
    scope_in: 'A weekly forecast',
    scope_out: null,
    milestones: [{ id: 1, name: 'Model ready', target_date: '2027-02-01', owner_user_id: PM_ID }],
    deliverables: [{ id: 1, name: 'Forecast model', milestone_name: 'Model ready' }],
    estimate_lines: [{ id: 1, description: 'Analysis', category: 'labour', amount: '10000.00' }],
    contingency_pct: 10,
    lines_total: '10000.00',
    contingency_amount: '1000.00',
    total: '11000.00',
  },
  total: '11000.00',
  created_at: '2026-11-05T09:00:00',
}

function gate(changes: Partial<ProjectGate> = {}): ProjectGate {
  return {
    gate: {
      id: 'go_ahead',
      name: 'Go-ahead gate',
      from_stage: 'planning',
      to_stage: 'delivery',
      applies_to: 'project',
      deciders: ['sponsor'],
    },
    project_id: 5,
    project_version: 4,
    project_stage: 'planning',
    project_status: 'on_track',
    pm_user_id: PM_ID,
    baseline: null,
    criteria: [
      criterion('plan_complete', 'check', true),
      criterion('estimate_within_budget', 'check', true),
      criterion('plan_realistic', 'person', null),
      criterion('risks_acceptable', 'person', null),
    ],
    checks_met: true,
    policy_version: 'a'.repeat(64),
    latest_decision: null,
    ...changes,
  }
}

function page(items: unknown[]) {
  return { items, total: items.length, limit: 100, offset: 0 }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface FakeOptions {
  gate?: ProjectGate
  userId?: number
  decideError?: { status: number; detail: unknown }
}

function fakeApi(role: string, options: FakeOptions = {}) {
  const user = {
    id: options.userId ?? USER_ID,
    email: `${role}@pmo.example`,
    display_name: 'Test User',
    role,
  }
  let project = { ...PROJECT }
  let current = options.gate ?? gate()
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init.method ?? 'GET'
    if (path === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS[role] })
    if (path === '/api/v1/projects/5/gate' && method === 'POST') {
      if (options.decideError) {
        return json({ detail: options.decideError.detail }, options.decideError.status)
      }
      const body = JSON.parse(String(init.body))
      if (body.outcome === 'pass') project = { ...project, stage: 'delivery' }
      if (body.outcome === 'decline') project = { ...project, status: 'cancelled' }
      current = {
        ...current,
        project_version: current.project_version + 1,
        project_stage: project.stage,
        project_status: project.status,
        baseline: body.outcome === 'pass' ? BASELINE : null,
        latest_decision: {
          id: 1,
          gate_id: 'go_ahead',
          outcome: body.outcome,
          decided_by: user.id,
          decided_by_name: 'Test User',
          reason: body.reason,
          criteria: current.criteria,
          policy_version: current.policy_version,
          created_at: '2026-11-05T09:00:00',
        },
      }
      return json(current)
    }
    if (path === '/api/v1/projects/5/gate') return json(current)
    if (path === '/api/v1/projects/5') return json(project)
    if (path === '/api/v1/projects/5/brief') return json({ detail: 'no brief' }, 404)
    if (path === '/api/v1/projects/5/stakeholders') return json(page([]))
    if (path === '/api/v1/projects/5/assumptions') return json(page([]))
    return json({ detail: `unexpected ${method} ${path}` }, 404)
  })
}

async function mountAt(role: string, options: FakeOptions = {}) {
  const fetchMock = fakeApi(role, options)
  vi.stubGlobal('fetch', fetchMock)
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter(true)
  router.push('/projects/5')
  await router.isReady()
  mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { fetchMock }
}

function decisions(fetchMock: Awaited<ReturnType<typeof mountAt>>['fetchMock']) {
  return fetchMock.mock.calls
    .filter(
      ([input, init]) =>
        init?.method === 'POST' &&
        new URL(String(input), 'http://localhost').pathname === '/api/v1/projects/5/gate',
    )
    .map(([, init]) => JSON.parse(String(init?.body)))
}

function find(testId: string) {
  return document.body.querySelector(`[data-test="${testId}"]`)
}

function findAll(testId: string) {
  return Array.from(document.body.querySelectorAll(`[data-test="${testId}"]`))
}

async function click(testId: string) {
  ;(find(testId) as HTMLElement).click()
  await flushPromises()
}

async function type(element: Element | null, value: string) {
  const input = element?.querySelector('input, textarea') as HTMLInputElement
  input.value = value
  input.dispatchEvent(new Event('input'))
  await flushPromises()
}

async function tickAll() {
  for (const box of findAll('person-tick')) {
    ;(box.querySelector('input') as HTMLInputElement).click()
    await flushPromises()
  }
  for (const note of findAll('person-note')) await type(note, 'Checked with the team')
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the go-ahead gate rules', () => {
  const sponsor = { id: USER_ID, role: 'sponsor' }

  it('lets a listed role decide a project in planning that it does not manage', () => {
    expect(canDecideGate(gate(), sponsor, true)).toBe(true)
    expect(canDecideGate(gate(), sponsor, false)).toBe(false)
    expect(canDecideGate(gate(), { id: USER_ID, role: 'ethics_reviewer' }, true)).toBe(false)
    expect(canDecideGate(gate({ pm_user_id: USER_ID }), sponsor, true)).toBe(false)
    expect(canDecideGate(gate({ project_stage: 'delivery' }), sponsor, true)).toBe(false)
    expect(canDecideGate(gate({ project_status: 'cancelled' }), sponsor, true)).toBe(false)
  })

  it("explains what each decision does to a project", () => {
    const [pass, , decline] = outcomeChoices('project')
    expect(pass.explain).toContain('freezes its plan as the baseline')
    expect(decline.explain).toContain('cancelled')
  })
})

describe('the Gate section on the project page', () => {
  it('shows the go-ahead gate and each criterion', async () => {
    await mountAt('auditor', {
      gate: gate({ criteria: [criterion('plan_complete', 'check', false)], checks_met: false }),
    })
    expect(find('gate')?.textContent).toContain('Go-ahead gate')
    expect(find('gate-deciders')?.textContent).toContain('From planning to delivery')
    expect(find('criterion-plan_complete')?.textContent).toContain('Not met')
    expect(find('baseline')).toBeNull()
  })

  it('gives a sponsor the tick boxes and Pass, Return and Decline', async () => {
    await mountAt('sponsor')
    expect(findAll('person-tick')).toHaveLength(2)
    for (const id of ['gate-pass', 'gate-return', 'gate-decline']) expect(find(id)).not.toBeNull()
    expect((find('gate-pass') as HTMLButtonElement).disabled).toBe(true)
  })

  it.each(['project_manager', 'auditor', 'admin', 'ethics_reviewer'])(
    'gives a %s no decision buttons and no tick boxes',
    async (role) => {
      await mountAt(role)
      expect(find('gate-criteria')).not.toBeNull()
      expect(find('gate-actions')).toBeNull()
      expect(findAll('person-tick')).toHaveLength(0)
    },
  )

  it("gives a sponsor no buttons on a project they manage", async () => {
    await mountAt('sponsor', { gate: gate({ pm_user_id: USER_ID }) })
    expect(find('gate-actions')).toBeNull()
  })

  it('passes without naming a project manager, then shows baseline 1', async () => {
    const { fetchMock } = await mountAt('sponsor')
    await tickAll()
    await click('gate-pass')
    expect(find('gate-project-manager')).toBeNull()
    await type(find('gate-reason'), REASON)
    expect((find('gate-confirm') as HTMLButtonElement).disabled).toBe(false)
    await click('gate-confirm')
    expect(decisions(fetchMock)).toEqual([
      {
        version: 4,
        outcome: 'pass',
        reason: REASON,
        criteria: [
          { id: 'plan_realistic', met: true, note: 'Checked with the team' },
          { id: 'risks_acceptable', met: true, note: 'Checked with the team' },
        ],
      },
    ])
    expect(find('message')?.textContent).toContain('it is in delivery')
    expect(find('gate-decision')?.textContent).toContain('Passed by Test User')
    expect(find('gate-actions')).toBeNull()
    expect(find('baseline')?.textContent).toContain('Baseline 1')
    expect(find('baseline-milestones')?.textContent).toContain('Model ready, 2027-02-01')
    expect(find('baseline-deliverables')?.textContent).toContain('Forecast model (Model ready)')
    expect(find('baseline-lines')?.textContent).toContain('Analysis')
    expect(find('baseline-total')?.textContent).toContain('11,000.00')
  })

  it('declines with the reason shown and no more buttons', async () => {
    await mountAt('sponsor')
    await click('gate-decline')
    expect(find('gate-dialog')?.textContent).toContain('A declined project is cancelled')
    await type(find('gate-reason'), 'The client has withdrawn the budget')
    await click('gate-confirm')
    expect(find('message')?.textContent).toContain('Project declined and cancelled.')
    expect(find('gate-decision')?.textContent).toContain('Declined by Test User')
    expect(find('gate-decision')?.textContent).toContain('The client has withdrawn the budget')
    expect(find('gate-actions')).toBeNull()
    expect(find('baseline')).toBeNull()
  })

  it('lists the unmet criteria when the API refuses a pass', async () => {
    const detail = {
      message: "this project can't pass the go-ahead gate until every criterion is met",
      unmet: [
        {
          id: 'estimate_within_budget',
          text: 'The estimate is within budget',
          detail: 'The brief has no top budget',
        },
      ],
    }
    await mountAt('sponsor', { decideError: { status: 409, detail } })
    await tickAll()
    await click('gate-pass')
    await type(find('gate-reason'), REASON)
    await click('gate-confirm')
    expect(find('message')?.textContent).toContain('until every criterion is met')
    expect(find('missing')?.textContent).toContain('The brief has no top budget')
  })

  it('shows the baseline of a project already in delivery', async () => {
    await mountAt('project_manager', {
      userId: PM_ID,
      gate: gate({ project_stage: 'delivery', baseline: BASELINE }),
    })
    expect(find('baseline')?.textContent).toContain('Baseline 1')
    expect(find('gate-actions')).toBeNull()
  })
})
