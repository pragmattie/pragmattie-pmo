import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import InboxDecision from '../components/InboxDecision.vue'
import type { ChangeLog, ChangeRequest } from '../lib/changes'
import type { Gate } from '../lib/gates'
import type { InboxEntry } from '../lib/inbox'
import { REQUEST_CONFLICT } from '../lib/intake'
import type { Proposal } from '../lib/proposals'
import { CONFLICT_MESSAGE, type Risk } from '../lib/risks'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'
import { useAuthStore } from '../stores/auth'

// A decision made from the inbox goes through the existing route, and a refusal (403 or 409)
// shows inside the inbox with the same message the request, risk register and project pages show.

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('visualViewport', new EventTarget())

// The actions /auth/me sends for each role (the API's table in app/authz.py).
const ACTIONS: Record<string, string[]> = {
  sponsor: ['gate:decide', 'intake:decide', 'project:read', 'proposal:approve', 'risk:accept'],
  project_manager: ['project:read', 'project:write', 'report:publish', 'risk:accept'],
}

const USER_ID = 7
const REASON = 'Checked with the team in the weekly review'

function entry(changes: Partial<InboxEntry>): InboxEntry {
  return {
    kind: 'item',
    id: 1,
    step: 'pass_gate',
    subject_type: 'request',
    subject_id: 1,
    subject_title: 'Warehouse forecasting pilot',
    project_id: null,
    project_name: null,
    who: 'sponsor',
    named_user_id: null,
    named_user_name: null,
    reason: 'the request is waiting at the approval gate',
    handoff: null,
    created_at: '2026-11-07T09:00:00',
    action_id: null,
    due_at: null,
    facts: [],
    recommendation: 'Review and decide.',
    ...changes,
  }
}

const GATE: Gate = {
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
  criteria: [],
  checks_met: true,
  policy_version: 'a'.repeat(64),
  latest_decision: null,
}

const PROPOSED: Risk = {
  id: 6,
  project_id: 3,
  kind: 'threat',
  title: 'Supplier data arrives late',
  cause: 'A cause',
  effect: 'An effect',
  category: 'project',
  objectives: ['schedule'],
  probability: 1,
  impact: 2,
  score: 2,
  band: 'Low',
  response: 'reduce',
  response_plan: 'A plan',
  owner_user_id: null,
  status: 'proposed',
  needs_judgement: false,
  show_in_proposal: false,
  source: 'agent',
  escalated_to: null,
  escalated_band: null,
  review_date: null,
  version: 1,
  created_at: '2026-11-01T09:00:00',
  updated_at: '2026-11-01T09:00:00',
}

const HEATMAP = {
  kind: null,
  cells: [],
  bands: [
    { band: 'Low', count: 0, owner_role: 'project_manager' },
    { band: 'Medium', count: 0, owner_role: 'sponsor' },
    { band: 'High', count: 0, owner_role: 'sponsor' },
  ],
  unscored: 0,
}

const CHANGE: ChangeRequest = {
  id: 1,
  project_id: 5,
  title: 'More analysis',
  reason: 'The client asked for it',
  changes: { add_lines: [{ description: 'More', category: 'people', amount: '50.00' }] },
  effect: {
    baseline_number: 1,
    baseline_total: '1000.00',
    new_total: '1050.00',
    cost_difference: '50.00',
    cost_difference_pct: '5.00',
    max_later_working_days: 0,
    max_earlier_working_days: 0,
    scope_changes: false,
    scope_changed: [],
    escalations: [],
    limits: { max_cost_increase_pct: '5.00', max_date_move_working_days: 5 },
  },
  decider: 'project_manager',
  measured_baseline_number: 1,
  status: 'submitted',
  raised_by: 8,
  raised_by_name: 'Raiser',
  submitted_at: '2026-11-05T09:00:00',
  decided_by: null,
  decided_by_name: null,
  decision_reason: null,
  decided_at: null,
  baseline_number: null,
  version: 2,
  created_at: '2026-11-05T09:00:00',
}

