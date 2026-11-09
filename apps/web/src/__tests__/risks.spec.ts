import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  AGENT_PROPOSAL,
  bandColor,
  canAcceptRisk,
  canCloseRisk,
  canEditRisk,
  changedFields,
  CONFLICT_MESSAGE,
  escalationLine,
  type HeatCell,
  heatGrid,
  listQuery,
  mayAcceptBand,
  mayClearEscalation,
  NEEDS_JUDGEMENT,
  NO_FILTERS,
  type Risk,
  type RiskSuggestion,
  scoreText,
  SUGGESTED_BY_AGENT,
  suggestionMessage,
  agentSuggestion,
  toggleCell,
  withSuggestedScore,
} from '../lib/risks'
import { bandColors, makeVuetify } from '../plugins/vuetify'
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
  sponsor: ['gate:decide', 'intake:decide', 'project:read', 'proposal:approve', 'risk:accept'],
  project_manager: ['project:read', 'project:write', 'report:publish', 'risk:accept'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

function risk(id: number, changes: Partial<Risk> = {}): Risk {
  return {
    id,
    project_id: 3,
    kind: 'threat',
    title: `Risk ${id}`,
    cause: 'A cause',
    effect: 'An effect',
    category: 'project',
    objectives: ['schedule'],
    probability: 2,
    impact: 4,
    score: 8,
    band: 'Medium',
    response: 'reduce',
    response_plan: 'A plan',
    owner_user_id: 11,
    status: 'open',
    needs_judgement: false,
    show_in_proposal: false,
    source: 'person',
    escalated_to: 'sponsor',
    escalated_band: 'Medium',
    review_date: null,
    version: 1,
    created_at: '2026-10-01T09:00:00',
    updated_at: '2026-10-01T09:00:00',
    ...changes,
  }
}

function band(score: number, impact: number) {
  if (score >= 13) return 'High'
  return score >= 7 || impact === 5 ? 'Medium' : 'Low'
}

function cells(counts: Record<string, number>): HeatCell[] {
  const out: HeatCell[] = []
  for (let probability = 1; probability <= 5; probability++) {
    for (let impact = 1; impact <= 5; impact++) {
      const count = counts[`${probability}:${impact}`] ?? 0
      out.push({ probability, impact, band: band(probability * impact, impact), count })
    }
  }
  return out
}

const RISKS = [
  risk(1, { probability: 5, impact: 4, score: 20, band: 'High', category: 'ai' }),
  risk(2),
  risk(3, { probability: 2, impact: 4 }),
  risk(5, {
    probability: 1,
    impact: 2,
    score: 2,
    band: 'Low',
    escalated_to: 'project_manager',
    escalated_band: 'Low',
  }),
  risk(6, {
    probability: null,
    impact: null,
    score: null,
    band: null,
    status: 'proposed',
    escalated_to: null,
    escalated_band: null,
    title: 'Proposed by the agent',
    source: 'agent',
    suggested_probability: 3,
    suggested_impact: 2,
    suggested_probability_reason: 'The supplier has slipped twice this year.',
    suggested_impact_reason: 'A week of delay on a non-critical task.',
    suggested_model: 'claude-test',
    suggested_audit_id: 40,
  }),
  risk(7, {
    status: 'rejected',
    escalated_to: null,
    escalated_band: null,
    title: 'Rejected as a duplicate',
  }),
  risk(4, {
    probability: null,
    impact: null,
    score: null,
    band: null,
    needs_judgement: true,
    escalated_to: null,
    escalated_band: null,
    title: 'Reputational harm',
  }),
]

const HEATMAP = {
  kind: null,
  cells: cells({ '5:4': 1, '2:4': 2 }),
  bands: [
    { band: 'Low', count: 0, owner_role: 'project_manager' },
    { band: 'Medium', count: 2, owner_role: 'sponsor' },
    { band: 'High', count: 1, owner_role: 'sponsor' },
  ],
  unscored: 1,
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function page(items: unknown[]) {
  return { items, total: items.length, limit: 100, offset: 0 }
}

const SUGGESTION: RiskSuggestion = {
  mode: 'enforce',
  dry_run: false,
  proposals: [
    {
      kind: 'threat',
      category: 'ai',
      title: 'Model answers drift',
      probability: 3,
      impact: 4,
      response: 'reduce',
      needs_judgement: false,
      reason: 'Monthly data loads.',
    },
  ],
  dropped: [],
  saved: [risk(6, { status: 'proposed', source: 'agent' })],
}

const HELD =
  "this risk is escalated to the sponsor: it can't be moved to a lower band until the sponsor " +
  'or above accepts it'

interface FakeOptions {
  suggestStatus?: number
  suggestion?: RiskSuggestion
  patchStatus?: number
  stored?: Risk
  projects?: { id: number; name: string }[]
}

function fakeApi(role: string, options: FakeOptions = {}) {
  const user = { id: 7, email: `${role}@pmo.example`, display_name: 'Test User', role }
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input, 'http://localhost')
    const path = url.pathname
    const method = init.method ?? 'GET'
    if (path === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS[role] })
    if (path === '/api/v1/projects') {
      return json(page(options.projects ?? [{ id: 3, name: 'Harbour Analytics' }]))
    }
    if (path === '/api/v1/users') {
      return json(page([{ id: 11, display_name: 'Pat Owner' }]))
    }
    if (path === '/api/v1/projects/3/risks/heatmap') {
      const kind = url.searchParams.get('kind')
      if (kind === 'opportunity') {
        return json({ ...HEATMAP, kind, cells: cells({ '4:2': 1 }), unscored: 0 })
      }
      return json({ ...HEATMAP, kind })
    }
    if (path === '/api/v1/risks/escalations') {
      const mine = RISKS.filter((r) => r.escalated_to === role)
      return json(page(mine.map((r) => ({ ...r, project_name: 'Harbour Analytics' }))))
    }
    if (path === '/api/v1/projects/3/risks/suggest' && method === 'POST') {
      if (options.suggestStatus === 409) {
        return json({ detail: 'the risk agent is off (AGENT_MODE=off)' }, 409)
      }
      return json(options.suggestion ?? SUGGESTION)
    }
    const decision = path.match(/^\/api\/v1\/projects\/3\/risks\/(\d+)\/(accept|open|reject)$/)
    if (decision && method === 'POST') {
      const status = { accept: 'accepted', open: 'open', reject: 'rejected' }[decision[2]]
      return json(
        risk(Number(decision[1]), {
          status: status as Risk['status'],
          version: 2,
          escalated_to: null,
          escalated_band: null,
        }),
      )
    }
    if (path === '/api/v1/projects/3/risks' && method === 'GET') {
      return json({ items: RISKS, total: RISKS.length, limit: 25, offset: 0 })
    }
    const history = path.match(/^\/api\/v1\/projects\/3\/risks\/(\d+)\/history$/)
    if (history) {
      return json({
        items: [
          {
            id: 90,
            version: 1,
            action: 'create',
            actor_user_id: 11,
            actor_name: 'Pat Owner',
            before: null,
            after: { title: 'Risk 2' },
            created_at: '2026-10-01T09:00:00',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      })
    }
    const one = path.match(/^\/api\/v1\/projects\/3\/risks\/(\d+)$/)
    if (one && method === 'PATCH') {
      if (options.patchStatus === 409) {
        return json({ detail: 'this risk has changed since you read it' }, 409)
      }
      if (options.patchStatus === 403) return json({ detail: HELD }, 403)
      return json(risk(Number(one[1]), { status: 'closed', version: 2 }))
    }
    if (one) return json(options.stored ?? risk(Number(one[1])))
    return json({ detail: `unexpected ${method} ${path}` }, 404)
  })
}

