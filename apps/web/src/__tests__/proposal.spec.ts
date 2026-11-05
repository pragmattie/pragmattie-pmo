import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ProposalSection from '../components/ProposalSection.vue'
import {
  approvalText,
  approvedCopy,
  buttonsFor,
  flagsFor,
  outOfDateText,
  type Proposal,
  type ProposalVersion,
} from '../lib/proposals'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'
import { useAuthStore } from '../stores/auth'
import ProposalPrintView from '../views/ProposalPrintView.vue'

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
const PM_ID = 11
const REASON = 'Matches the plan and what the client asked for'

function proposal(changes: Partial<Proposal> = {}): Proposal {
  return {
    id: 1,
    project_id: 5,
    project_name: 'Forecast',
    client_name: 'Northwind Example',
    status: 'draft',
    summary: 'A pilot for 1650.00',
    scope_in: 'Two warehouses',
    scope_out: null,
    assumptions: null,
    terms: 'Pay by 2027-02-01',
    price: '1650.00',
    estimate_lines: [{ description: 'Team', category: 'people', amount: '1650.00' }],
    milestones: [{ name: 'Pilot', target_date: '2027-03-01', description: null }],
    deliverables: [{ name: 'Forecast model', description: null, milestone: 'Pilot' }],
    risks: [
      { risk_id: 3, title: 'Late data', response: 'reduce', response_plan: 'Agree a data date' },
    ],
    plan_version_seen: 4,
    plan_changed: false,
    flags: [],
    latest_version: null,
    submitted_by: null,
    submitted_by_name: null,
    approved_version: null,
    approval_current: false,
    version: 2,
    updated_at: '2026-11-05T09:00:00',
    ...changes,
  }
}

function version(number: number, kinds: string[] = []): ProposalVersion {
  const { summary, scope_in, scope_out, assumptions, terms, price } = proposal()
  const { estimate_lines, milestones, deliverables, risks } = proposal()
  return {
    id: number,
    number,
    content: {
      summary,
      scope_in,
      scope_out,
      assumptions,
      terms,
      price,
      estimate_lines,
      milestones,
      deliverables,
      risks,
      project_name: 'Forecast',
      client_name: 'Northwind Example',
    },
    submitted_by: PM_ID,
    submitted_by_name: 'Pat Manager',
    submitted_at: '2026-11-05T09:00:00',
    events: kinds.map((kind, index) => ({
      id: index + 1,
      kind: kind as ProposalVersion['events'][number]['kind'],
      actor_user_id: USER_ID,
      actor_name: 'Sam Sponsor',
      reason: kind === 'approve' ? REASON : null,
      sent_to: kind === 'sent' ? 'Dana' : null,
      sent_on: kind === 'sent' ? '2026-11-05' : null,
      means: kind === 'sent' ? 'Email' : null,
      answer: null,
      answered_on: null,
      note: null,
      created_at: '2026-11-05T10:00:00',
    })),
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface FakeOptions {
  stored?: Proposal | null
  versions?: ProposalVersion[]
  // What a POST or PATCH answers: the new proposal, or an error.
  answer?: Proposal | { status: number; detail: unknown }
  // At most this many versions per page, whatever limit is asked for.
  pageSize?: number
}

function fakeApi(options: FakeOptions = {}) {
  const stored = options.stored === undefined ? proposal() : options.stored
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input, 'http://localhost')
    const method = init.method ?? 'GET'
    if (method !== 'GET') {
      const answer = options.answer ?? proposal()
      if ('detail' in answer) return json({ detail: answer.detail }, answer.status)
      return json(answer)
    }
    if (url.pathname === '/api/v1/projects/5/proposal') {
      return stored ? json(stored) : json({ detail: 'this project has no proposal yet' }, 404)
    }
    if (url.pathname === '/api/v1/projects/5/proposal/versions') {
      const all = options.versions ?? []
      const offset = Number(url.searchParams.get('offset') ?? 0)
      const limit = Math.min(Number(url.searchParams.get('limit') ?? 100), options.pageSize ?? 100)
      return json({ items: all.slice(offset, offset + limit), total: all.length, limit, offset })
    }
    return json({ detail: `unexpected ${method} ${url.pathname}` }, 404)
  })
}