const CHANGE_LOG: ChangeLog = {
  items: [CHANGE],
  total: 1,
  limit: 100,
  offset: 0,
  project_stage: 'delivery',
  project_status: 'on_track',
  pm_user_id: USER_ID,
  sponsor_deciders: ['sponsor'],
  current_baseline_number: 1,
  proposal_out_of_date: false,
}

// A submitted proposal for project 5: its latest copy's contributors are set per test.
function submittedProposal(contributors: number[]): Proposal {
  const nobody = { source: null, reviewed_by: null, reviewed_at: null, unreviewed: false }
  return {
    id: 1,
    project_id: 5,
    project_name: 'Pilot',
    client_name: 'Northwind Example',
    client_simulated: true,
    status: 'submitted',
    summary: 'A pilot',
    scope_in: 'Two warehouses',
    scope_out: 'Stores',
    assumptions: 'Data arrives on time',
    terms: 'Monthly invoices',
    price: '1650.00',
    estimate_lines: [],
    milestones: [],
    deliverables: [],
    risks: [],
    plan_version_seen: 1,
    plan_changed: false,
    flags: [],
    sections: {
      summary: nobody,
      scope_in: nobody,
      scope_out: nobody,
      assumptions: nobody,
      terms: nobody,
    },
    unreviewed_sections: [],
    latest_version: 1,
    submitted_by: null,
    submitted_by_name: null,
    contributors,
    contributor_names: contributors.map((id) => `User ${id}`),
    approved_version: null,
    approval_current: false,
    version: 3,
    updated_at: '2026-11-07T09:00:00',
  }
}

let proposalShown: Proposal = submittedProposal([])

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

// The API: each GET answers with the subject, and the decision route refuses with `refusal`.
function fakeApi(decision: string, refusal: { status: number; detail: string }) {
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init.method ?? 'GET'
    if (method === 'POST' && path === decision) return json({ detail: refusal.detail }, refusal.status)
    if (path === '/api/v1/requests/1/gate') return json(GATE)
    if (path === '/api/v1/projects/3/risks/6') return json(PROPOSED)
    if (path === '/api/v1/projects/3/risks/heatmap') return json(HEATMAP)
    if (path === '/api/v1/projects/3/risks/6/history') {
      return json({ items: [], total: 0, limit: 50, offset: 0 })
    }
    if (path === '/api/v1/projects/5/gate') return json({ baseline: null })
    if (path === '/api/v1/projects/5') return json({ id: 5, client_name: 'Northwind Example' })
    if (path === '/api/v1/projects/5/proposal') return json(proposalShown)
    if (path === '/api/v1/projects/5/proposal/versions') {
      return json({ items: [], total: 0, limit: 100, offset: 0 })
    }
    if (path === '/api/v1/projects/5/changes') return json(CHANGE_LOG)
    return json({ detail: `unexpected ${method} ${path}` }, 404)
  })
}