async function mountRisks(role: string, options: FakeOptions = {}) {
  const fetchMock = fakeApi(role, options)
  vi.stubGlobal('fetch', fetchMock)
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter(true)
  router.push('/risks')
  await router.isReady()
  const wrapper = mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  // The page puts the chosen project in the address, a navigation of its own.
  await new Promise((resolve) => setTimeout(resolve))
  await flushPromises()
  return { wrapper, router, fetchMock }
}

function listCalls(fetchMock: ReturnType<typeof fakeApi>): URLSearchParams[] {
  return fetchMock.mock.calls
    .map(([input]) => new URL(String(input), 'http://localhost'))
    .filter((url) => url.pathname === '/api/v1/projects/3/risks')
    .map((url) => url.searchParams)
}

function lastList(fetchMock: ReturnType<typeof fakeApi>) {
  return Object.fromEntries(listCalls(fetchMock).at(-1)!)
}

type Wrapper = Awaited<ReturnType<typeof mountRisks>>['wrapper']

function setSelect(wrapper: Wrapper, test: string, value: unknown) {
  const select = wrapper
    .findAllComponents({ name: 'VSelect' })
    .find((c) => c.attributes('data-test') === test)
  expect(select, test).toBeTruthy()
  select!.vm.$emit('update:modelValue', value)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
  document.body.innerHTML = ''
})

