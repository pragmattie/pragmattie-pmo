import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  dueText,
  formFor,
  type InboxEntry,
  mayHold,
  NO_DECISIONS,
  POLL_MS,
  waitsOnText,
} from '../lib/inbox'
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
  project_manager: [
    'project:read',
    'project:write',
    'report:publish',
    'risk:accept',
    'standards:read',
  ],
  ethics_reviewer: ['audit:read', 'gate:decide', 'project:read', 'standards:read'],
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

function entry(changes: Partial<InboxEntry> = {}): InboxEntry {
  return {
    kind: 'item',
    id: 1,
    step: 'pass_gate',
    subject_type: 'request',
    subject_id: 4,
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
    facts: [
      { key: 'value_case_confirmed', label: 'Value case', value: 'missing: benefits', met: false },
    ],
    recommendation: 'Return it: 1 of 1 checks are not met (Value case).',
    ...changes,
  }
}

const WINDOW = entry({
  kind: 'window',
  id: 9,
  step: 'score_risk',
  subject_type: 'risk',
  subject_id: 3,
  subject_title: 'Model drift',
  project_id: 2,
  project_name: 'Pilot',
  who: 'project_manager',
  reason: 'the risk is proposed and not scored',
  action_id: 9,
  due_at: '2026-11-07T10:15:00',
  facts: [{ key: 'score', label: 'Score', value: 'not scored', met: null }],
  recommendation: 'Score its probability and impact so it gets a band and an owner.',
})

