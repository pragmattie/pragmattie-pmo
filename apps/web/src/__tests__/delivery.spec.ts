import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DeliverySection from '../components/DeliverySection.vue'
import { daysLateText, type Delivery, type SpendEntry } from '../lib/delivery'
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

function entry(changes: Partial<SpendEntry> = {}): SpendEntry {
  return {
    id: 1,
    amount: '250.00',
    as_of: '2027-04-30',
    note: 'Finance report, April',
    supersedes_id: null,
    created_by: 7,
    created_by_name: 'Pat Manager',
    created_at: '2027-05-01T09:00:00',
    ...changes,
  }
}

function delivery(changes: Partial<Delivery> = {}): Delivery {
  const spend = [entry({ id: 2, amount: '240.00', supersedes_id: 1 }), entry()]
  return {
    project_id: 5,
    project_stage: 'delivery',
    project_status: 'on_track',
    baseline_number: 1,
    baseline_total: '1000.00',
    today: '2027-06-01',
    milestones: [
      {
        id: 11,
        name: 'Pilot live',
        target_date: '2027-03-01',
        plan_date: '2027-03-15',
        reached_on: '2027-03-03',
        reached_note: 'Signed off',
        days_late: 2,
        version: 2,
      },
      {
        id: 12,
        name: 'Handover',
        target_date: null,
        plan_date: '2027-07-01',
        reached_on: null,
        reached_note: null,
        days_late: null,
        version: 1,
      },
    ],
    deliverables: [
      { id: 21, name: 'Model', milestone_id: 11, status: 'in_progress', version: 3 },
      { id: 22, name: 'Report', milestone_id: 11, status: 'accepted', version: 4 },
    ],
    current_spend: spend[0],
    spend: { items: spend, total: 2, limit: 50, offset: 0 },
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

async function mountSection(
  role: string,
  found: Delivery = delivery(),
  sent: Sent[] = [],
  answer?: (path: string) => Response | null,
) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const path = new URL(input, 'http://localhost').pathname
      const method = init.method ?? 'GET'
      if (method !== 'GET') sent.push({ method, path, body: JSON.parse(String(init.body)) })
      return answer?.(path) ?? json(found, method === 'POST' ? 201 : 200)
    }),
  )
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = {
    id: 7,
    email: `${role}@pmo.example`,
    display_name: 'Test User',
    role,
    actions: ACTIONS[role],
  }
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

describe('daysLateText', () => {
  it('says late, early, on time, not due or no baseline target', () => {
    const at = { target_date: '2027-03-01', reached_on: '2027-03-01' }
    expect(daysLateText({ ...at, days_late: 2 })).toBe('2 working days late')
    expect(daysLateText({ ...at, days_late: 1 })).toBe('1 working day late')
    expect(daysLateText({ ...at, days_late: -1 })).toBe('1 working day early')
    expect(daysLateText({ ...at, days_late: 0 })).toBe('On time')
    expect(daysLateText({ ...at, reached_on: null, days_late: 0 })).toBe('Not due yet')
    expect(daysLateText({ target_date: null, reached_on: null, days_late: null })).toBe(
      'No baseline target',
    )
  })
})