async function mountAs(role: string, options: FakeOptions = {}, clientName: string | null = 'Northwind Example') {
  const fetchMock = fakeApi(options)
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
  const router = makeRouter(true)
  const wrapper = mount(ProposalSection, {
    props: { projectId: 5, clientName },
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { wrapper, fetchMock }
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

function posts(fetchMock: ReturnType<typeof fakeApi>) {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method && init.method !== 'GET')
    .map(([input, init]) => ({
      path: new URL(String(input), 'http://localhost').pathname,
      method: init?.method,
      body: init?.body ? JSON.parse(String(init.body)) : null,
    }))
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the proposal helpers', () => {
  const can = (role: string) => (action: string) => ACTIONS[role].includes(action)

  it('shows each role only its buttons for the status', () => {
    const draft = proposal()
    expect(buttonsFor(draft, can('project_manager'), PM_ID)).toEqual({
      edit: true,
      submit: true,
      decide: false,
      sent: false,
      answer: false,
      withdraw: true,
    })
    const submitted = proposal({ status: 'submitted', submitted_by: PM_ID })
    expect(buttonsFor(submitted, can('sponsor'), USER_ID).decide).toBe(true)
    // Nobody decides on their own submission.
    expect(buttonsFor(submitted, can('sponsor'), PM_ID).decide).toBe(false)
    for (const role of ['project_manager', 'auditor', 'ethics_reviewer', 'admin']) {
      expect(buttonsFor(submitted, can(role), USER_ID).decide).toBe(false)
    }
    const approved = proposal({ status: 'approved' })
    expect(buttonsFor(approved, can('project_manager'), PM_ID).sent).toBe(true)
    expect(buttonsFor(approved, can('sponsor'), USER_ID).sent).toBe(false)
    expect(buttonsFor(proposal({ status: 'sent' }), can('project_manager'), PM_ID).answer).toBe(true)
    expect(Object.values(buttonsFor(proposal({ status: 'accepted' }), can('project_manager'), PM_ID))).toEqual(
      [false, false, false, false, false, false],
    )
  })

  it.each([
    ['draft', true],
    ['submitted', true],
    ['approved', true],
    ['sent', true],
    ['accepted', false],
    ['declined', false],
    ['withdrawn', false],
  ])('offers withdraw on a %s proposal: %s', (status, shown) => {
    const row = proposal({ status: status as Proposal['status'] })
    expect(buttonsFor(row, can('project_manager'), PM_ID).withdraw).toBe(shown)
    expect(buttonsFor(row, can('admin'), PM_ID).withdraw).toBe(false)
  })

  it('says when an approval was for an earlier version', () => {
    expect(approvalText(proposal())).toBeNull()
    expect(approvalText(proposal({ approved_version: 2, approval_current: true }))).toBe(
      'Approved as version 2.',
    )
    expect(approvalText(proposal({ approved_version: 1 }))).toContain('an earlier version')
    const withdrawn = approvalText(proposal({ status: 'withdrawn', approved_version: 1 }))
    expect(withdrawn).toContain('was withdrawn')
    expect(withdrawn).not.toContain('changed')
  })

  it('says why an approved copy is out of date, and only when it is', () => {
    expect(outOfDateText(proposal())).toBeNull()
    expect(
      outOfDateText(proposal({ status: 'sent', approved_version: 1, approval_current: true })),
    ).toBeNull()
    expect(outOfDateText(proposal({ approved_version: 1 }))).toContain('edited since')
    const withdrawn = outOfDateText(proposal({ status: 'withdrawn', approved_version: 1 }))
    expect(withdrawn).toContain('withdrawn')
    expect(withdrawn).toContain('must not be sent')
  })

  it('finds the flags of a section and the approved copy', () => {
    const flags = [
      { rule: 'stray_money', section: 'summary', text: '$99' },
      { rule: 'stale_price', section: 'price', text: 'old' },
    ]
    expect(flagsFor(flags, 'summary')).toEqual([flags[0]])
    expect(approvedCopy([version(3), version(2, ['approve']), version(1, ['approve'])])?.number).toBe(2)
    expect(approvedCopy([version(1, ['return'])])).toBeNull()
  })
})

describe('the proposal section', () => {
  it('shows the sections, what came from the plan and the risks without a score', async () => {
    await mountAs('project_manager')
    expect(find('proposal-status')?.textContent).toContain('Draft')
    expect(find('proposal-section-summary')?.textContent).toContain('A pilot for 1650.00')
    expect(find('proposal-price')?.textContent).toBe('1650.00')
    expect(find('proposal-section-milestones')?.textContent).toContain('2027-03-01: Pilot')
    expect(find('proposal-section-deliverables')?.textContent).toContain('Forecast model')
    expect(findAll('proposal-risk').map((item) => item.textContent?.trim())).toEqual([
      'Late data: Reduce. Agree a data date',
    ])
    expect(find('proposal')?.textContent).not.toMatch(/score|band/i)
    expect(find('proposal-clean')).not.toBeNull()
    expect(find('proposal-no-versions')).not.toBeNull()
  })

  it('shows each flag beside the text it concerns', async () => {
    const flags = [
      { rule: 'stray_date', section: 'terms', text: '2027-02-01' },
      { rule: 'stale_price', section: 'price', text: 'The proposal’s price 1650.00 is not 1760.00' },
    ]
    await mountAs('project_manager', { stored: proposal({ flags, plan_changed: true }) })
    expect(find('proposal-flags')?.textContent).toContain('2 open')
    expect(find('proposal-plan-changed')).not.toBeNull()
    const terms = find('proposal-section-terms')
    expect(terms?.querySelector('[data-test="proposal-flag"]')?.textContent).toContain('2027-02-01')
    const price = find('proposal-section-price')
    expect(price?.querySelector('[data-test="proposal-flag"]')?.textContent).toContain('1760.00')
    expect(find('proposal-section-summary')?.querySelector('[data-test="proposal-flag"]')).toBeNull()
  })

  it('names the flags when the API refuses a submit', async () => {
    const { fetchMock } = await mountAs('project_manager', {
      answer: {
        status: 409,
        detail: {
          message: "the proposal can't be submitted while the checker has flags open",
          flags: [{ rule: 'stray_money', section: 'summary', text: '$99' }],
        },
      },
    })
    await click('proposal-submit')
    expect(posts(fetchMock)).toEqual([
      { path: '/api/v1/projects/5/proposal/submit', method: 'POST', body: { version: 2 } },
    ])
    const error = find('proposal-action-error')?.textContent ?? ''
    expect(error).toContain('flags open')
    expect(error).toContain('summary: $99')
  })

  it.each([
    ['project_manager', ['proposal-edit', 'proposal-refresh', 'proposal-submit', 'proposal-withdraw']],
    ['sponsor', []],
    ['auditor', []],
    ['ethics_reviewer', []],
    ['admin', []],
  ])('shows a %s the buttons for a draft', async (role, shown) => {
    await mountAs(role)
    const buttons = Array.from(find('proposal-buttons')?.querySelectorAll('[data-test]') ?? [])
    expect(buttons.map((button) => button.getAttribute('data-test'))).toEqual(shown)
  })

  it.each([
    ['sponsor', ['proposal-approve', 'proposal-return']],
    ['project_manager', ['proposal-edit', 'proposal-refresh', 'proposal-withdraw']],
    ['auditor', []],
    ['ethics_reviewer', []],
  ])('shows a %s the buttons for a submitted proposal', async (role, shown) => {
    await mountAs(role, {
      stored: proposal({ status: 'submitted', latest_version: 1, submitted_by: PM_ID }),
      versions: [version(1)],
    })
    const buttons = Array.from(find('proposal-buttons')?.querySelectorAll('[data-test]') ?? [])
    expect(buttons.map((button) => button.getAttribute('data-test'))).toEqual(shown)
  })

  it('lets the sponsor approve with a reason', async () => {
    const { fetchMock } = await mountAs('sponsor', {
      stored: proposal({ status: 'submitted', latest_version: 1, submitted_by: PM_ID }),
      versions: [version(1)],
      answer: proposal({ status: 'approved', approved_version: 1, approval_current: true }),
    })
    await click('proposal-approve')
    await type(find('proposal-reason'), REASON)
    await click('proposal-confirm')
    expect(posts(fetchMock)).toEqual([
      {
        path: '/api/v1/projects/5/proposal/approve',
        method: 'POST',
        body: { version: 2, reason: REASON },
      },
    ])
    expect(find('proposal-status')?.textContent).toContain('Approved')
    expect(find('proposal-approval')?.textContent).toContain('Approved as version 1')
  })

  it('records sending with who, when and how, and says nothing is sent', async () => {
    const { fetchMock } = await mountAs('project_manager', {
      stored: proposal({ status: 'approved', latest_version: 1, approved_version: 1, approval_current: true }),
      versions: [version(1, ['approve'])],
      answer: proposal({ status: 'sent' }),
    })
    expect(find('proposal-print-link')).not.toBeNull()
    await click('proposal-sent')
    expect(find('proposal-sent-form')?.textContent).toContain('Nothing is sent from here')
    await type(find('proposal-sent-to'), 'Dana, Northwind buyer')
    await type(find('proposal-sent-on'), '2026-11-05')
    await type(find('proposal-sent-means'), 'Email')
    await click('proposal-confirm')
    expect(posts(fetchMock)).toEqual([
      {
        path: '/api/v1/projects/5/proposal/sent',
        method: 'POST',
        body: { version: 2, sent_to: 'Dana, Northwind buyer', sent_on: '2026-11-05', means: 'Email' },
      },
    ])
  })

  it('shows the version history with what people did', async () => {
    await mountAs('auditor', {
      stored: proposal({ status: 'sent', latest_version: 2 }),
      versions: [version(2, ['approve', 'sent']), version(1)],
    })
    expect(findAll('proposal-version')).toHaveLength(2)
    expect(findAll('proposal-event').map((item) => item.textContent?.trim())).toEqual([
      `Approved by Sam Sponsor: ${REASON}`,
      'Sent by Sam Sponsor to Dana on 2026-11-05 by Email',
    ])
  })

  it('offers to create a proposal when there is none, and saves only changed text', async () => {
    const { fetchMock } = await mountAs('project_manager', { stored: null })
    expect(find('proposal-none')).not.toBeNull()
    await click('proposal-create')
    expect(posts(fetchMock)[0]).toEqual({
      path: '/api/v1/projects/5/proposal',
      method: 'POST',
      body: null,
    })
    await click('proposal-edit')
    const forms = find('proposal-date-forms')?.textContent ?? ''
    for (const form of ['2027-01-15', '15 January 2027', 'January 15, 2027', 'not read']) {
      expect(forms).toContain(form)
    }
    await type(find('proposal-field-scope_out'), 'Stores')
    await click('proposal-save')
    expect(posts(fetchMock)[1]).toEqual({
      path: '/api/v1/projects/5/proposal',
      method: 'PATCH',
      body: { version: 2, scope_out: 'Stores' },
    })
  })

  it('says a project with no client has no proposal, and asks nothing', async () => {
    const { fetchMock } = await mountAs('project_manager', {}, null)
    expect(find('proposal-no-client')).not.toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('the print view', () => {
  async function mountPrint(
    versions: ProposalVersion[],
    stored: Proposal = proposal({
      status: 'approved',
      latest_version: 1,
      approved_version: 1,
      approval_current: true,
    }),
    pageSize?: number,
  ) {
    vi.stubGlobal('fetch', fakeApi({ versions, stored, pageSize }))
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAuthStore()
    auth.user = { id: USER_ID, email: 'a@pmo.example', display_name: 'A', role: 'auditor', actions: ACTIONS.auditor }
    auth.checked = true
    const router = makeRouter(true)
    router.push('/projects/5/proposal/print')
    await router.isReady()
    mount(ProposalPrintView, {
      global: { plugins: [pinia, router, makeVuetify()] },
      attachTo: document.body,
    })
    await flushPromises()
  }

  it('shows the approved version, not a later draft', async () => {
    const later = version(2)
    later.content = { ...later.content, price: '9999.00' }
    await mountPrint([later, version(1, ['approve'])])
    expect(find('proposal-print-title')?.textContent).toContain('Forecast')
    expect(find('proposal-print-price')?.textContent).toBe('1650.00')
    expect(findAll('proposal-print-risk').map((item) => item.textContent?.trim())).toEqual([
      'Late data: Reduce. Agree a data date',
    ])
    expect(find('proposal-print')?.textContent).not.toMatch(/score|band/i)
  })

  it('reads every page of versions to find the approved copy', async () => {
    const versions = [5, 4, 3, 2].map((number) => version(number))
    await mountPrint([...versions, version(1, ['approve'])], undefined, 2)
    expect(find('proposal-print-title')).not.toBeNull()
    expect(find('proposal-print')?.textContent).toContain('Version 1.')
  })

  it('shows no warning while the approved copy is current', async () => {
    await mountPrint([version(1, ['approve'])])
    expect(find('proposal-print-out-of-date')).toBeNull()
  })

  it('warns at the top when the proposal was edited after approval', async () => {
    await mountPrint(
      [version(2), version(1, ['approve'])],
      proposal({ status: 'draft', latest_version: 2, approved_version: 1 }),
    )
    const warning = find('proposal-print-out-of-date')
    expect(warning?.textContent).toContain('out of date')
    expect(warning?.textContent).toContain('must not be sent')
    expect(warning?.textContent).toContain('edited since')
    const article = find('proposal-print')?.querySelector('article')
    expect(article?.firstElementChild).toBe(warning)
  })

  it('warns at the top when the approved proposal was withdrawn', async () => {
    await mountPrint(
      [version(1, ['approve', 'withdraw'])],
      proposal({ status: 'withdrawn', latest_version: 1, approved_version: 1 }),
    )
    const warning = find('proposal-print-out-of-date')
    expect(warning?.textContent).toContain('was withdrawn')
    expect(warning?.textContent).toContain('must not be sent')
  })

  it('says when nothing has been approved', async () => {
    await mountPrint([version(1, ['return'])])
    expect(find('proposal-print-none')).not.toBeNull()
  })
})
