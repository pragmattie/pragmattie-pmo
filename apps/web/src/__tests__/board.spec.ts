import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  type Board,
  boardQuery,
  type Card,
  HIGHLIGHT_MS,
  moneyText,
  movedKeys,
  POLL_MS,
  risksText,
  states,
} from '../lib/board'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// The actions /auth/me sends for each role (the API's table in app/authz.py).
const ACTIONS: Record<string, string[]> = {
  admin: ['audit:read', 'orchestrator:run', 'project:read', 'standards:read', 'user:admin'],
  sponsor: [
    'gate:decide',
    'intake:decide',
    'project:read',
    'proposal:approve',
    'risk:accept',
    'standards:read',
  ],
  project_manager: ['project:read', 'project:write', 'risk:accept', 'standards:read'],
  ethics_reviewer: ['audit:read', 'gate:decide', 'project:read', 'standards:read'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

const COLUMNS: [string, string][] = [
  ['requests', 'Requests'],
  ['approval_gate', 'Approval gate'],
  ['planning', 'Planning'],
  ['go_ahead_gate', 'Go-ahead gate'],
  ['delivery', 'Delivery'],
  ['closure', 'Closure'],
  ['closed', 'Closed'],
]

function card(changes: Partial<Card> = {}): Card {
  return {
    key: 'project-2',
    kind: 'project',
    id: 2,
    column: 'go_ahead_gate',
    name: 'Warehouse forecasting',
    client_name: 'Harbour Foods',
    client_id: 5,
    project_manager: 'Pat Lee',
    health: 'at_risk',
    proposal_status: 'accepted',
    top_risk_band: 'High',
    open_risks: 3,
    plan_total: '110.00',
    top_budget: '150.50',
    stopped: false,
    held_by: 'person',
    simulated: false,
    waiting: [
      {
        step: 'pass_gate',
        subject_type: 'project',
        subject_id: 2,
        holder: 'person',
        who: 'sponsor',
        text: 'Waiting on the sponsor: go-ahead gate',
        due_at: null,
        simulated: false,
      },
    ],
    ...changes,
  }
}

const REQUEST = card({
  key: 'request-4',
  kind: 'request',
  id: 4,
  column: 'requests',
  name: 'Route planning pilot',
  client_name: null,
  client_id: null,
  project_manager: 'Robin',
  health: null,
  proposal_status: null,
  top_risk_band: null,
  open_risks: 0,
  plan_total: null,
  top_budget: '2500.00',
  held_by: 'agent',
  waiting: [
    {
      step: 'draft_value_case',
      subject_type: 'request',
      subject_id: 4,
      holder: 'agent',
      who: 'intake_agent',
      text: 'Agent drafting the value case',
      due_at: null,
      simulated: false,
    },
  ],
})

const SIMULATED = card({
  key: 'project-3',
  id: 3,
  column: 'delivery',
  name: 'Store layout',
  held_by: 'agent',
  simulated: true,
  waiting: [
    {
      step: 'client_answer',
      subject_type: 'proposal',
      subject_id: 8,
      holder: 'agent',
      who: 'simulated_client',
      text: 'Simulated client answering the proposal',
      due_at: null,
      simulated: true,
    },
  ],
})

function makeBoard(cards: Card[]): Board {
  const counts = Object.fromEntries(
    COLUMNS.map(([id]) => [id, cards.filter((c) => c.column === id).length]),
  )
  return {
    columns: COLUMNS.map(([id, title]) => ({
      id,
      title,
      cards: cards.filter((c) => c.column === id),
    })),
    summary: {
      counts,
      planning_value: '110.00',
      delivery_value: '2000.00',
      open_medium_high_risks: 2,
      decisions_waiting: 1,
      stopped: 0,
    },
    clients: [{ id: 5, name: 'Harbour Foods' }],
    project_managers: ['Pat Lee', 'Robin'],
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function signedInAs(role: string, boards: Board[] = [makeBoard([card(), REQUEST, SIMULATED])]) {
  const user = { id: 7, email: `${role}@pmo.example`, display_name: 'Test', role, actions: ACTIONS[role] }
  let reads = 0
  return vi.fn(async (path: string) => {
    if (path === '/api/v1/auth/me') return json(user)
    if (path === '/api/v1/health') return json({ status: 'ok', version: '0.1.0' })
    if (path === '/api/v1/inbox/count') return json({ count: 0 })
    if (path.startsWith('/api/v1/board')) {
      const found = boards[Math.min(reads, boards.length - 1)]
      reads += 1
      return json(found)
    }
    return json({ detail: 'not found' }, 404)
  })
}

async function mountAt(path: string) {
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
  return { wrapper, router }
}

function boardCalls(fetch: ReturnType<typeof signedInAs>): string[] {
  return fetch.mock.calls.map(([path]) => String(path)).filter((p) => p.startsWith('/api/v1/board'))
}

describe('the board page', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  it.each(Object.keys(ACTIONS))('shows the %s every column in order', async (role) => {
    vi.stubGlobal('fetch', signedInAs(role))
    const { wrapper } = await mountAt('/board')
    const columns = wrapper.findAll('[data-test^="column-"]')
    expect(columns.map((c) => c.attributes('data-test'))).toEqual(
      COLUMNS.map(([id]) => `column-${id}`),
    )
    expect(wrapper.find('[data-test="column-go_ahead_gate"]').text()).toContain('Go-ahead gate')
    expect(wrapper.find('[data-test="card-project-2"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a card with its fields and what it waits on', async () => {
    vi.stubGlobal('fetch', signedInAs('sponsor'))
    const { wrapper } = await mountAt('/board')
    const project = wrapper.find('[data-test="card-project-2"]')
    expect(project.find('[data-test="card-name"]').text()).toBe('Warehouse forecasting')
    expect(project.find('[data-test="card-client"]').text()).toBe('Harbour Foods')
    expect(project.find('[data-test="card-pm"]').text()).toContain('Pat Lee')
    expect(project.find('[data-test="card-health"]').text()).toBe('At risk')
    expect(project.find('[data-test="card-proposal"]').text()).toBe('Proposal: accepted')
    expect(project.find('[data-test="card-risks"]').text()).toContain('3 open risks, highest High')
    expect(project.find('[data-test="card-money"]').text()).toBe('Plan 110.00 of top budget 150.50')
    expect(project.find('[data-test="waiting-person"]').text()).toContain(
      'Waiting on the sponsor: go-ahead gate',
    )
    // Agent-held and person-held cards look different.
    expect(project.classes()).toContain('held-person')
    const request = wrapper.find('[data-test="card-request-4"]')
    expect(request.classes()).toContain('held-agent')
    expect(request.find('[data-test="waiting-agent"]').text()).toContain('Agent drafting the value case')
    expect(request.find('[data-test="card-health"]').exists()).toBe(false)
    // Anything simulated says so.
    const simulated = wrapper.find('[data-test="card-project-3"]')
    expect(simulated.find('[data-test="card-simulated"]').text()).toBe('Simulated')
    expect(simulated.find('[data-test="waiting-agent"]').text()).toContain('(simulated)')
    expect(project.find('[data-test="card-simulated"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('opens the request or project page from a card', async () => {
    vi.stubGlobal('fetch', signedInAs('auditor'))
    const { wrapper } = await mountAt('/board')
    expect(wrapper.find('[data-test="card-project-2"]').attributes('href')).toBe('/projects/2')
    expect(wrapper.find('[data-test="card-request-4"]').attributes('href')).toBe('/intake/4')
    wrapper.unmount()
  })

  it('shows the summary bar', async () => {
    vi.stubGlobal('fetch', signedInAs('sponsor'))
    const { wrapper } = await mountAt('/board')
    const summary = wrapper.find('[data-test="board-summary"]')
    expect(summary.find('[data-test="summary-count-requests"]').text()).toBe('1')
    expect(summary.find('[data-test="summary-count-go_ahead_gate"]').text()).toBe('1')
    expect(summary.find('[data-test="summary-count-closed"]').text()).toBe('0')
    expect(summary.find('[data-test="summary-planning-value"]').text()).toBe('110.00')
    expect(summary.find('[data-test="summary-delivery-value"]').text()).toBe('2000.00')
    expect(summary.find('[data-test="summary-risks"]').text()).toBe('2')
    expect(summary.find('[data-test="summary-decisions"]').text()).toBe('1')
    wrapper.unmount()
  })

  it('asks the API again with each filter', async () => {
    const fetch = signedInAs('sponsor')
    vi.stubGlobal('fetch', fetch)
    const { wrapper } = await mountAt('/board')
    const view = wrapper.findComponent({ name: 'BoardView' })
    const filters = (view.vm as unknown as { filters: Record<string, unknown> }).filters
    filters.client_id = 5
    await flushPromises()
    filters.project_manager = 'Pat Lee'
    await flushPromises()
    filters.waiting = 'person'
    await flushPromises()
    filters.show_stopped = true
    await flushPromises()
    expect(boardCalls(fetch)).toEqual([
      '/api/v1/board',
      '/api/v1/board?client_id=5',
      '/api/v1/board?client_id=5&project_manager=Pat+Lee',
      '/api/v1/board?client_id=5&project_manager=Pat+Lee&waiting=person',
      '/api/v1/board?client_id=5&project_manager=Pat+Lee&waiting=person&show_stopped=true',
    ])
    wrapper.unmount()
  })

  it('refreshes every 15 seconds and highlights a card that moved', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const moved = card({
      column: 'delivery',
      held_by: 'agent',
      waiting: [{ ...REQUEST.waiting[0], text: 'Agent looking for risks', step: 'propose_risks' }],
    })
    const fetch = signedInAs('sponsor', [makeBoard([card(), REQUEST]), makeBoard([moved, REQUEST])])
    vi.stubGlobal('fetch', fetch)
    const { wrapper } = await mountAt('/board')
    expect(boardCalls(fetch)).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(POLL_MS)
    await flushPromises()
    expect(boardCalls(fetch)).toHaveLength(2)
    const project = wrapper.find('[data-test="column-delivery"] [data-test="card-project-2"]')
    expect(project.classes()).toContain('moved')
    expect(wrapper.find('[data-test="card-request-4"]').classes()).not.toContain('moved')
    await vi.advanceTimersByTimeAsync(HIGHLIGHT_MS)
    await flushPromises()
    expect(wrapper.find('[data-test="card-project-2"]').classes()).not.toContain('moved')
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(POLL_MS)
    // Leaving the page stops the polling.
    expect(boardCalls(fetch)).toHaveLength(2)
  })
})

describe('the board helpers', () => {
  it('builds the filter query', () => {
    const none = { client_id: null, project_manager: null, waiting: null, show_stopped: false }
    expect(boardQuery(none)).toBe('')
    expect(boardQuery({ ...none, waiting: 'agent' })).toBe('?waiting=agent')
  })

  it('words money and risks', () => {
    expect(moneyText(card())).toBe('Plan 110.00 of top budget 150.50')
    expect(moneyText(card({ plan_total: null, top_budget: null }))).toBe(
      'No plan yet; no top budget',
    )
    expect(moneyText(REQUEST)).toBe('Top budget 2500.00')
    expect(risksText(card({ open_risks: 0, top_risk_band: null }))).toBe('No open risks')
    expect(risksText(card({ open_risks: 1, top_risk_band: 'Low' }))).toBe('1 open risk, highest Low')
  })

  it('finds the cards that changed column or waiting state, not new ones', () => {
    const before = states(makeBoard([card(), REQUEST]))
    const after = makeBoard([card({ column: 'delivery' }), REQUEST, SIMULATED])
    expect(movedKeys(before, after)).toEqual(['project-2'])
    const waitingChanged = makeBoard([card(), { ...REQUEST, held_by: 'person' }])
    expect(movedKeys(before, waitingChanged)).toEqual(['request-4'])
  })
})
