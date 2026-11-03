import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import { canDecideGate, type Gate, type GateCriterion, readyToPass } from '../lib/gates'
import type { ProjectRequest } from '../lib/intake'
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
  ethics_reviewer: ['audit:read', 'gate:decide', 'project:read', 'standards:read'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

const USER_ID = 7
const REASON = 'The forecast pays for itself within a year'

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
    uses_ai: false,
    known_risks: null,
    requested_by: 11,
    requested_by_name: 'Pat Manager',
    status: 'submitted',
    submitted_at: '2026-10-02T09:00:00',
    version: 2,
    created_at: '2026-10-01T09:00:00',
    updated_at: '2026-10-01T09:00:00',
    ...changes,
  }
}

function criterion(id: string, kind: 'check' | 'person', met: boolean | null): GateCriterion {
  return {
    id,
    text: `Criterion ${id}`,
    kind,
    check: kind === 'check' ? id : null,
    met,
    detail: kind === 'check' ? (met ? 'fine' : 'rated red: data') : null,
    note: null,
  }
}

function gate(changes: Partial<Gate> = {}): Gate {
  return {
    gate: {
      id: 'approval',
      name: 'Approval gate',
      from_stage: 'intake',
      to_stage: 'planning',
      applies_to: 'request',
      deciders: ['sponsor'],
    },
    request_id: 1,
    request_version: 2,
    request_status: 'submitted',
    requested_by: 11,
    criteria: [
      criterion('value_case_confirmed', 'check', true),
      criterion('readiness_not_red', 'check', true),
      criterion('value_justifies_cost', 'person', null),
      criterion('people_available', 'person', null),
    ],
    checks_met: true,
    policy_version: 'a'.repeat(64),
    latest_decision: null,
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
  gate?: Gate
  gateStatus?: number
  decideError?: { status: number; detail: unknown }
}

function fakeApi(role: string, options: FakeOptions = {}) {
  const user = { id: USER_ID, email: `${role}@pmo.example`, display_name: 'Test User', role }
  let stored = options.stored ?? request()
  let current = options.gate ?? gate()
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init.method ?? 'GET'
    if (path === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS[role] })
    if (path === '/api/v1/clients') return json({ items: [], total: 0, limit: 100, offset: 0 })
    if (path === '/api/v1/requests/1/history') {
      return json({ items: [], total: 0, limit: 50, offset: 0 })
    }
    if (path === '/api/v1/requests/1/value-case') return json({ detail: 'none' }, 404)
    if (path === '/api/v1/requests/1/gate' && method === 'POST') {
      if (options.decideError) return json({ detail: options.decideError.detail }, options.decideError.status)
      const body = JSON.parse(String(init.body))
      const status = { pass: 'approved', return: 'draft', decline: 'declined' }[
        body.outcome as 'pass' | 'return' | 'decline'
      ]
      stored = { ...stored, status, version: stored.version + 1 }
      current = {
        ...current,
        request_status: status,
        request_version: stored.version,
        latest_decision: {
          id: 1,
          gate_id: 'approval',
          outcome: body.outcome,
          decided_by: USER_ID,
          decided_by_name: 'Test User',
          reason: body.reason,
          criteria: current.criteria,
          policy_version: current.policy_version,
          created_at: '2026-10-03T09:00:00',
        },
      }
      return json(current)
    }
    if (path === '/api/v1/requests/1/gate') {
      if (options.gateStatus) {
        const detail = "the approval gate isn't available yet: a person needs to apply the gates policy file"
        return json({ detail }, options.gateStatus)
      }
      return json(current)
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

function decisions(fetchMock: Mounted['fetchMock']) {
  return fetchMock.mock.calls
    .filter(
      ([input, init]) =>
        init?.method === 'POST' &&
        new URL(String(input), 'http://localhost').pathname === '/api/v1/requests/1/gate',
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
    const input = box.querySelector('input') as HTMLInputElement
    input.click()
    await flushPromises()
  }
  for (const note of findAll('person-note')) await type(note, 'Checked with the team')
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the gate rules', () => {
  const sponsor = { id: USER_ID, role: 'sponsor' }

  it('lets a listed role decide a submitted request it did not raise', () => {
    expect(canDecideGate(gate(), sponsor, true)).toBe(true)
    expect(canDecideGate(gate(), sponsor, false)).toBe(false)
    expect(canDecideGate(gate(), { id: USER_ID, role: 'ethics_reviewer' }, true)).toBe(false)
    expect(canDecideGate(gate({ requested_by: USER_ID }), sponsor, true)).toBe(false)
    expect(canDecideGate(gate({ request_status: 'draft' }), sponsor, true)).toBe(false)
    expect(canDecideGate(gate(), null, true)).toBe(false)
  })

  it('is ready to pass only with every check met and every person criterion ticked with a note', () => {
    const ticks = [
      { id: 'value_justifies_cost', met: true, note: 'Yes' },
      { id: 'people_available', met: true, note: 'Yes' },
    ]
    expect(readyToPass(gate(), ticks)).toBe(true)
    expect(readyToPass(gate(), [ticks[0]])).toBe(false)
    expect(readyToPass(gate(), [ticks[0], { ...ticks[1], note: ' ' }])).toBe(false)
    const red = gate({ criteria: [criterion('readiness_not_red', 'check', false)] })
    expect(readyToPass(red, ticks)).toBe(false)
  })
})

describe('the Gate section', () => {
  it('shows each criterion with a tick or a cross', async () => {
    const criteria = [
      criterion('value_case_confirmed', 'check', true),
      criterion('readiness_not_red', 'check', false),
      criterion('value_justifies_cost', 'person', null),
    ]
    await mountAt('auditor', { gate: gate({ criteria, checks_met: false }) })
    expect(find('criterion-value_case_confirmed')?.textContent).toContain('Met')
    expect(find('criterion-readiness_not_red')?.textContent).toContain('Not met')
    expect(find('criterion-readiness_not_red')?.textContent).toContain('rated red: data')
    expect(find('criterion-value_justifies_cost')?.textContent).toContain(
      'the person deciding ticks this',
    )
    expect(find('gate-deciders')?.textContent).toContain('Decided by: sponsor')
  })

  it('gives a sponsor the tick boxes and Pass, Return and Decline', async () => {
    await mountAt('sponsor')
    expect(findAll('person-tick')).toHaveLength(2)
    expect(find('gate-pass')).not.toBeNull()
    expect(find('gate-return')).not.toBeNull()
    expect(find('gate-decline')).not.toBeNull()
    // Pass waits for every person criterion to be ticked with a note.
    expect((find('gate-pass') as HTMLButtonElement).disabled).toBe(true)
    expect(find('pass-hint')).not.toBeNull()
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

  it("gives a sponsor no buttons on a request they raised", async () => {
    await mountAt('sponsor', {
      stored: request({ requested_by: USER_ID }),
      gate: gate({ requested_by: USER_ID }),
    })
    expect(find('gate-actions')).toBeNull()
  })

  it('gives a sponsor no buttons once the request is decided', async () => {
    await mountAt('sponsor', {
      stored: request({ status: 'approved', version: 3 }),
      gate: gate({ request_status: 'approved', request_version: 3 }),
    })
    expect(find('gate-actions')).toBeNull()
  })

  it('passes with the ticks, notes and a reason', async () => {
    const { fetchMock } = await mountAt('sponsor')
    await tickAll()
    expect((find('gate-pass') as HTMLButtonElement).disabled).toBe(false)
    await click('gate-pass')
    expect(find('gate-dialog')).not.toBeNull()
    // The reason needs at least 20 characters.
    await type(find('gate-reason'), 'Too short')
    expect((find('gate-confirm') as HTMLButtonElement).disabled).toBe(true)
    await type(find('gate-reason'), REASON)
    await click('gate-confirm')
    expect(decisions(fetchMock)).toEqual([
      {
        version: 2,
        outcome: 'pass',
        reason: REASON,
        criteria: [
          { id: 'value_justifies_cost', met: true, note: 'Checked with the team' },
          { id: 'people_available', met: true, note: 'Checked with the team' },
        ],
      },
    ])
    expect(find('message')?.textContent).toContain('Request approved at the gate.')
    expect(find('gate-decision')?.textContent).toContain('Passed by Test User')
    expect(find('gate-actions')).toBeNull()
  })

  it('returns a request without the ticks', async () => {
    const { fetchMock } = await mountAt('sponsor')
    await click('gate-return')
    await type(find('gate-reason'), 'The data question needs an answer first')
    await click('gate-confirm')
    const [sent] = decisions(fetchMock)
    expect(sent.outcome).toBe('return')
    expect(sent.criteria.every((c: { met: boolean }) => !c.met)).toBe(true)
    expect(find('message')?.textContent).toContain('Request returned to draft.')
  })

  it('warns that a decline is final', async () => {
    await mountAt('sponsor')
    await click('gate-decline')
    expect(find('gate-dialog')?.textContent).toContain('A declined request is final')
  })

  it('lists the unmet criteria when the API refuses a pass', async () => {
    const detail = {
      message: "this request can't pass the approval gate until every criterion is met",
      unmet: [{ id: 'readiness_not_red', text: 'No area is rated red', detail: 'rated red: data' }],
    }
    await mountAt('sponsor', { decideError: { status: 409, detail } })
    await tickAll()
    await click('gate-pass')
    await type(find('gate-reason'), REASON)
    await click('gate-confirm')
    expect(find('message')?.textContent).toContain('until every criterion is met')
    expect(find('missing')?.textContent).toContain('No area is rated red: rated red: data')
  })

  it("shows the API's message while the gates policy isn't applied", async () => {
    await mountAt('sponsor', { gateStatus: 503 })
    expect(find('gate-error')?.textContent).toContain("the approval gate isn't available yet")
    expect(find('gate-actions')).toBeNull()
  })
})
