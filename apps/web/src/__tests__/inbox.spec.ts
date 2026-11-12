import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  dueText,
  EMPTY,
  formFor,
  type InboxEntry,
  mayHold,
  NO_DECISIONS,
  NOT_YOURS_TO_DECIDE,
  POLL_MS,
  receivesItems,
  stepTitle,
  subjectLink,
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

// The no-deadlock item (item 6.10): every approver contributed to the copy, so it goes to the admin.
const NO_FREE_APPROVER =
  'every approver contributed to this copy, so none of them may approve or return it: make a ' +
  'sponsor who did not contribute the decider'
const ADMIN_ITEM = entry({
  id: 21,
  step: 'approve_proposal',
  subject_type: 'proposal',
  subject_id: 5,
  subject_title: 'Proposal for Pilot',
  project_id: 2,
  project_name: 'Pilot',
  who: 'admin',
  reason: 'the proposal is submitted and waiting for approval',
  handoff: NO_FREE_APPROVER,
  facts: [],
  recommendation: 'Approve it: the checker has no flags open.',
})

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

describe('the Inbox page', () => {
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

  it('is titled Inbox and keeps the count on the page', async () => {
    vi.stubGlobal('fetch', signedInAs('project_manager'))
    const { wrapper } = await mountAt('/inbox')
    expect(wrapper.get('[data-test="page-title"]').text()).toBe('Inbox')
    expect(wrapper.get('[data-test="inbox-waiting"]').text()).toBe('2 waiting')
    wrapper.unmount()
  })

  it('says the inbox is empty when nothing is waiting', async () => {
    vi.stubGlobal('fetch', signedInAs('ethics_reviewer'))
    const { wrapper } = await mountAt('/inbox')
    expect(wrapper.get('[data-test="inbox-empty"]').text()).toBe(EMPTY)
    expect(EMPTY).toContain('inbox is empty')
    wrapper.unmount()
  })

  it('links to the subject with from=inbox, and returns with that item expanded', async () => {
    vi.stubGlobal('fetch', signedInAs('project_manager'))
    const { wrapper } = await mountAt('/inbox?open=item-2')
    // The item named in `open` is expanded on arrival.
    const plan = wrapper.get('[data-test="inbox-item-item-2"]')
    const link = plan.get('[data-test="inbox-open-subject"]')
    expect(link.attributes('href')).toBe('/projects/2?from=inbox&item=item-2')
    expect(wrapper.find('[data-test="inbox-item-window-9"] [data-test="inbox-reason"]').exists())
      .toBe(false)
    wrapper.unmount()
  })

  it('tells the auditor plainly that nothing will appear', async () => {
    const role = 'auditor'
    const fetch = signedInAs(role)
    vi.stubGlobal('fetch', fetch)
    const { wrapper } = await mountAt('/inbox')
    expect(wrapper.find('[data-test="inbox-no-decisions"]').text()).toBe(NO_DECISIONS)
    expect(wrapper.findAll('[data-test^="inbox-item-"]')).toHaveLength(0)
    expect(fetch.mock.calls.some(([path]) => String(path).startsWith('/api/v1/inbox?'))).toBe(false)
    wrapper.unmount()
  })

  it('shows the admin a proposal decision routed to them, and why, with no decision buttons', async () => {
    const fetch = signedInAs('admin', [ADMIN_ITEM])
    vi.stubGlobal('fetch', fetch)
    const { wrapper } = await mountAt('/inbox')
    expect(wrapper.find('[data-test="inbox-no-decisions"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-test^="inbox-item-"]')).toHaveLength(1)
    await expandAll(wrapper)
    expect(wrapper.find('[data-test="inbox-handoff"]').text()).toContain(NO_FREE_APPROVER)
    expect(wrapper.find('[data-test="inbox-not-yours"]').text()).toBe(NOT_YOURS_TO_DECIDE)
    const body = wrapper.find('[data-test="inbox-item-item-21"] .v-expansion-panel-text')
    const buttons = body.findAll('button').map((button) => button.text())
    expect(wrapper.find('[data-test^="proposal-approve"]').exists()).toBe(false)
    expect(buttons.some((text) => /approve|return/i.test(text))).toBe(false)
    // The proposal's own form is never loaded for them.
    expect(fetch.mock.calls.some(([path]) => String(path).includes('/proposal'))).toBe(false)
    wrapper.unmount()
  })

  it('tells the admin nothing needs them when nothing is routed to them', async () => {
    vi.stubGlobal('fetch', signedInAs('admin'))
    const { wrapper } = await mountAt('/inbox')
    expect(wrapper.find('[data-test="inbox-no-decisions"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="inbox-empty"]').exists()).toBe(true)
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

  it('shows a dot on the menu, labelled with the count, and the count on the home page', async () => {
    vi.stubGlobal('fetch', signedInAs('project_manager'))
    const { wrapper } = await mountAt('/')
    const dot = wrapper.get('[data-test="inbox-dot"]')
    expect(dot.text()).toBe('')
    expect(dot.attributes('aria-label')).toBe('2 waiting')
    expect(wrapper.find('[data-test="inbox-badge"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="home-inbox"]').text()).toBe('Inbox: 2 waiting')
    wrapper.unmount()
  })

  it('shows no dot when nothing is waiting', async () => {
    vi.stubGlobal('fetch', signedInAs('auditor'))
    const { wrapper } = await mountAt('/')
    expect(wrapper.find('[data-test="inbox-dot"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="home-inbox"]').text()).toBe('Inbox: 0 waiting')
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

  it('loads the inbox for anyone the API can route an item to', () => {
    const can = (role: string) => (action: string) => ACTIONS[role].includes(action)
    expect(receivesItems(can('admin'), 'admin')).toBe(true)
    expect(receivesItems(can('sponsor'), 'sponsor')).toBe(true)
    expect(receivesItems(can('auditor'), 'auditor')).toBe(false)
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
    expect(formFor({ step: 'publish_status_report', subject_type: 'status_report' })).toBe('link')
  })

  it("links a status report item to the report on its project's Status reports page", () => {
    const item = entry({
      step: 'publish_status_report',
      subject_type: 'status_report',
      subject_id: 31,
      project_id: 5,
    })
    expect(subjectLink(item)).toEqual({
      name: 'status-reports',
      params: { id: 5 },
      query: { from: 'inbox', item: `${item.kind}-${item.id}`, report: '31' },
    })
  })

  it('links a report flag to where it is cleared (item 7.5)', () => {
    const unread = entry({
      step: 'report_unread',
      subject_type: 'status_report',
      subject_id: 31,
      project_id: 5,
    })
    expect(subjectLink(unread)).toEqual({
      name: 'status-report-executive',
      params: { id: '5', reportId: '31' },
      query: { from: 'inbox', item: `${unread.kind}-${unread.id}` },
    })
    const overdue = entry({ step: 'report_overdue_sponsor', subject_type: 'project', subject_id: 5, project_id: 5 })
    expect(subjectLink(overdue)).toEqual({
      name: 'status-reports',
      params: { id: 5 },
      query: { from: 'inbox', item: `${overdue.kind}-${overdue.id}` },
    })
    expect(stepTitle('report_unread')).toBe('Read the published status report')
  })
})
