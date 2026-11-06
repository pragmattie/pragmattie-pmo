import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ChangesSection from '../components/ChangesSection.vue'
import {
  canDecideChange,
  canWithdrawChange,
  type ChangeEffect,
  type ChangeLog,
  type ChangeRequest,
  changeSetFrom,
  deciderText,
  effectText,
  emptyChangeForm,
} from '../lib/changes'
import type { Baseline } from '../lib/gates'
import { makeVuetify } from '../plugins/vuetify'
import { useAuthStore } from '../stores/auth'

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
const ROLES = Object.keys(ACTIONS)

// The project's project manager; the raiser is another project manager.
const PM_ID = 7
const RAISER_ID = 8
const REASON = 'Agreed with the client in the weekly review'

function effect(changes: Partial<ChangeEffect> = {}): ChangeEffect {
  return {
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
    ...changes,
  }
}

function change(changes: Partial<ChangeRequest> = {}): ChangeRequest {
  return {
    id: 1,
    project_id: 5,
    title: 'More analysis',
    reason: 'The client asked for it',
    changes: { add_lines: [{ description: 'More', category: 'people', amount: '50.00' }] },
    effect: effect(),
    decider: 'project_manager',
    measured_baseline_number: 1,
    status: 'submitted',
    raised_by: RAISER_ID,
    raised_by_name: 'Raiser',
    submitted_at: '2026-11-05T09:00:00',
    decided_by: null,
    decided_by_name: null,
    decision_reason: null,
    decided_at: null,
    baseline_number: null,
    version: 2,
    created_at: '2026-11-05T09:00:00',
    ...changes,
  }
}

function changeLog(items: ChangeRequest[], changes: Partial<ChangeLog> = {}): ChangeLog {
  return {
    items,
    total: items.length,
    limit: 100,
    offset: 0,
    project_stage: 'delivery',
    project_status: 'on_track',
    pm_user_id: PM_ID,
    current_baseline_number: 1,
    proposal_out_of_date: false,
    ...changes,
  }
}

const BASELINE: Baseline = {
  id: 1,
  project_id: 5,
  number: 1,
  gate_decision_id: 3,
  change_request_id: null,
  plan: {
    scope_in: 'Pilot stores',
    scope_out: 'Other stores',
    milestones: [{ id: 1, name: 'Pilot live', target_date: '2027-03-01', owner_user_id: PM_ID }],
    deliverables: [{ id: 1, name: 'Forecast model', milestone_name: 'Pilot live' }],
    estimate_lines: [{ id: 1, description: 'Analysts', category: 'people', amount: '1000.00' }],
    contingency_pct: 0,
    lines_total: '1000.00',
    contingency_amount: '0.00',
    total: '1000.00',
  },
  total: '1000.00',
  created_at: '2026-11-04T09:00:00',
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

function fakeApi(found: ChangeLog, sent: Sent[], answer?: (path: string) => Response | null) {
  return vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init.method ?? 'GET'
    if (method !== 'GET') sent.push({ method, path, body: JSON.parse(String(init.body)) })
    const given = answer?.(path)
    if (given) return given
    if (path === '/api/v1/projects/5/changes' && method === 'GET') return json(found)
    if (path === '/api/v1/projects/5/changes' && method === 'POST') return json(change({ status: 'draft' }), 201)
    if (path.endsWith('/decide')) {
      const body = JSON.parse(String(init.body))
      return json(
        change({
          status: body.outcome === 'approve' ? 'approved' : 'rejected',
          baseline_number: body.outcome === 'approve' ? 2 : null,
        }),
      )
    }
    return json(change())
  })
}