describe('risk register helpers', () => {
  it('lays the heat map out with the highest impact first and probability across', () => {
    const grid = heatGrid(cells({ '5:4': 1, '2:4': 2 }))
    expect(grid).toHaveLength(5)
    expect(grid.map((row) => row[0].impact)).toEqual([5, 4, 3, 2, 1])
    expect(grid[1].map((cell) => cell.probability)).toEqual([1, 2, 3, 4, 5])
    expect(grid[1][1].count).toBe(2)
    expect(grid[1][4].count).toBe(1)
    expect(grid.flat().reduce((sum, cell) => sum + cell.count, 0)).toBe(3)
  })

  it('fills a missing heat map cell with zero', () => {
    const grid = heatGrid([])
    expect(grid.flat()).toHaveLength(25)
    expect(grid.flat().every((cell) => cell.count === 0)).toBe(true)
  })

  it('builds the list query from the filters, leaving out empty ones', () => {
    const query = listQuery({ ...NO_FILTERS, status: 'open', band: 'High' }, 3, 25)
    expect(Object.fromEntries(new URLSearchParams(query))).toEqual({
      status: 'open',
      band: 'High',
      limit: '25',
      offset: '50',
    })
  })

  it('filters to a clicked cell, and clears it on a second click', () => {
    const on = toggleCell(NO_FILTERS, { probability: 2, impact: 4 }, 'threat')
    expect(on).toMatchObject({ probability: 2, impact: 4, status: 'open', kind: 'threat' })
    const off = toggleCell(on, { probability: 2, impact: 4 }, 'threat')
    expect(off).toMatchObject({ probability: null, impact: null })
  })

  it("says a judgement risk needs a person's judgement instead of a score", () => {
    expect(scoreText({ score: 12, needs_judgement: false })).toBe('12')
    expect(scoreText({ score: null, needs_judgement: true })).toBe(NEEDS_JUDGEMENT)
    expect(scoreText({ score: null, needs_judgement: false })).toBe('Not scored yet')
  })

  it('takes band colours from the theme', () => {
    expect(bandColor('Medium')).toBe('band-medium')
    expect(bandColor(null)).toBe('grey')
    for (const name of ['Low', 'Medium', 'High']) expect(bandColors).toHaveProperty(bandColor(name))
  })

  it('lists the fields an audit row changed, without the version', () => {
    const entry = {
      id: 1,
      version: 2,
      action: 'update',
      actor_user_id: 1,
      actor_name: 'A',
      before: { title: 'Old', impact: 3, version: 1 },
      after: { title: 'New', impact: 3, version: 2 },
      created_at: '',
    }
    expect(changedFields(entry)).toEqual(['title'])
    expect(changedFields({ ...entry, before: null })).toEqual([])
  })
})

describe('accept, open and close rules', () => {
  const bands = HEATMAP.bands

  it("lets a role accept only a band at or below it, from the bands' owners", () => {
    expect(mayAcceptBand('project_manager', 'Low', bands)).toBe(true)
    expect(mayAcceptBand('project_manager', 'Medium', bands)).toBe(false)
    expect(mayAcceptBand('sponsor', 'High', bands)).toBe(true)
    expect(mayAcceptBand('auditor', 'Low', bands)).toBe(false)
    expect(mayAcceptBand('sponsor', null, bands)).toBe(false)
    expect(mayAcceptBand('sponsor', 'Medium', [])).toBe(false)
  })

  it('offers Accept only on an open, scored risk to a role holding risk:accept', () => {
    const open = { status: 'open' as const, score: 9, band: 'Medium' }
    expect(canAcceptRisk(open, 'sponsor', true, bands)).toBe(true)
    expect(canAcceptRisk(open, 'sponsor', false, bands)).toBe(false)
    expect(canAcceptRisk({ ...open, status: 'proposed' }, 'sponsor', true, bands)).toBe(false)
    expect(canAcceptRisk({ ...open, score: null, band: null }, 'sponsor', true, bands)).toBe(false)
  })

  it('lets only a role at or above the escalation owner clear an escalation', () => {
    const high = { escalated_to: 'sponsor', escalated_band: 'High' }
    const low = { escalated_to: 'project_manager', escalated_band: 'Low' }
    expect(mayClearEscalation(high, 'project_manager', true, bands)).toBe(false)
    expect(mayClearEscalation(high, 'sponsor', true, bands)).toBe(true)
    expect(mayClearEscalation(low, 'project_manager', true, bands)).toBe(true)
    expect(mayClearEscalation(low, 'project_manager', false, bands)).toBe(false)
    const none = { escalated_to: null, escalated_band: null }
    expect(mayClearEscalation(none, 'project_manager', false, bands)).toBe(true)
    expect(escalationLine(high)).toBe(
      'Escalated to the sponsor: only the sponsor can accept or close it',
    )
  })

  it('edits any risk but a rejected one', () => {
    expect(canEditRisk({ status: 'open' })).toBe(true)
    expect(canEditRisk({ status: 'rejected' })).toBe(false)
  })

  it('closes only open and accepted risks', () => {
    expect(canCloseRisk({ status: 'open' })).toBe(true)
    expect(canCloseRisk({ status: 'accepted' })).toBe(true)
    for (const status of ['proposed', 'closed', 'rejected'] as const) {
      expect(canCloseRisk({ status })).toBe(false)
    }
  })
})