describe('the Delivery section', () => {
  it('shows milestones against the baseline, deliverable status and spend with its history', async () => {
    await mountSection('auditor')
    const milestones = findAll('delivery-milestone')
    expect(milestones).toHaveLength(2)
    expect(milestones[0].textContent).toContain('Plan date now 2027-03-15')
    expect(milestones[0].querySelector('[data-test="milestone-target"]')?.textContent).toContain(
      '2027-03-01',
    )
    expect(milestones[0].querySelector('[data-test="milestone-reached"]')?.textContent).toContain(
      'Signed off',
    )
    expect(milestones[0].querySelector('[data-test="milestone-days-late"]')?.textContent).toContain(
      '2 working days late',
    )
    expect(milestones[1].querySelector('[data-test="milestone-target"]')?.textContent).toContain(
      'No baseline target',
    )
    const statuses = findAll('deliverable-status').map((s) => s.textContent?.trim())
    expect(statuses).toEqual(['In progress', 'Accepted'])
    expect(find('spend-current')?.textContent).toContain('240.00')
    expect(find('spend-baseline')?.textContent).toContain('1,000.00')
    expect(findAll('spend-entry')).toHaveLength(2)
    expect(find('spend-corrects')?.textContent).toContain('corrects entry 1')
  })

  it.each(['admin', 'sponsor', 'ethics_reviewer', 'auditor'])('%s sees no actions', async (role) => {
    await mountSection(role)
    expect(find('milestone-reach')).toBeNull()
    expect(find('milestone-clear')).toBeNull()
    expect(find('deliverable-set-not_started')).toBeNull()
    expect(find('spend-add')).toBeNull()
  })

  it('hides every action once the project is cancelled', async () => {
    await mountSection('project_manager', delivery({ project_status: 'cancelled' }))
    expect(find('milestone-reach')).toBeNull()
    expect(find('spend-add')).toBeNull()
  })

  it('never offers accepted, and leaves an accepted deliverable alone', async () => {
    await mountSection('project_manager')
    const rows = findAll('delivery-deliverable')
    const offered = [...rows[0].querySelectorAll('[data-test^="deliverable-set-"]')].map((b) =>
      b.getAttribute('data-test'),
    )
    expect(offered).toEqual(['deliverable-set-not_started', 'deliverable-set-ready_for_acceptance'])
    expect(rows[1].querySelector('[data-test^="deliverable-set-"]')).toBeNull()
    expect(find('deliverable-set-accepted')).toBeNull()
  })

  it('moves a deliverable with the version it read', async () => {
    const sent: Sent[] = []
    await mountSection('project_manager', delivery(), sent)
    await click('deliverable-set-ready_for_acceptance')
    expect(sent).toEqual([
      {
        method: 'PUT',
        path: '/api/v1/projects/5/delivery/deliverables/21',
        body: { version: 3, status: 'ready_for_acceptance' },
      },
    ])
    expect(find('delivery-message')?.textContent).toContain('Ready for acceptance')
  })

  it('marks a milestone reached and clears one', async () => {
    const sent: Sent[] = []
    await mountSection('project_manager', delivery(), sent)
    await click('milestone-clear')
    await click('milestone-reach')
    await type('reach-date', '2027-05-28')
    await type('reach-note', 'Client signed it off')
    await click('reach-save')
    expect(sent).toEqual([
      {
        method: 'PUT',
        path: '/api/v1/projects/5/delivery/milestones/11',
        body: { version: 2, reached_on: null, reached_note: null },
      },
      {
        method: 'PUT',
        path: '/api/v1/projects/5/delivery/milestones/12',
        body: { version: 1, reached_on: '2027-05-28', reached_note: 'Client signed it off' },
      },
    ])
    expect(find('reach-form')).toBeNull()
  })

  it('records spend, and a correction names the newest entry', async () => {
    const sent: Sent[] = []
    await mountSection('project_manager', delivery(), sent)
    await click('spend-add')
    await type('spend-amount', '300.5')
    await type('spend-note', 'Finance report, May')
    await click('spend-save')
    await click('spend-correct')
    await type('spend-amount', '235.00')
    await type('spend-note', 'Finance corrected April')
    await click('spend-save')
    expect(sent).toEqual([
      {
        method: 'POST',
        path: '/api/v1/projects/5/delivery/spend',
        body: { amount: '300.5', as_of: '2027-06-01', note: 'Finance report, May' },
      },
      {
        method: 'POST',
        path: '/api/v1/projects/5/delivery/spend',
        body: {
          amount: '235.00',
          as_of: '2027-04-30',
          note: 'Finance corrected April',
          supersedes_id: 2,
        },
      },
    ])
  })

  it('shows the API refusal and reloads', async () => {
    const answer = (path: string) =>
      path.endsWith('/spend')
        ? json(
            {
              detail:
                "a correction must name the project's newest spend entry; reload the spend and try again",
            },
            409,
          )
        : null
    await mountSection('project_manager', delivery(), [], answer)
    await click('spend-correct')
    await type('spend-note', 'Late correction')
    await click('spend-save')
    expect(find('delivery-message')?.textContent).toContain('newest spend entry')
    expect(find('spend-form')).not.toBeNull()
  })
})
