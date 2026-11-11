import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import InboxDecision from '../components/InboxDecision.vue'
import RiskScoreForm from '../components/RiskScoreForm.vue'
import type { InboxEntry } from '../lib/inbox'
import { CONFLICT_MESSAGE, type Risk, scorePreview } from '../lib/risks'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'
import { useAuthStore } from '../stores/auth'

// A `score_risk` item is scored in place from the Inbox, through the existing risk update route
// with the risk's version; the API checks every rule again. Edit and Close open that risk.

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('visualViewport', new EventTarget())

const ACTIONS: Record<string, string[]> = {
  project_manager: ['project:read', 'project:write', 'report:publish', 'risk:accept'],
  sponsor: ['gate:decide', 'intake:decide', 'project:read', 'proposal:approve', 'risk:accept'],
}

const ITEM: InboxEntry = {
  kind: 'item',
  id: 14,
  step: 'score_risk',
  subject_type: 'risk',
  subject_id: 6,
  subject_title: 'Reputational harm from model answers',
  project_id: 3,
  project_name: 'Harbour Analytics',
  who: 'project_manager',
  named_user_id: null,
  named_user_name: null,
  reason: 'the risk is proposed and not scored',
  handoff: "the risk needs a person's judgement, so a person scores it",
  created_at: '2026-11-07T09:00:00',
  action_id: null,
  due_at: null,
  facts: [],
  recommendation: 'Score its probability and impact.',
}

// A risk that needs a person's judgement: the agent may not score it.
const JUDGEMENT: Risk = {
  id: 6,
  project_id: 3,
  kind: 'threat',
  title: 'Reputational harm from model answers',
  cause: 'A cause',
  effect: 'An effect',
  category: 'ai',
  objectives: ['quality'],
  probability: null,
  impact: null,
  score: null,
  band: null,
  response: 'reduce',
  response_plan: 'A plan',
  owner_user_id: null,
  status: 'proposed',
  needs_judgement: true,
  show_in_proposal: false,
  source: 'agent',
  escalated_to: null,
  escalated_band: null,
  review_date: null,
  version: 4,
  created_at: '2026-11-01T09:00:00',
  updated_at: '2026-11-01T09:00:00',
}

const SUGGESTED: Risk = {
  ...JUDGEMENT,
  needs_judgement: false,
  suggested_probability: 2,
  suggested_impact: 5,
  suggested_probability_reason: 'Seen before.',
  suggested_impact_reason: 'Key function.',
  suggested_model: 'a-model',
}

// The policy's band for each cell, as the heat map sends it.
const CELLS = [1, 2, 3, 4, 5].flatMap((probability) =>
  [1, 2, 3, 4, 5].map((impact) => {
    const score = probability * impact
    const band = score >= 13 ? 'High' : score >= 7 || impact === 5 ? 'Medium' : 'Low'
    return { probability, impact, band, count: 0 }
  }),
)
const HEATMAP = {
  kind: null,
  cells: CELLS,
  bands: [
    { band: 'Low', count: 0, owner_role: 'project_manager' },
    { band: 'Medium', count: 0, owner_role: 'sponsor' },
    { band: 'High', count: 0, owner_role: 'sponsor' },
  ],
  unscored: 1,
}

// The labelled scales as the risk policy route sends them.
const SCALES = {
  probability: [1, 2, 3, 4, 5].map((level) => ({
    level,
    label: ['Very low', 'Low', 'Medium', 'High', 'Very high'][level - 1],
    definition: `Probability definition ${level}.`,
  })),
  impact: [1, 2, 3, 4, 5].map((level) => ({
    level,
    label: ['Very low', 'Low', 'Medium', 'High', 'Very high'][level - 1],
    definition: `Impact definition ${level}.`,
  })),
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeApi(stored: Risk, patchStatus = 200) {
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init.method ?? 'GET'
    if (path === '/api/v1/projects/3/risks/6' && method === 'PATCH') {
      if (patchStatus === 409) return json({ detail: 'this risk has changed since you read it' }, 409)
      const body = JSON.parse(String(init.body))
      return json({ ...stored, ...body, score: 12, band: 'Medium', version: stored.version + 1 })
    }
    if (path === '/api/v1/projects/3/risks/6') return json(stored)
    if (path === '/api/v1/projects/3/risks/heatmap') return json(HEATMAP)
    if (path === '/api/v1/risk-policy/scales') return json(SCALES)
    if (path === '/api/v1/users') {
      return json({ items: [{ id: 21, display_name: 'Rosa Owner' }], total: 1, limit: 100, offset: 0 })
    }
    if (path === '/api/v1/projects/3/risks/6/history') {
      return json({ items: [], total: 0, limit: 50, offset: 0 })
    }
    return json({ detail: `unexpected ${method} ${path}` }, 404)
  })
}