describe('risks page', () => {
  it('is in the menu and loads the first project', async () => {
    const { wrapper, router, fetchMock } = await mountRisks('auditor')
    expect(wrapper.find('[data-test="nav-risks"]').exists()).toBe(true)
    expect(router.currentRoute.value.query.project).toBe('3')
    expect(wrapper.get('[data-test="page-title"]').text()).toBe('Risk register')
    expect(lastList(fetchMock)).toEqual({ limit: '25', offset: '0' })
  })

  it("links to the selected project's brief and follows the selector", async () => {
    const projects = [
      { id: 3, name: 'Harbour Analytics' },
      { id: 4, name: 'Depot Planning' },
    ]
    const { wrapper } = await mountRisks('auditor', { projects })
    const link = () => wrapper.find('[data-test="project-brief-link"]')
    expect(link().text()).toBe("Open the project's brief")
    expect(link().attributes('href')).toBe('/projects/3')
    setSelect(wrapper, 'project-picker', 4)
    await flushPromises()
    expect(link().attributes('href')).toBe('/projects/4')
  })

  it('shows no brief link with no project selected', async () => {
    const { wrapper } = await mountRisks('auditor', { projects: [] })
    expect(wrapper.find('[data-test="no-projects"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="project-brief-link"]').exists()).toBe(false)
  })

  it('shows the heat map counts and open totals per band', async () => {
    const { wrapper } = await mountRisks('auditor')
    expect(wrapper.get('[data-test="heat-cell-2-4"]').text()).toBe('2')
    expect(wrapper.get('[data-test="heat-cell-5-4"]').text()).toBe('1')
    expect(wrapper.get('[data-test="heat-cell-1-1"]').text()).toBe('')
    expect(wrapper.get('[data-test="heat-cell-5-4"]').classes()).toContain('bg-band-high')
    expect(wrapper.get('[data-test="heat-cell-2-4"]').classes()).toContain('bg-band-medium')
    expect(wrapper.get('[data-test="band-total-Medium"]').text()).toBe('Medium: 2')
    expect(wrapper.get('[data-test="band-total-High"]').text()).toBe('High: 1')
    expect(wrapper.get('[data-test="band-total-unscored"]').text()).toContain('1')
    expect(wrapper.get('[data-test="open-total"]').text()).toBe('4 open')
  })

  it('shows threats and opportunities separately', async () => {
    const { wrapper, fetchMock } = await mountRisks('auditor')
    await wrapper.get('[data-test="heat-kind-opportunity"]').trigger('click')
    await flushPromises()
    const last = String(fetchMock.mock.calls.at(-1)![0])
    expect(last).toBe('/api/v1/projects/3/risks/heatmap?kind=opportunity')
    expect(wrapper.get('[data-test="heat-cell-4-2"]').text()).toBe('1')
    expect(wrapper.get('[data-test="heat-cell-2-4"]').text()).toBe('')
  })

  it('filters the table to a clicked cell, and clears it on a second click', async () => {
    const { wrapper, fetchMock } = await mountRisks('auditor')
    await wrapper.get('[data-test="heat-cell-2-4"]').trigger('click')
    await flushPromises()
    expect(lastList(fetchMock)).toMatchObject({ probability: '2', impact: '4', status: 'open' })
    expect(wrapper.get('[data-test="heat-cell-2-4"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.find('[data-test="cell-filter"]').exists()).toBe(true)
    await wrapper.get('[data-test="heat-cell-2-4"]').trigger('click')
    await flushPromises()
    expect(lastList(fetchMock)).not.toHaveProperty('probability')
  })

  it('keeps the table in step with the heat map when the kind switch changes', async () => {
    const { wrapper, fetchMock } = await mountRisks('auditor')
    await wrapper.get('[data-test="heat-kind-threat"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="heat-cell-2-4"]').trigger('click')
    await flushPromises()
    expect(lastList(fetchMock)).toMatchObject({
      probability: '2',
      impact: '4',
      status: 'open',
      kind: 'threat',
    })

    await wrapper.get('[data-test="heat-kind-opportunity"]').trigger('click')
    await flushPromises()
    expect(lastList(fetchMock)).toEqual({ limit: '25', offset: '0' })
    expect(wrapper.find('[data-test="cell-filter"]').exists()).toBe(false)
  })

  it.each([
    ['filter-status', 'closed', { status: 'closed' }],
    ['filter-band', 'High', { band: 'High' }],
    ['filter-category', 'ai', { category: 'ai' }],
    ['filter-kind', 'opportunity', { kind: 'opportunity' }],
  ])('sends the %s filter to the API', async (test, value, expected) => {
    const { wrapper, fetchMock } = await mountRisks('auditor')
    setSelect(wrapper, test, value)
    await flushPromises()
    expect(lastList(fetchMock)).toEqual({ ...expected, limit: '25', offset: '0' })
  })

  it('offers the policy band names as band filters', async () => {
    const { wrapper } = await mountRisks('auditor')
    const select = wrapper
      .findAllComponents({ name: 'VSelect' })
      .find((c) => c.attributes('data-test') === 'filter-band')!
    expect(select.props('items')).toEqual(['Low', 'Medium', 'High'])
  })

  it("shows a judgement risk as needing a person's judgement, with owner names", async () => {
    const { wrapper } = await mountRisks('auditor')
    expect(wrapper.get('[data-test="score-4"]').text()).toBe(NEEDS_JUDGEMENT)
    expect(wrapper.get('[data-test="score-1"]').text()).toBe('20')
    expect(wrapper.get('[data-test="band-1"]').text()).toBe('High')
    expect(wrapper.get('[data-test="risk-table"]').text()).toContain('Pat Owner')
  })

  it('opens a drawer with every field and the history', async () => {
    const { wrapper } = await mountRisks('auditor')
    await wrapper.get('[data-test="risk-row-2"]').trigger('click')
    await flushPromises()
    const drawer = wrapper.get('[data-test="risk-drawer"]')
    expect(drawer.get('[data-test="drawer-title"]').text()).toBe('Risk 2')
    for (const name of ['Cause', 'Effect', 'Response plan', 'Owner', 'Version', 'Source']) {
      expect(drawer.find(`[data-test="field-${name}"]`).exists(), name).toBe(true)
    }
    expect(drawer.get('[data-test="field-Owner"]').text()).toBe('Pat Owner')
    expect(drawer.get('[data-test="history"]').text()).toContain('Create (version 1) by Pat Owner')
  })
})

describe('role-based buttons', () => {
  it('shows a project manager the add, edit and close buttons', async () => {
    const { wrapper } = await mountRisks('project_manager')
    expect(wrapper.find('[data-test="add-risk"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="add-from-library"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="suggest-risks"]').exists()).toBe(true)
    // Risk 5 is Low, so its escalation is the project manager's own.
    await wrapper.get('[data-test="risk-row-5"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="edit-risk"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="close-risk"]').exists()).toBe(true)
  })

  it.each(['sponsor', 'auditor'])('hides every change button from a %s', async (role) => {
    const { wrapper } = await mountRisks(role)
    expect(wrapper.find('[data-test="add-risk"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="add-from-library"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="suggest-risks"]').exists()).toBe(false)
    await wrapper.get('[data-test="risk-row-2"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="risk-drawer"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="edit-risk"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="close-risk"]').exists()).toBe(false)
  })

  it('hides the close button on a closed risk', async () => {
    const { wrapper } = await mountRisks('project_manager')
    await wrapper.get('[data-test="risk-row-5"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="close-risk"]').trigger('click')
    await flushPromises()
    ;(document.querySelector('[data-test="confirm-close-yes"]') as HTMLElement).click()
    await flushPromises()
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe('Risk closed.')
    expect(wrapper.find('[data-test="close-risk"]').exists()).toBe(false)
  })
})