async function mountDecision(
  role: string,
  item: InboxEntry,
  decision: string,
  refusal: { status: number; detail: string },
) {
  const fetchMock = fakeApi(decision, refusal)
  vi.stubGlobal('fetch', fetchMock)
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = {
    id: USER_ID,
    email: `${role}@pmo.example`,
    display_name: 'Test User',
    role,
    actions: ACTIONS[role],
  }
  auth.checked = true
  const wrapper = mount(InboxDecision, {
    props: { entry: item },
    global: { plugins: [pinia, makeRouter(true), makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { wrapper, fetchMock }
}

function find(testId: string) {
  return document.body.querySelector(`[data-test="${testId}"]`)
}

async function click(testId: string) {
  ;(find(testId) as HTMLElement).click()
  await flushPromises()
}

async function type(testId: string, value: string) {
  const field = find(testId)?.querySelector('input, textarea') as HTMLInputElement
  field.value = value
  field.dispatchEvent(new Event('input'))
  await flushPromises()
}

function posted(fetchMock: Awaited<ReturnType<typeof mountDecision>>['fetchMock']) {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([input]) => new URL(String(input), 'http://localhost').pathname)
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('a refusal shows inside the inbox as it does elsewhere', () => {
  // The request page shows its own wording for a stale version, and the API's message otherwise.
  it.each([
    [403, 'you raised this request, so another person must decide it', null],
    [409, 'this request has changed since you read it', REQUEST_CONFLICT],
  ])('a gate decision refused with %i', async (status, detail, shown) => {
    const route = '/api/v1/requests/1/gate'
    const { wrapper, fetchMock } = await mountDecision('sponsor', entry({}), route, {
      status,
      detail,
    })
    await click('gate-return')
    await type('gate-reason', REASON)
    await click('gate-confirm')
    expect(posted(fetchMock)).toEqual([route])
    expect(find('inbox-conflict')?.textContent?.trim()).toBe(shown ?? detail)
    expect(wrapper.emitted('decided')).toBeUndefined()
  })

  // The risk register shows the API's message for a 403 and its conflict message for a 409.
  it.each([
    [403, 'a project manager may not open a risk in this band', null],
    [409, 'this risk has changed since you read it', CONFLICT_MESSAGE],
  ])('a risk decision refused with %i', async (status, detail, shown) => {
    const route = '/api/v1/projects/3/risks/6/open'
    const item = entry({
      step: 'open_low_risk',
      subject_type: 'risk',
      subject_id: 6,
      project_id: 3,
      project_name: 'Pilot',
      who: 'project_manager',
    })
    const { wrapper, fetchMock } = await mountDecision('project_manager', item, route, {
      status,
      detail,
    })
    await click('open-risk')
    expect(posted(fetchMock)).toEqual([route])
    expect(find('drawer-message')?.textContent?.trim()).toBe(shown ?? detail)
    expect(wrapper.emitted('decided')).toBeUndefined()
  })

  // The project page's change requests show the API's message for both.
  it.each([
    [403, 'only the project manager decides this change'],
    [409, 'a newer baseline has been made since this change was measured'],
  ])('a change decision refused with %i', async (status, detail) => {
    const route = '/api/v1/projects/5/changes/1/decide'
    const item = entry({
      step: 'decide_change_pm',
      subject_type: 'change_request',
      subject_id: 1,
      project_id: 5,
      project_name: 'Pilot',
      who: 'project_manager',
    })
    const { wrapper, fetchMock } = await mountDecision('project_manager', item, route, {
      status,
      detail,
    })
    await click('change-approve')
    await type('change-decision-reason', REASON)
    await click('change-confirm')
    expect(posted(fetchMock)).toEqual([route])
    expect(find('changes-message')?.textContent?.trim()).toBe(detail)
    expect(wrapper.emitted('decided')).toBeUndefined()
  })
})

describe('a proposal decision in the inbox', () => {
  const item = () =>
    entry({
      step: 'approve_proposal',
      subject_type: 'proposal',
      subject_id: 1,
      project_id: 5,
      project_name: 'Pilot',
      handoff: 'the named sponsor, User 11, contributed to this copy',
    })
  const route = '/api/v1/projects/5/proposal/approve'
  const refusal = { status: 403, detail: 'not expected' }

  it('offers Approve and Return to a sponsor who did not contribute', async () => {
    proposalShown = submittedProposal([11])
    await mountDecision('sponsor', item(), route, refusal)
    expect(find('proposal-approve')).not.toBeNull()
    expect(find('proposal-return')).not.toBeNull()
    expect(find('proposal-contributors')?.textContent).toContain('User 11')
  })

  it('offers neither to a sponsor who reviewed a section of the copy', async () => {
    proposalShown = submittedProposal([11, USER_ID])
    const { fetchMock } = await mountDecision('sponsor', item(), route, refusal)
    expect(find('proposal-approve')).toBeNull()
    expect(find('proposal-return')).toBeNull()
    expect(find('proposal-contributed')?.textContent).toContain('You contributed to this copy')
    expect(posted(fetchMock)).toEqual([])
  })
})
