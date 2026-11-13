import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DeliverySection from '../components/DeliverySection.vue'
import GateSection from '../components/GateSection.vue'
import {
  type AcceptanceRound,
  canAnswer,
  canOffer,
  type DeliverableProgress,
  type Delivery,
  roundText,
} from '../lib/delivery'
import { canDecideGate, type ProjectGate } from '../lib/gates'
import { gateSubjectFor } from '../lib/inbox'
import { makeVuetify } from '../plugins/vuetify'
import { useAuthStore } from '../stores/auth'

// Deliverable acceptance and the acceptance gate (item 7.6).

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
const USER_ID = 7

function offerRound(changes: Partial<AcceptanceRound> = {}): AcceptanceRound {
  return {
    id: 1,
    round: 1,
    kind: 'offer',
    offered_to: 'Dana Reyes',
    offered_on: '2027-05-20',
    means: 'email',
    answer: null,
    answered_on: null,
    missing: null,
    note: null,
    source: 'person',
    recorded_by: USER_ID,
    recorded_by_name: 'Pat Manager',
    created_at: '2027-05-20T09:00:00',
    ...changes,
  }
}

function answerRound(changes: Partial<AcceptanceRound> = {}): AcceptanceRound {
  return offerRound({
    id: 2,
    kind: 'answer',
    offered_to: null,
    offered_on: null,
    means: null,
    answer: 'not_accepted',
    answered_on: '2027-05-25',
    missing: 'The accuracy report',
    ...changes,
  })
}

function deliverable(changes: Partial<DeliverableProgress> = {}): DeliverableProgress {
  return {
    id: 21,
    name: 'Model',
    milestone_id: 11,
    status: 'ready_for_acceptance',
    version: 3,
    acceptance: [],
    awaiting_answer: false,
    ...changes,
  }
}