describe('version conflicts', () => {
  it('shows a clear message and reloads the risk on a 409', async () => {
    const stored = risk(5, { title: 'Changed by someone else', version: 5 })
    const { wrapper, fetchMock } = await mountRisks('project_manager', {
      patchStatus: 409,
      stored,
    })
    await wrapper.get('[data-test="risk-row-5"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="close-risk"]').trigger('click')
    await flushPromises()
    ;(document.querySelector('[data-test="confirm-close-yes"]') as HTMLElement).click()
    await flushPromises()

    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(String(patch[1]!.body))).toEqual({ version: 1, status: 'closed' })
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe(CONFLICT_MESSAGE)
    expect(wrapper.get('[data-test="drawer-title"]').text()).toBe('Changed by someone else')
    expect(wrapper.get('[data-test="field-Version"]').text()).toBe('5')
    const reloads = fetchMock.mock.calls.filter(
      ([input, init]) => input === '/api/v1/projects/3/risks/5' && init?.method === 'GET',
    )
    expect(reloads.length).toBeGreaterThan(0)
  })

  it('shows a clear message and reloads the risk on a 409 when an edit is saved', async () => {
    const stored = risk(2, { title: 'Changed by someone else', version: 5 })
    const { wrapper, fetchMock } = await mountRisks('project_manager', {
      patchStatus: 409,
      stored,
    })
    await wrapper.get('[data-test="risk-row-2"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="edit-risk"]').trigger('click')
    await flushPromises()
    ;(document.querySelector('[data-test="form-save"]') as HTMLElement).click()
    await flushPromises()

    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(String(patch[1]!.body))).toMatchObject({ version: 1, title: 'Risk 2' })
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe(CONFLICT_MESSAGE)
    expect(wrapper.get('[data-test="drawer-title"]').text()).toBe('Changed by someone else')
    expect(wrapper.get('[data-test="field-Version"]').text()).toBe('5')
    const reloads = fetchMock.mock.calls.filter(
      ([input, init]) => input === '/api/v1/projects/3/risks/2' && (init?.method ?? 'GET') === 'GET',
    )
    expect(reloads.length).toBeGreaterThan(0)
  })
})

