import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  type Benefit,
  benefitStanding,
  changedSections,
  type Closure,
  type ClosureReport,
  lessonsOpen,
  reportOpen,
} from '../lib/closure'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

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

function benefit(changes: Partial<Benefit> = {}): Benefit {
  return {
    benefit_id: 41,
    position: 0,
    description: 'Faster planning',
    measure: 'Days to plan',
    value_before: 'ten days',
    target: 'two days',
    target_date: '2027-09-01',
    value_now: null,
    value_now_on: null,
    measure_owner_id: null,
    measure_owner_name: null,
    measure_by: null,
    version: 0,
    ...changes,
  }
}

function report(changes: Partial<ClosureReport> = {}): ClosureReport {
  return {
    id: 9,
    project_id: 5,
    figures: [
      {
        key: 'final_baseline_total',
        label: 'Final baseline total',
        kind: 'money',
        value: '1200.00',
        note: 'The total of the last baseline',
        sources: [{ table: 'project_baselines', id: 3, field: 'total' }],
      },
      {
        key: 'accepted_risks',
        label: 'Accepted risks still standing',
        kind: 'count',
        value: 1,
        note: 'Accepted and not closed',
        sources: [{ table: 'risks', id: 12, field: 'status' }],
      },
    ],
    figures_at: '2027-06-01T09:00:00',
    sections: { delivered: 'The pilot.', value_case: null, remaining: null },
    section_states: {
      delivered: { source: 'agent', reviewed_by: null, reviewed_at: null, unreviewed: true },
      value_case: { source: null, reviewed_by: null, reviewed_at: null, unreviewed: false },
      remaining: { source: null, reviewed_by: null, reviewed_at: null, unreviewed: false },
    },
    flags: [],
    frozen_at: null,
    version: 4,
    created_at: '2027-06-01T09:00:00',
    ...changes,
  }
}

function closure(changes: Partial<Closure> = {}): Closure {
  return {
    project_id: 5,
    project_name: 'Warehouse demand forecast',
    project_stage: 'closure',
    project_status: 'on_track',
    report: report(),
    benefits: [benefit()],
    lessons: [
      {
        id: 3,
        project_id: 5,
        category: 'scope',
        what_happened: 'The regions moved twice.',
        do_again: null,
        do_differently: 'Agree regions first.',
        suggest_as_risk: false,
        source: 'agent',
        status: 'suggested',
        reviewed_by: null,
        reviewed_at: null,
        version: 1,
        created_at: '2027-06-01T09:00:00',
      },
    ],
    accepted_risks: [{ id: 12, title: 'Key analyst leaves', band: 'Low' }],
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

async function mountAt(found: Closure, role = 'project_manager') {
  const sent: Sent[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input, 'http://localhost')
      const method = init.method ?? 'GET'
      if (method !== 'GET') sent.push({ method, path: url.pathname, body: JSON.parse(String(init.body)) })
      if (url.pathname === '/api/v1/auth/me') {
        return json({ id: 7, email: 'u@pmo.example', display_name: 'U', role, actions: ACTIONS[role] })
      }
      if (url.pathname === '/api/v1/users') {
        return json({ items: [{ id: 7, display_name: 'Pat Manager' }], total: 1, limit: 100, offset: 0 })
      }
      if (url.pathname.endsWith('/lessons/3/record')) return json({ ...found.lessons[0], status: 'recorded' })
      if (url.pathname.startsWith('/api/v1/projects/5/closure')) return json(found)
      return json({ detail: `unexpected ${url.pathname}` }, 404)
    }),
  )
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter(true)
  router.push('/projects/5/closure')
  await router.isReady()
  mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { router, sent }
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

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the closure report helpers', () => {
  it('says how a benefit stands at close', () => {
    expect(benefitStanding(benefit())).toBe('Not measured, and no owner or date yet')
    expect(benefitStanding(benefit({ value_now: 'three days', value_now_on: '2027-05-30' }))).toBe(
      'three days on 2027-05-30',
    )
    expect(
      benefitStanding(
        benefit({ measure_owner_id: 7, measure_owner_name: 'Pat', measure_by: '2027-09-01' }),
      ),
    ).toBe('To be measured by Pat by 2027-09-01')
  })

  it('sends only the sections a person changed', () => {
    expect(changedSections(report(), { delivered: 'The pilot.', remaining: 'Nothing' })).toEqual({
      remaining: 'Nothing',
    })
  })

  it('opens writes only in closure, and lessons in delivery too, never once closed', () => {
    expect(reportOpen(closure())).toBe(true)
    expect(reportOpen(closure({ project_stage: 'delivery' }))).toBe(false)
    expect(lessonsOpen(closure({ project_stage: 'delivery' }))).toBe(true)
    expect(lessonsOpen(closure({ project_stage: 'closed' }))).toBe(false)
    expect(reportOpen(closure({ project_stage: 'closed' }))).toBe(false)
  })
})