async function mountSection(
  role: string,
  userId: number,
  found: ChangeLog,
  sent: Sent[] = [],
  answer?: (path: string) => Response | null,
) {
  vi.stubGlobal('fetch', fakeApi(found, sent, answer))
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = {
    id: userId,
    email: `${role}@pmo.example`,
    display_name: 'Test User',
    role,
    actions: ACTIONS[role],
  }
  const wrapper = mount(ChangesSection, {
    props: { projectId: 5, baseline: BASELINE },
    global: { plugins: [pinia, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
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

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the change helpers', () => {
  it('words the effect from what the API computed', () => {
    expect(effectText(effect())).toBe('Cost +50.00 (+5.00%); no scope change')
    expect(
      effectText(
        effect({
          cost_difference: '-400.00',
          cost_difference_pct: '-40.00',
          max_later_working_days: 6,
          scope_changes: true,
        }),
      ),
    ).toBe('Cost -400.00 (-40.00%); up to 6 working days later; changes scope')
    expect(effectText(effect({ cost_difference_pct: null }))).toBe('Cost +50.00; no scope change')
  })

  it('says who decides and why', () => {
    expect(deciderText(change())).toBe('Project manager: within their limits')
    expect(
      deciderText(
        change({ decider: 'sponsor', effect: effect({ escalations: ['scope_change', 'dates_above_limit'] }) }),
      ),
    ).toBe("Sponsor: it changes scope; a milestone moves later than the project manager's limit")
  })

  it('builds a change set from the form, never with an effect or decider', () => {
    expect(changeSetFrom(emptyChangeForm())).toEqual({})
    const form = {
      ...emptyChangeForm(),
      addLineDescription: ' More analysis ',
      addLineAmount: '50.00',
      moveMilestone: 1,
      moveTo: '2027-03-09',
      removeDeliverables: [1],
      contingency: '5',
    }
    const set = changeSetFrom(form)
    expect(set).toEqual({
      add_lines: [{ description: 'More analysis', category: 'people', amount: '50.00' }],
      move_milestones: [{ milestone_id: 1, target_date: '2027-03-09' }],
      remove_deliverables: [1],
      contingency_pct: 5,
    })
    expect(set).not.toHaveProperty('effect')
    expect(set).not.toHaveProperty('decider')
  })

  it('lets only the named decider decide, never on their own change', () => {
    const pm = { id: PM_ID, role: 'project_manager', actions: ACTIONS.project_manager }
    const raiser = { id: RAISER_ID, role: 'project_manager', actions: ACTIONS.project_manager }
    const sponsor = { id: 9, role: 'sponsor', actions: ACTIONS.sponsor }
    const ethics = { id: 10, role: 'ethics_reviewer', actions: ACTIONS.ethics_reviewer }
    const small = change()
    const large = change({ decider: 'sponsor' })
    expect(canDecideChange(small, pm, PM_ID)).toBe(true)
    expect(canDecideChange(small, raiser, PM_ID)).toBe(false)
    expect(canDecideChange(small, sponsor, PM_ID)).toBe(false)
    expect(canDecideChange(large, sponsor, PM_ID)).toBe(true)
    expect(canDecideChange(large, sponsor, sponsor.id)).toBe(false)
    expect(canDecideChange(large, pm, PM_ID)).toBe(false)
    expect(canDecideChange(large, ethics, PM_ID)).toBe(false)
    expect(canDecideChange(change({ status: 'draft' }), pm, PM_ID)).toBe(false)
    expect(canDecideChange(change({ raised_by: PM_ID, decider: 'sponsor' }), pm, PM_ID)).toBe(false)
    expect(canWithdrawChange(small, raiser, PM_ID)).toBe(true)
    expect(canWithdrawChange(small, pm, PM_ID)).toBe(true)
    expect(canWithdrawChange(small, { ...raiser, id: 99 }, PM_ID)).toBe(false)
    expect(canWithdrawChange(change({ status: 'approved' }), raiser, PM_ID)).toBe(false)
  })
})

// For each role: who sees Raise, and who sees Approve on a project-manager-level and a
// sponsor-level change (the sponsor and the PM are not the raiser).
const USERS: Record<string, number> = {
  admin: 1,
  sponsor: 2,
  project_manager: PM_ID,
  ethics_reviewer: 3,
  auditor: 4,
}
const EXPECTED: Record<string, { raise: boolean; pmLevel: boolean; sponsorLevel: boolean }> = {
  admin: { raise: false, pmLevel: false, sponsorLevel: false },
  sponsor: { raise: false, pmLevel: false, sponsorLevel: true },
  project_manager: { raise: true, pmLevel: true, sponsorLevel: false },
  ethics_reviewer: { raise: false, pmLevel: false, sponsorLevel: false },
  auditor: { raise: false, pmLevel: false, sponsorLevel: false },
}

describe('the Changes section, for each role', () => {
  it.each(ROLES)('%s sees the change log and only the actions it may take', async (role) => {
    const expected = EXPECTED[role]
    await mountSection(role, USERS[role], changeLog([change()]))
    expect(find('change-log')).not.toBeNull()
    expect(find('change-effect')?.textContent).toContain('Cost +50.00 (+5.00%)')
    expect(find('change-decider')?.textContent).toContain('Project manager')
    expect(!!find('change-raise')).toBe(expected.raise)
    expect(!!find('change-approve')).toBe(expected.pmLevel)
    document.body.innerHTML = ''
    await mountSection(role, USERS[role], changeLog([change({ decider: 'sponsor' })]))
    expect(!!find('change-approve')).toBe(expected.sponsorLevel)
    expect(!!find('change-reject')).toBe(expected.sponsorLevel)
  })
})

describe('the Changes section', () => {
  it('shows a decided change with who decided, the reason and the baseline it made', async () => {
    const decided = change({
      status: 'approved',
      decided_by: PM_ID,
      decided_by_name: 'Pat Manager',
      decision_reason: REASON,
      decided_at: '2026-11-06T09:00:00',
      baseline_number: 2,
    })
    await mountSection('auditor', 4, changeLog([decided], { current_baseline_number: 2 }))
    expect(find('change-decision')?.textContent).toContain('Approved by Pat Manager')
    expect(find('change-decision')?.textContent).toContain(REASON)
    expect(find('change-baseline')?.textContent).toContain('Made baseline 2')
    expect(find('change-approve')).toBeNull()
  })

  it('says when the proposal is out of date against the new baseline', async () => {
    await mountSection('auditor', 4, changeLog([], { proposal_out_of_date: true, current_baseline_number: 2 }))
    expect(find('proposal-out-of-date')?.textContent).toContain('baseline 2')
    expect(find('changes-empty')).not.toBeNull()
  })

  it('hides every action once the project is not in delivery', async () => {
    await mountSection('project_manager', PM_ID, changeLog([change()], { project_stage: 'planning' }))
    expect(find('change-raise')).toBeNull()
    expect(find('change-approve')).toBeNull()
    expect(find('change-withdraw')).toBeNull()
  })

  it('raises a change with only the change set: the API computes the effect', async () => {
    const sent: Sent[] = []
    await mountSection('project_manager', RAISER_ID, changeLog([]), sent)
    await click('change-raise')
    await type('change-title', 'More analysis')
    await type('change-reason', 'The client asked for it')
    await type('change-add-line', 'More analysis')
    await type('change-add-amount', '50.00')
    await click('change-save')
    expect(sent).toEqual([
      {
        method: 'POST',
        path: '/api/v1/projects/5/changes',
        body: {
          title: 'More analysis',
          reason: 'The client asked for it',
          changes: {
            add_lines: [{ description: 'More analysis', category: 'people', amount: '50.00' }],
          },
        },
      },
    ])
    expect(find('changes-message')?.textContent).toContain('raised as a draft')
  })

  it('lets the project manager approve with a reason, then asks the page to reload', async () => {
    const sent: Sent[] = []
    const wrapper = await mountSection('project_manager', PM_ID, changeLog([change()]), sent)
    await click('change-approve')
    expect((find('change-confirm') as HTMLButtonElement).disabled).toBe(true)
    await type('change-decision-reason', REASON)
    await click('change-confirm')
    expect(sent).toEqual([
      {
        method: 'POST',
        path: '/api/v1/projects/5/changes/1/decide',
        body: { version: 2, outcome: 'approve', reason: REASON },
      },
    ])
    expect(wrapper.emitted('decided')?.[0]).toEqual(['Change approved: the plan is now baseline 2.'])
  })

  it("shows the API's refusal when a decision is refused", async () => {
    await mountSection('project_manager', PM_ID, changeLog([change()]), [], (path) =>
      path.endsWith('/decide')
        ? json({ detail: 'a newer baseline has been made since this change was measured' }, 409)
        : null,
    )
    await click('change-approve')
    await type('change-decision-reason', REASON)
    await click('change-confirm')
    expect(find('changes-message')?.textContent).toContain('a newer baseline')
  })

  it('offers a resubmit when the change was measured on an older baseline', async () => {
    await mountSection('project_manager', RAISER_ID, changeLog([change()], { current_baseline_number: 2 }))
    expect(find('change-resubmit')).not.toBeNull()
  })
})