async function openRow(wrapper: Wrapper, id: number) {
  await wrapper.get(`[data-test="risk-row-${id}"]`).trigger('click')
  await flushPromises()
}

const has = (wrapper: Wrapper, test: string) => wrapper.find(`[data-test="${test}"]`).exists()

describe('decision buttons', () => {
  it('shows a sponsor Accept on a Medium risk, but no Open, Reject or Edit', async () => {
    const { wrapper } = await mountRisks('sponsor')
    await openRow(wrapper, 2)
    expect(has(wrapper, 'accept-risk')).toBe(true)
    expect(has(wrapper, 'open-risk')).toBe(false)
    expect(has(wrapper, 'reject-risk')).toBe(false)
    expect(has(wrapper, 'edit-risk')).toBe(false)
    expect(wrapper.get('[data-test="escalated"]').text()).toContain('sponsor (Medium band)')
  })

  it('hides Accept from a sponsor on a proposed or unscored risk', async () => {
    const { wrapper } = await mountRisks('sponsor')
    await openRow(wrapper, 6)
    expect(has(wrapper, 'accept-risk')).toBe(false)
    await openRow(wrapper, 4)
    expect(has(wrapper, 'accept-risk')).toBe(false)
  })

  it('shows a project manager Accept on a Low risk only', async () => {
    const { wrapper } = await mountRisks('project_manager')
    await openRow(wrapper, 2)
    expect(has(wrapper, 'accept-risk')).toBe(false)
    await openRow(wrapper, 5)
    expect(has(wrapper, 'accept-risk')).toBe(true)
  })

  it('shows a project manager Open and Reject on a proposed risk, without Close', async () => {
    const { wrapper } = await mountRisks('project_manager')
    await openRow(wrapper, 6)
    expect(has(wrapper, 'open-risk')).toBe(true)
    expect(has(wrapper, 'reject-risk')).toBe(true)
    expect(has(wrapper, 'close-risk')).toBe(false)
    await openRow(wrapper, 2)
    expect(has(wrapper, 'open-risk')).toBe(false)
    expect(has(wrapper, 'reject-risk')).toBe(false)
  })

  it('shows an auditor no decision buttons', async () => {
    const { wrapper } = await mountRisks('auditor')
    for (const id of [2, 5, 6]) {
      await openRow(wrapper, id)
      for (const test of ['accept-risk', 'open-risk', 'reject-risk', 'close-risk']) {
        expect(has(wrapper, test), `${test} on risk ${id}`).toBe(false)
      }
    }
  })

  it('accepts with a reason and a review date', async () => {
    const { wrapper, fetchMock } = await mountRisks('sponsor')
    await openRow(wrapper, 2)
    await wrapper.get('[data-test="accept-risk"]').trigger('click')
    await flushPromises()
    const yes = document.querySelector('[data-test="accept-yes"]') as HTMLButtonElement
    expect(yes.disabled).toBe(true)
    const reason = document.querySelector('[data-test="accept-reason"] textarea')!
    ;(reason as HTMLTextAreaElement).value = 'Cheaper to watch than to treat'
    reason.dispatchEvent(new Event('input'))
    const date = document.querySelector('[data-test="accept-review-date"] input')!
    ;(date as HTMLInputElement).value = '2999-01-01'
    date.dispatchEvent(new Event('input'))
    await flushPromises()
    expect(yes.disabled).toBe(false)
    yes.click()
    await flushPromises()
    const post = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/accept'))!
    expect(JSON.parse(String(post[1]!.body))).toEqual({
      version: 1,
      reason: 'Cheaper to watch than to treat',
      review_date: '2999-01-01',
    })
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe('Risk accepted.')
    expect(wrapper.get('[data-test="field-Status"]').text()).toBe('Accepted')
  })

  it("takes the earliest review date from the browser's local day, not UTC", async () => {
    const tz = process.env.TZ
    process.env.TZ = 'America/Los_Angeles'
    // 23:30 local on 3 November 2026 is already 4 November in UTC.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-03T23:30:00-08:00'))
    try {
      expect(new Date().toISOString().slice(0, 10)).toBe('2026-11-04')
      const { wrapper } = await mountRisks('sponsor')
      await openRow(wrapper, 2)
      await wrapper.get('[data-test="accept-risk"]').trigger('click')
      await flushPromises()
      const reason = document.querySelector('[data-test="accept-reason"] textarea')!
      ;(reason as HTMLTextAreaElement).value = 'Cheaper to watch than to treat'
      reason.dispatchEvent(new Event('input'))
      const date = document.querySelector('[data-test="accept-review-date"] input')!
      expect(date.getAttribute('min')).toBe('2026-11-03')
      ;(date as HTMLInputElement).value = '2026-11-03'
      date.dispatchEvent(new Event('input'))
      await flushPromises()
      const yes = document.querySelector('[data-test="accept-yes"]') as HTMLButtonElement
      expect(yes.disabled).toBe(false)
    } finally {
      vi.useRealTimers()
      if (tz === undefined) delete process.env.TZ
      else process.env.TZ = tz
    }
  })

  it('opens a proposed risk', async () => {
    const { wrapper, fetchMock } = await mountRisks('project_manager')
    await openRow(wrapper, 6)
    await wrapper.get('[data-test="open-risk"]').trigger('click')
    await flushPromises()
    const post = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/6/open'))!
    expect(JSON.parse(String(post[1]!.body))).toEqual({ version: 1 })
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe('Risk opened.')
  })

  it('rejects a proposed risk only with a reason', async () => {
    const { wrapper, fetchMock } = await mountRisks('project_manager')
    await openRow(wrapper, 6)
    await wrapper.get('[data-test="reject-risk"]').trigger('click')
    await flushPromises()
    const yes = document.querySelector('[data-test="reject-yes"]') as HTMLButtonElement
    expect(yes.disabled).toBe(true)
    const reason = document.querySelector('[data-test="reject-reason"] textarea')!
    ;(reason as HTMLTextAreaElement).value = 'Already covered by the vendor risk'
    reason.dispatchEvent(new Event('input'))
    await flushPromises()
    expect(yes.disabled).toBe(false)
    yes.click()
    await flushPromises()
    const post = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/6/reject'))!
    expect(JSON.parse(String(post[1]!.body))).toEqual({
      version: 1,
      reason: 'Already covered by the vendor risk',
    })
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe('Risk rejected.')
    expect(wrapper.get('[data-test="field-Status"]').text()).toBe('Rejected')
  })
})