// What each role's inbox holds in these tests.
const ITEMS: Record<string, InboxEntry[]> = {
  sponsor: [entry()],
  project_manager: [
    WINDOW,
    entry({
      id: 2,
      step: 'draft_plan',
      subject_type: 'project',
      subject_id: 2,
      subject_title: 'Pilot',
      project_id: 2,
      project_name: 'Pilot',
      who: 'project_manager',
      named_user_id: 7,
      named_user_name: 'Test User',
      reason: 'the plan is incomplete: the project has no plan',
      handoff: 'no agent does this yet',
      facts: [],
      recommendation: 'Complete the plan: the project has no plan.',
    }),
  ],
  ethics_reviewer: [],
  admin: [],
  auditor: [],
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function signedInAs(role: string, items = ITEMS[role]) {
  const user = {
    id: 7,
    email: `${role}@pmo.example`,
    display_name: 'Test User',
    role,
    actions: ACTIONS[role],
  }
  return vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/v1/auth/me') return json(user)
    if (path === '/api/v1/health') return json({ status: 'ok', version: '0.1.0' })
    if (path === '/api/v1/inbox/count') return json({ count: items.length })
    if (path.startsWith('/api/v1/inbox?')) {
      return json({ items, total: items.length, limit: 50, offset: 0 })
    }
    if (path.startsWith('/api/v1/orchestrator/actions/') && init?.method === 'POST') {
      return json({ inbox_item_id: 12, action: {} })
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

type Wrapper = Awaited<ReturnType<typeof mountAt>>['wrapper']

async function expandAll(wrapper: Wrapper) {
  for (const title of wrapper.findAll('.v-expansion-panel-title')) await title.trigger('click')
  await flushPromises()
}

describe('the Needs you page', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  it.each(['sponsor', 'project_manager', 'ethics_reviewer'])(
    'shows the %s only the items the API gives them',
    async (role) => {
      vi.stubGlobal('fetch', signedInAs(role))
      const { wrapper } = await mountAt('/inbox')
      const items = wrapper.findAll('[data-test^="inbox-item-"]')
      expect(items).toHaveLength(ITEMS[role].length)
      if (!ITEMS[role].length) {
        expect(wrapper.find('[data-test="inbox-empty"]').exists()).toBe(true)
      }
      wrapper.unmount()
    },
  )

  it.each(['admin', 'auditor'])('tells the %s plainly that nothing will appear', async (role) => {
    const fetch = signedInAs(role)
    vi.stubGlobal('fetch', fetch)
    const { wrapper } = await mountAt('/inbox')
    expect(wrapper.find('[data-test="inbox-no-decisions"]').text()).toBe(NO_DECISIONS)
    expect(wrapper.findAll('[data-test^="inbox-item-"]')).toHaveLength(0)
    expect(fetch.mock.calls.some(([path]) => String(path).startsWith('/api/v1/inbox?'))).toBe(false)
    wrapper.unmount()
  })

  it('shows why each item is here, its facts and the recommendation', async () => {
    vi.stubGlobal('fetch', signedInAs('project_manager'))
    const { wrapper } = await mountAt('/inbox')
    await expandAll(wrapper)
    const plan = wrapper.find('[data-test="inbox-item-item-2"]')
    expect(plan.find('[data-test="inbox-reason"]').text()).toContain('the project has no plan')
    expect(plan.find('[data-test="inbox-handoff"]').text()).toContain('no agent does this yet')
    expect(plan.text()).toContain('the project manager (Test User)')
    expect(plan.find('[data-test="inbox-recommendation"]').text()).toContain(
      'Complete the plan: the project has no plan.',
    )
    // A drafting step is done on the project's own page.
    expect(plan.find('[data-test="inbox-open-subject"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a window action with its time and a Hold button for its role', async () => {
    // Vuetify's dialog reads the visual viewport, which jsdom lacks.
    vi.stubGlobal('visualViewport', new EventTarget())
    const fetch = signedInAs('project_manager')
    vi.stubGlobal('fetch', fetch)
    const { wrapper } = await mountAt('/inbox')
    await expandAll(wrapper)
    const window = wrapper.find('[data-test="inbox-item-window-9"]')
    expect(window.find('[data-test="inbox-due"]').text()).toMatch(
      /^happens at \d\d:\d\d unless you hold it$/,
    )
    await window.find('[data-test="inbox-hold"]').trigger('click')
    await flushPromises()
    const reason = document.querySelector('[data-test="inbox-hold-reason"] textarea')!
    ;(reason as HTMLTextAreaElement).value = 'I want to score this one myself'
    reason.dispatchEvent(new Event('input'))
    await flushPromises()
    ;(document.querySelector('[data-test="inbox-hold-yes"]') as HTMLButtonElement).click()
    await flushPromises()
    const hold = fetch.mock.calls.find(([path]) => String(path).includes('/hold'))!
    expect(hold[0]).toBe('/api/v1/orchestrator/actions/9/hold')
    expect(JSON.parse(String(hold[1]!.body))).toEqual({ reason: 'I want to score this one myself' })
    wrapper.unmount()
  })

  it('shows no Hold button to a role that is not the step\'s', async () => {
    vi.stubGlobal('fetch', signedInAs('sponsor', [WINDOW]))
    const { wrapper } = await mountAt('/inbox')
    await expandAll(wrapper)
    expect(wrapper.find('[data-test="inbox-hold"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="inbox-hold-who"]').text()).toContain('project manager')
    wrapper.unmount()
  })
})

describe('the inbox count', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('shows on the menu and the home page', async () => {
    vi.stubGlobal('fetch', signedInAs('project_manager'))
    const { wrapper } = await mountAt('/')
    expect(wrapper.find('[data-test="inbox-badge"]').text()).toBe('2')
    expect(wrapper.find('[data-test="home-inbox"]').text()).toBe('2 decisions need you')
    wrapper.unmount()
  })

  it('shows no badge when nothing is waiting', async () => {
    vi.stubGlobal('fetch', signedInAs('auditor'))
    const { wrapper } = await mountAt('/')
    expect(wrapper.find('[data-test="inbox-badge"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="home-inbox"]').text()).toBe('0 decisions need you')
    wrapper.unmount()
  })

  it('refreshes every 15 seconds, pauses while the tab is hidden and refreshes on return', async () => {
    vi.useFakeTimers()
    const fetch = signedInAs('sponsor')
    vi.stubGlobal('fetch', fetch)
    const { wrapper } = await mountAt('/')
    const counts = () => fetch.mock.calls.filter(([p]) => p === '/api/v1/inbox/count').length
    const first = counts()
    expect(first).toBe(1)
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(counts()).toBe(2)
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(counts()).toBe(2)
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(counts()).toBe(3)
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(counts()).toBe(4)
    wrapper.unmount()
  })
})

describe('the inbox helpers', () => {
  it('names the role and the person it waits on', () => {
    expect(waitsOnText(entry())).toBe('the sponsor')
    expect(waitsOnText(entry({ named_user_name: 'Ada' }))).toBe('the sponsor (Ada)')
  })

  it('offers Hold only on a window action, to its role', () => {
    expect(mayHold(WINDOW, 'project_manager')).toBe(true)
    expect(mayHold(WINDOW, 'sponsor')).toBe(false)
    expect(mayHold(entry({ who: 'project_manager' }), 'project_manager')).toBe(false)
  })

  it('says when a window action happens', () => {
    expect(dueText(entry())).toBeNull()
    expect(dueText(WINDOW)).toMatch(/^happens at \d\d:\d\d unless you hold it$/)
  })

  it('opens the existing form for each step', () => {
    expect(formFor({ step: 'pass_gate', subject_type: 'project' })).toBe('gate')
    expect(formFor({ step: 'accept_risk', subject_type: 'risk' })).toBe('risk')
    expect(formFor({ step: 'send_proposal', subject_type: 'proposal' })).toBe('proposal')
    expect(formFor({ step: 'decide_change_pm', subject_type: 'change_request' })).toBe('changes')
    expect(formFor({ step: 'draft_value_case', subject_type: 'request' })).toBe('link')
  })
})