describe('the closure report page', () => {
  it('links every figure to its source record', async () => {
    await mountAt(closure())
    expect(find('page-title')?.textContent).toContain('Closure report')
    const values = findAll('figure-value').map((e) => e.textContent?.trim())
    expect(values).toEqual(['1,200.00', '1'])
    const links = findAll('figure-source') as HTMLAnchorElement[]
    expect(links.map((l) => l.textContent?.trim())).toEqual([
      'Baseline 3, total',
      'Risk 12, status',
    ])
    expect(links[0].getAttribute('href')).toBe('/projects/5#baseline')
    expect(links[1].getAttribute('href')).toBe('/risks?project=5&risk=12')
    expect(find('accepted-risks')?.textContent).toContain('Key analyst leaves')
  })

  it('labels an agent draft and confirms it with the version', async () => {
    const { sent } = await mountAt(closure())
    expect(find('agent-draft-delivered')?.textContent).toContain('Agent draft, not reviewed')
    expect(find('unreviewed')).not.toBeNull()
    await click('confirm-delivered')
    expect(sent).toEqual([
      {
        method: 'POST',
        path: '/api/v1/projects/5/closure-report/sections/delivered/confirm',
        body: { version: 4 },
      },
    ])
  })

  it("lets the project manager enter a benefit's value now with its version", async () => {
    const { sent } = await mountAt(closure())
    expect(find('benefit-standing')?.textContent).toContain('Not measured')
    await click('measure')
    const value = find('value-now')?.querySelector('input') as HTMLInputElement
    value.value = 'three days'
    value.dispatchEvent(new Event('input'))
    const day = find('value-now-on')?.querySelector('input') as HTMLInputElement
    day.value = '2027-05-30'
    day.dispatchEvent(new Event('input'))
    await flushPromises()
    await click('save-measure')
    expect(sent).toEqual([
      {
        method: 'PUT',
        path: '/api/v1/projects/5/closure-report/benefits/41',
        body: {
          version: 0,
          value_now: 'three days',
          value_now_on: '2027-05-30',
          measure_owner_id: null,
          measure_by: null,
        },
      },
    ])
  })

  it('records an agent suggestion as a lesson', async () => {
    const { sent } = await mountAt(closure())
    expect(find('lesson-status')?.textContent).toContain('Agent suggestion, not recorded')
    await click('record-lesson')
    expect(sent[0]).toEqual({
      method: 'POST',
      path: '/api/v1/projects/5/lessons/3/record',
      body: { version: 1 },
    })
  })

  it('shows a sponsor the report without any write', async () => {
    await mountAt(closure(), 'sponsor')
    expect(find('figures')).not.toBeNull()
    expect(find('measure')).toBeNull()
    expect(find('confirm-delivered')).toBeNull()
    expect(find('save')).toBeNull()
    expect(find('lesson-form')).toBeNull()
    expect(find('draft')).toBeNull()
  })

  it('shows a closed project frozen, with nothing to change', async () => {
    await mountAt(
      closure({
        project_stage: 'closed',
        report: report({ frozen_at: '2027-06-10T12:00:00' }),
      }),
    )
    expect(find('frozen')?.textContent).toContain('The project is closed')
    expect(find('section-delivered')?.tagName).toBe('P')
    expect(find('measure')).toBeNull()
    expect(find('lesson-form')).toBeNull()
    expect(find('record-lesson')).toBeNull()
  })

  it('offers to start the report in closure when there is none', async () => {
    const { sent } = await mountAt(closure({ report: null }))
    expect(find('no-report')).not.toBeNull()
    await click('create-report')
    expect(sent).toEqual([{ method: 'POST', path: '/api/v1/projects/5/closure-report', body: {} }])
  })
})