describe('escalations list', () => {
  it.each([
    ['sponsor', [2, 3, 1]],
    ['project_manager', [5]],
  ])('lists what is escalated to a %s', async (role, ids) => {
    const { wrapper } = await mountRisks(role)
    const panel = wrapper.get('[data-test="escalations"]')
    for (const id of ids) expect(panel.find(`[data-test="escalation-${id}"]`).exists()).toBe(true)
    expect(panel.findAll('[data-test^="escalation-"]')).toHaveLength(ids.length)
  })

  it('says when nothing is escalated to the role', async () => {
    const { wrapper } = await mountRisks('auditor')
    expect(wrapper.get('[data-test="no-escalations"]').text()).toContain('Nothing is escalated')
  })

  it('opens an escalated risk from the list', async () => {
    const { wrapper } = await mountRisks('project_manager')
    await wrapper.get('[data-test="escalation-5"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="drawer-title"]').text()).toBe('Risk 5')
  })
})

describe('risk agent', () => {
  it('marks agent proposals in the table and the drawer, and only those', async () => {
    const { wrapper } = await mountRisks('auditor')
    expect(wrapper.get('[data-test="agent-chip-6"]').text()).toBe(AGENT_PROPOSAL)
    expect(wrapper.findAll('[data-test^="agent-chip-"]')).toHaveLength(1)
    await wrapper.get('[data-test="risk-row-6"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="drawer-agent-chip"]').text()).toBe(AGENT_PROPOSAL)
    await wrapper.get('[data-test="risk-row-2"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="drawer-agent-chip"]').exists()).toBe(false)
  })

  it('runs the agent for a project manager and reloads the register', async () => {
    const { wrapper, fetchMock } = await mountRisks('project_manager')
    const listsBefore = listCalls(fetchMock).length
    await wrapper.get('[data-test="suggest-risks"]').trigger('click')
    await flushPromises()
    const post = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/risks/suggest'))!
    expect(post[1]?.method).toBe('POST')
    expect(wrapper.get('[data-test="suggest-result"]').text()).toContain('proposed 1 risk.')
    expect(listCalls(fetchMock).length).toBeGreaterThan(listsBefore)
  })

  it('says why when the agent is off', async () => {
    const { wrapper } = await mountRisks('project_manager', { suggestStatus: 409 })
    await wrapper.get('[data-test="suggest-risks"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="suggest-result"]').text()).toContain('the risk agent is off')
  })

  it('words the result for each mode', () => {
    expect(suggestionMessage({ ...SUGGESTION, proposals: [], saved: [] })).toBe(
      'The risk agent found no new risks to suggest.',
    )
    expect(suggestionMessage({ ...SUGGESTION, mode: 'shadow', saved: [] })).toBe(
      'The risk agent suggested 1 risk (shadow mode, so nothing was saved): Model answers drift.',
    )
    expect(suggestionMessage(SUGGESTION)).toContain(`"${AGENT_PROPOSAL}"`)
  })
})