function delivery(changes: Partial<Delivery> = {}): Delivery {
  return {
    project_id: 5,
    project_stage: 'delivery',
    project_status: 'on_track',
    has_client: true,
    client_simulated: false,
    pm_user_id: 99,
    baseline_number: 1,
    baseline_total: '1000.00',
    today: '2027-06-01',
    milestones: [],
    deliverables: [deliverable()],
    current_spend: null,
    spend: { items: [], total: 0, limit: 50, offset: 0 },
    ...changes,
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface Sent {
  method: string
  path: string
  body: unknown
}

function signIn(role: string) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = {
    id: USER_ID,
    email: `${role}@pmo.example`,
    display_name: 'Test User',
    role,
    actions: ACTIONS[role],
  }
  return pinia
}

async function mountDelivery(role: string, found: Delivery = delivery(), sent: Sent[] = []) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const path = new URL(input, 'http://localhost').pathname
      const method = init.method ?? 'GET'
      if (method !== 'GET') sent.push({ method, path, body: JSON.parse(String(init.body)) })
      return json(found, method === 'POST' ? 201 : 200)
    }),
  )
  const pinia = signIn(role)
  const wrapper = mount(DeliverySection, {
    props: { projectId: 5 },
    global: { plugins: [pinia, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function find(testId: string) {
  return document.body.querySelector(`[data-test="${testId}"]`)
}

function findAll(testId: string) {
  return [...document.body.querySelectorAll(`[data-test="${testId}"]`)]
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

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the acceptance rules', () => {
  const pm = { id: USER_ID, role: 'project_manager' }
  const sponsor = { id: USER_ID, role: 'sponsor' }
  const holds = (role: string) => (action: string) => ACTIONS[role].includes(action)

  it('offers only a ready deliverable whose last offer has its answer', () => {
    expect(canOffer(delivery(), deliverable(), true)).toBe(true)
    expect(canOffer(delivery(), deliverable(), false)).toBe(false)
    expect(canOffer(delivery(), deliverable({ status: 'in_progress' }), true)).toBe(false)
    expect(canOffer(delivery(), deliverable({ awaiting_answer: true }), true)).toBe(false)
    expect(canOffer(delivery({ project_status: 'cancelled' }), deliverable(), true)).toBe(false)
  })

  it('records an answer only on a waiting offer, by whoever answers for the project', () => {
    const waiting = deliverable({ awaiting_answer: true })
    expect(canAnswer(delivery(), deliverable(), pm, holds('project_manager'))).toBe(false)
    expect(canAnswer(delivery(), waiting, pm, holds('project_manager'))).toBe(true)
    expect(canAnswer(delivery(), waiting, sponsor, holds('sponsor'))).toBe(false)
    const noClient = delivery({ has_client: false })
    expect(canAnswer(noClient, waiting, pm, holds('project_manager'))).toBe(false)
    expect(canAnswer(noClient, waiting, sponsor, holds('sponsor'))).toBe(true)
    // Nobody accepts the deliverables of a project they manage.
    expect(canAnswer({ ...noClient, pm_user_id: USER_ID }, waiting, sponsor, holds('sponsor'))).toBe(
      false,
    )
    const ethics = { id: USER_ID, role: 'ethics_reviewer' }
    expect(canAnswer(noClient, waiting, ethics, holds('ethics_reviewer'))).toBe(false)
  })

  it('describes each round, and names the simulated client', () => {
    expect(roundText(offerRound())).toBe('Round 1: offered to Dana Reyes on 2027-05-20 by email')
    expect(roundText(answerRound())).toBe(
      'Round 1: Not accepted on 2027-05-25 (recorded by Pat Manager); missing: The accuracy report',
    )
    expect(
      roundText(answerRound({ answer: 'accepted', missing: null, source: 'simulated', recorded_by: null })),
    ).toBe('Round 1: Accepted on 2027-05-25 (recorded by Simulated client)')
  })

  it('opens the acceptance gate from a pass_gate item that names it', () => {
    expect(gateSubjectFor({ subject_type: 'project', gate_id: 'acceptance' })).toBe('acceptance')
    expect(gateSubjectFor({ subject_type: 'project', gate_id: 'go_ahead' })).toBe('project')
    expect(gateSubjectFor({ subject_type: 'request', gate_id: 'approval' })).toBe('request')
  })
})

describe('the acceptance controls', () => {
  it('lets the project manager offer a ready deliverable', async () => {
    const sent: Sent[] = []
    await mountDelivery('project_manager', delivery(), sent)
    expect(find('deliverable-answer')).toBeNull()
    await click('deliverable-offer')
    expect((find('offer-save') as HTMLButtonElement).disabled).toBe(true)
    await type('offer-to', 'Dana Reyes, Harbour')
    await type('offer-means', 'email')
    await type('offer-note', 'With the handover pack')
    await click('offer-save')
    expect(sent).toEqual([
      {
        method: 'POST',
        path: '/api/v1/projects/5/delivery/deliverables/21/offer',
        body: {
          offered_to: 'Dana Reyes, Harbour',
          offered_on: '2027-06-01',
          means: 'email',
          note: 'With the handover pack',
        },
      },
    ])
    expect(find('offer-form')).toBeNull()
    expect(find('delivery-message')?.textContent).toContain('Nothing was sent from here')
  })

  it('records a not accepted answer with what is missing', async () => {
    const sent: Sent[] = []
    const waiting = delivery({
      deliverables: [deliverable({ awaiting_answer: true, acceptance: [offerRound()] })],
    })
    await mountDelivery('project_manager', waiting, sent)
    expect(find('deliverable-offer')).toBeNull()
    expect(find('acceptance-waiting')).not.toBeNull()
    await click('deliverable-answer')
    ;(find('answer-not_accepted')?.querySelector('input') as HTMLInputElement).click()
    await flushPromises()
    expect((find('answer-save') as HTMLButtonElement).disabled).toBe(true)
    await type('answer-missing', 'The accuracy report')
    await click('answer-save')
    expect(sent).toEqual([
      {
        method: 'POST',
        path: '/api/v1/projects/5/delivery/deliverables/21/answer',
        body: {
          answer: 'not_accepted',
          answered_on: '2027-06-01',
          missing: 'The accuracy report',
        },
      },
    ])
  })

  it('records an accepted answer without anything missing', async () => {
    const sent: Sent[] = []
    const waiting = delivery({
      deliverables: [deliverable({ awaiting_answer: true, acceptance: [offerRound()] })],
    })
    await mountDelivery('project_manager', waiting, sent)
    await click('deliverable-answer')
    expect(find('answer-missing')).toBeNull()
    await click('answer-save')
    expect(sent[0].body).toEqual({ answer: 'accepted', answered_on: '2027-06-01' })
  })

  it('shows every round, labelling the simulated client', async () => {
    const history = [
      offerRound(),
      answerRound(),
      offerRound({ id: 3, round: 2 }),
      answerRound({
        id: 4,
        round: 2,
        answer: 'accepted',
        missing: null,
        source: 'simulated',
        recorded_by: null,
        recorded_by_name: null,
      }),
    ]
    await mountDelivery(
      'auditor',
      delivery({
        client_simulated: true,
        deliverables: [deliverable({ status: 'accepted', acceptance: history })],
      }),
    )
    const rounds = findAll('acceptance-round').map((r) => r.textContent ?? '')
    expect(rounds).toHaveLength(4)
    expect(rounds[1]).toContain('missing: The accuracy report')
    expect(rounds[3]).toContain('Simulated client')
    expect(findAll('acceptance-simulated')).toHaveLength(1)
    expect(find('acceptance-answerer')?.textContent).toContain('simulated client')
    expect(find('deliverable-offer')).toBeNull()
    expect(find('deliverable-answer')).toBeNull()
  })

  it('gives the sponsor Record the answer on a project with no client, and nobody else', async () => {
    const waiting = delivery({
      has_client: false,
      deliverables: [deliverable({ awaiting_answer: true, acceptance: [offerRound()] })],
    })
    await mountDelivery('sponsor', waiting)
    expect(find('deliverable-answer')).not.toBeNull()
    expect(find('acceptance-answerer')?.textContent).toContain('the sponsor accepts')
    document.body.innerHTML = ''
    await mountDelivery('project_manager', waiting)
    expect(find('deliverable-answer')).toBeNull()
  })

  it.each(['sponsor', 'auditor', 'admin', 'ethics_reviewer'])(
    '%s gets no acceptance controls on a client project',
    async (role) => {
      const waiting = delivery({
        deliverables: [deliverable({ awaiting_answer: true, acceptance: [offerRound()] })],
      })
      await mountDelivery(role, waiting)
      expect(find('deliverable-offer')).toBeNull()
      expect(find('deliverable-answer')).toBeNull()
    },
  )
})

function acceptanceGate(changes: Partial<ProjectGate> = {}): ProjectGate {
  return {
    gate: {
      id: 'acceptance',
      name: 'Acceptance gate',
      from_stage: 'delivery',
      to_stage: 'closure',
      applies_to: 'project',
      deciders: ['sponsor'],
    },
    criteria: [
      {
        id: 'deliverables_accepted',
        text: 'Every deliverable in the current baseline is accepted',
        kind: 'check',
        check: 'deliverables_accepted',
        met: false,
        detail: "not accepted yet: 'Model' (ready for acceptance)",
        note: null,
      },
      {
        id: 'scope_met',
        text: 'The work meets the scope in the baseline',
        kind: 'person',
        check: null,
        met: null,
        detail: null,
        note: null,
      },
    ],
    checks_met: false,
    policy_version: 'a'.repeat(64),
    latest_decision: null,
    project_id: 5,
    project_version: 4,
    project_stage: 'delivery',
    project_status: 'on_track',
    pm_user_id: 99,
    baseline: null,
    ...changes,
  }
}

describe('the acceptance gate', () => {
  it('may be decided by a listed role while the project is in delivery, never its own', () => {
    const sponsor = { id: USER_ID, role: 'sponsor' }
    expect(canDecideGate(acceptanceGate(), sponsor, true)).toBe(true)
    expect(canDecideGate(acceptanceGate({ project_stage: 'closure' }), sponsor, true)).toBe(false)
    expect(canDecideGate(acceptanceGate({ pm_user_id: USER_ID }), sponsor, true)).toBe(false)
    expect(canDecideGate(acceptanceGate(), { id: 1, role: 'project_manager' }, false)).toBe(false)
  })

  it('reads its own route and shows its criteria like the other gates', async () => {
    const paths: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        paths.push(new URL(input, 'http://localhost').pathname)
        return json(acceptanceGate())
      }),
    )
    const pinia = signIn('sponsor')
    mount(GateSection, {
      props: { subject: 'acceptance', subjectId: 5 },
      global: { plugins: [pinia, makeVuetify()] },
      attachTo: document.body,
    })
    await flushPromises()
    expect(paths).toEqual(['/api/v1/projects/5/acceptance-gate'])
    expect(find('acceptance-gate')?.textContent).toContain('Acceptance gate')
    expect(find('gate-deciders')?.textContent).toContain('From delivery to closure')
    expect(find('criterion-deliverables_accepted')?.textContent).toContain("'Model'")
    expect((find('gate-pass') as HTMLButtonElement).disabled).toBe(true)
    expect(find('gate-return')).not.toBeNull()
  })
})