async function mountItem(stored: Risk, options: { patchStatus?: number; role?: string } = {}) {
  const role = options.role ?? 'project_manager'
  const fetchMock = fakeApi(stored, options.patchStatus)
  vi.stubGlobal('fetch', fetchMock)
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = {
    id: 7,
    email: `${role}@pmo.example`,
    display_name: 'Test User',
    role,
    actions: ACTIONS[role],
  }
  auth.checked = true
  const router = makeRouter(true)
  const wrapper = mount(InboxDecision, {
    props: { entry: ITEM },
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { wrapper, fetchMock, router }
}

type Wrapper = Awaited<ReturnType<typeof mountItem>>['wrapper']

async function choose(wrapper: Wrapper, probability: number, impact: number) {
  const form = wrapper.findComponent(RiskScoreForm)
  form.vm.$emit('update:probability', probability)
  form.vm.$emit('update:impact', impact)
  await flushPromises()
}

function patches(fetchMock: ReturnType<typeof fakeApi>) {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
  document.body.innerHTML = ''
})

describe('scoring a risk in the Inbox', () => {
  it("scores a risk that needs a person's judgement through the update route, with its version", async () => {
    const { wrapper, fetchMock } = await mountItem(JUDGEMENT)
    expect(wrapper.find('[data-test="score-form"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="score-save"]').attributes('disabled')).toBeDefined()
    await choose(wrapper, 3, 4)
    const preview = wrapper.get('[data-test="score-preview"]').text()
    expect(preview).toContain('Score 12')
    expect(wrapper.get('[data-test="score-band"]').text()).toBe('Medium')
    await wrapper.get('[data-test="score-save"]').trigger('click')
    await flushPromises()
    const [call] = patches(fetchMock)
    expect(call[0]).toBe('/api/v1/projects/3/risks/6')
    expect(JSON.parse(String(call[1]!.body))).toEqual({ version: 4, probability: 3, impact: 4 })
    // The Inbox reloads, and the item leaves once the API reports it done.
    expect(wrapper.emitted('decided')![0]).toEqual(['Risk scored.'])
  })

  it('shows the conflict message on a 409 and reloads the risk', async () => {
    const { wrapper, fetchMock } = await mountItem(JUDGEMENT, { patchStatus: 409 })
    await choose(wrapper, 2, 2)
    await wrapper.get('[data-test="score-save"]').trigger('click')
    await flushPromises()
    expect(patches(fetchMock)).toHaveLength(1)
    expect(wrapper.get('[data-test="drawer-message"]').text()).toBe(CONFLICT_MESSAGE)
    expect(wrapper.emitted('decided')).toBeUndefined()
    const gets = fetchMock.mock.calls.filter(
      ([path, init]) => path === '/api/v1/projects/3/risks/6' && init?.method !== 'PATCH',
    )
    expect(gets.length).toBe(2)
  })

  it('fills the form from the agent\'s score with "Use this score", saving nothing', async () => {
    const { wrapper, fetchMock } = await mountItem(SUGGESTED)
    await wrapper.get('[data-test="use-suggestion"]').trigger('click')
    await flushPromises()
    const form = wrapper.findComponent(RiskScoreForm)
    expect(form.props('probability')).toBe(2)
    expect(form.props('impact')).toBe(5)
    expect(wrapper.get('[data-test="score-preview"]').text()).toContain('Score 10')
    expect(patches(fetchMock)).toHaveLength(0)
  })

  it('shows no scoring form to a role that cannot edit risks', async () => {
    const { wrapper } = await mountItem(JUDGEMENT, { role: 'sponsor' })
    expect(wrapper.find('[data-test="risk-drawer"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="score-form"]').exists()).toBe(false)
  })

  it('offers each level with its label and definition, and shows the chosen one as help', async () => {
    const { wrapper } = await mountItem(JUDGEMENT)
    const form = wrapper.findComponent(RiskScoreForm)
    const field = (test: string) =>
      form.findAllComponents({ name: 'VSelect' }).find((c) => c.attributes('data-test') === test)!
    expect(field('score-probability').props('items')[2]).toEqual({
      title: '3 Medium',
      value: 3,
      props: { subtitle: 'Probability definition 3.' },
    })
    expect(field('score-impact').props('items').map((i: { title: string }) => i.title)).toEqual([
      '1 Very low',
      '2 Low',
      '3 Medium',
      '4 High',
      '5 Very high',
    ])
    await choose(wrapper, 3, 4)
    expect(form.text()).toContain('Probability definition 3.')
    expect(form.text()).toContain('Impact definition 4.')
  })

  it("names the risk's owner rather than showing an id", async () => {
    const { wrapper } = await mountItem({ ...JUDGEMENT, owner_user_id: 21 })
    expect(wrapper.get('[data-test="field-Owner"]').text()).toBe('Rosa Owner')
  })

  it('opens the register at that risk, with its drawer, from Edit', async () => {
    const { wrapper, router } = await mountItem(JUDGEMENT)
    const push = vi.spyOn(router, 'push')
    await wrapper.get('[data-test="edit-risk"]').trigger('click')
    expect(push).toHaveBeenCalledWith({
      name: 'risks',
      query: { project: 3, risk: 6, from: 'inbox', item: 'item-14' },
    })
  })
})

describe('the score preview', () => {
  it('multiplies probability by impact and takes the band from the heat map cell', () => {
    expect(scorePreview(CELLS, 3, 4)).toEqual({ score: 12, band: 'Medium' })
    expect(scorePreview(CELLS, 1, 5)).toEqual({ score: 5, band: 'Medium' })
    expect(scorePreview(CELLS, null, 4)).toBeNull()
    expect(scorePreview([], 2, 2)).toEqual({ score: 4, band: null })
  })
})