describe("the risk agent's suggested score", () => {
  const proposed = RISKS.find((r) => r.id === 6)!

  it('reads a suggestion only from a proposed risk that has one', () => {
    expect(agentSuggestion(proposed)).toEqual({
      probability: 3,
      impact: 2,
      probabilityReason: 'The supplier has slipped twice this year.',
      impactReason: 'A week of delay on a non-critical task.',
      model: 'claude-test',
    })
    expect(agentSuggestion({ ...proposed, status: 'open' })).toBeNull()
    expect(agentSuggestion({ ...proposed, suggested_impact: null })).toBeNull()
    expect(agentSuggestion(risk(2))).toBeNull()
  })

  it('fills the form copy with the suggested score and leaves the risk itself alone', () => {
    const filled = withSuggestedScore(proposed)
    expect(filled).toMatchObject({ probability: 3, impact: 2 })
    expect(proposed.probability).toBeNull()
    expect(proposed.impact).toBeNull()
    expect(withSuggestedScore(risk(2))).toEqual(risk(2))
  })

  it("shows the suggestion apart from the real score, labelled as the agent's", async () => {
    const { wrapper } = await mountRisks('project_manager')
    await openRow(wrapper, 6)
    const card = wrapper.get('[data-test="agent-suggestion"]')
    expect(card.get('[data-test="agent-suggestion-title"]').text()).toBe(SUGGESTED_BY_AGENT)
    expect(card.get('[data-test="agent-suggestion-model"]').text()).toBe('Model: claude-test')
    expect(card.get('[data-test="agent-suggestion-probability"]').text()).toBe(
      'Probability 3. The supplier has slipped twice this year.',
    )
    expect(card.get('[data-test="agent-suggestion-impact"]').text()).toBe(
      'Impact 2. A week of delay on a non-critical task.',
    )
    expect(card.get('[data-test="agent-suggestion-caption"]').text()).toBe(
      "The risk's own score stays empty until a person sets it, or, for a Low suggestion, " +
        'until the objection window applies it.',
    )
    expect(wrapper.get('[data-test="field-Probability"]').text()).toBe('—')
    expect(wrapper.get('[data-test="field-Impact"]').text()).toBe('—')
    await openRow(wrapper, 2)
    expect(has(wrapper, 'agent-suggestion')).toBe(false)
  })

  it('"Use this score" fills the edit form, and saves only when the person saves', async () => {
    const { wrapper, fetchMock } = await mountRisks('project_manager')
    await openRow(wrapper, 6)
    await wrapper.get('[data-test="use-suggestion"]').trigger('click')
    await flushPromises()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
    expect(document.querySelector('[data-test="risk-form"]')).not.toBeNull()
    ;(document.querySelector('[data-test="form-save"]') as HTMLElement).click()
    await flushPromises()
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(String(patch[0])).toBe('/api/v1/projects/3/risks/6')
    expect(JSON.parse(String(patch[1]!.body))).toMatchObject({
      probability: 3,
      impact: 2,
      version: 1,
    })
  })

  it('shows a reader the suggestion without "Use this score"', async () => {
    const { wrapper } = await mountRisks('auditor')
    await openRow(wrapper, 6)
    expect(has(wrapper, 'agent-suggestion')).toBe(true)
    expect(has(wrapper, 'use-suggestion')).toBe(false)
  })
})

describe('escalation hold and final rejections', () => {
  it('hides Close risk on a risk escalated above the project manager, with the line', async () => {
    const { wrapper } = await mountRisks('project_manager')
    await openRow(wrapper, 2)
    expect(has(wrapper, 'edit-risk')).toBe(true)
    expect(has(wrapper, 'close-risk')).toBe(false)
    expect(wrapper.get('[data-test="escalation-hold"]').text()).toBe(
      'Escalated to the sponsor: only the sponsor can accept or close it.',
    )
    await openRow(wrapper, 5)
    expect(has(wrapper, 'close-risk')).toBe(true)
    expect(has(wrapper, 'escalation-hold')).toBe(false)
  })

  it('shows no escalation line to the escalation owner', async () => {
    const { wrapper } = await mountRisks('sponsor')
    await openRow(wrapper, 2)
    expect(has(wrapper, 'escalated')).toBe(true)
    expect(has(wrapper, 'escalation-hold')).toBe(false)
  })

  it('shows no Edit button on a rejected risk', async () => {
    const { wrapper } = await mountRisks('project_manager')
    await openRow(wrapper, 7)
    expect(has(wrapper, 'risk-drawer')).toBe(true)
    expect(has(wrapper, 'edit-risk')).toBe(false)
    expect(has(wrapper, 'close-risk')).toBe(false)
  })

  it("shows the API's message in the drawer when a save is refused", async () => {
    const { wrapper } = await mountRisks('project_manager', { patchStatus: 403 })
    await openRow(wrapper, 2)
    await wrapper.get('[data-test="edit-risk"]').trigger('click')
    await flushPromises()
    ;(document.querySelector('[data-test="form-save"]') as HTMLElement).click()
    await flushPromises()
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe(HELD)
    expect(document.querySelector('[data-test="form-error"]')?.textContent?.trim()).toBe(HELD)
  })
})
