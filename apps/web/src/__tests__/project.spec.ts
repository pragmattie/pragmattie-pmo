import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import type { ProjectRequest } from '../lib/intake'
import { budgetText, label, risksLink } from '../lib/projects'
import { makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('visualViewport', new EventTarget())

const ACTIONS = ['project:read', 'project:write', 'report:publish', 'risk:accept']

const PROJECT = {
  id: 5,
  client_id: 3,
  client_name: 'Northwind Example Ltd',
  name: 'Warehouse demand forecast',
  stage: 'planning',
  status: 'on_track',
  sponsor: 'Sam Sponsor',
  sponsor_user_id: 2,
  pm: 'Pat Manager',
  pm_user_id: 7,
  request_id: 1,
  created_at: '2026-11-03T09:00:00',
}

const BRIEF = {
  id: 9,
  project_id: 5,
  purpose: 'Stock-outs every quarter cost sales',
  objectives: 'Half as many stock-outs\n\nBenefits:\n- Fewer stock-outs',
  scope_in: 'A weekly forecast',
  scope_out: null,
  wanted_by: '2027-06-30',
  budget_low: '10000.00',
  budget_high: '20000.00',
  decision_rights: [
    { role: 'project_manager', title: 'Project manager', decides: "the project's brief" },
    { role: 'sponsor', title: 'Project sponsor', decides: 'stage gates' },
  ],
  sponsor: 'Sam Sponsor',
  sponsor_user_id: 2,
  pm: 'Pat Manager',
  pm_user_id: 7,
  version: 1,
  updated_at: '2026-11-03T09:00:00',
}

const STAKEHOLDERS = [
  { id: 1, project_id: 5, name: 'Sam Sponsor', user_id: 2, role_or_interest: 'Sponsor' },
  { id: 2, project_id: 5, name: 'Pat Manager', user_id: 7, role_or_interest: 'Project manager' },
  { id: 3, project_id: 5, name: 'Robin Contact', user_id: null, role_or_interest: 'Client contact' },
].map((s) => ({ ...s, needs_to_hear: null, how_often: null, version: 1 }))

const ASSUMPTION = {
  id: 4,
  project_id: 5,
  text: 'Three years of sales history exist',
  owner_user_id: 7,
  owner_name: 'Pat Manager',
  confirm_by: '2026-12-01',
  status: 'open',
  version: 1,
}

function page(items: unknown[]) {
  return { items, total: items.length, limit: 100, offset: 0 }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface FakeOptions {
  assumptions?: unknown[]
  noBrief?: boolean
  request?: Partial<ProjectRequest>
}

function fakeApi(options: FakeOptions = {}) {
  const user = { id: 7, email: 'pm@pmo.example', display_name: 'Pat Manager', role: 'project_manager' }
  return vi.fn(async (input: string) => {
    const path = new URL(input, 'http://localhost').pathname
    if (path === '/api/v1/auth/me') return json({ ...user, actions: ACTIONS })
    if (path === '/api/v1/projects') return json(page([PROJECT]))
    if (path === '/api/v1/projects/5') return json(PROJECT)
    if (path === '/api/v1/projects/5/brief') {
      return options.noBrief ? json({ detail: 'this project has no brief' }, 404) : json(BRIEF)
    }
    if (path === '/api/v1/projects/5/stakeholders') return json(page(STAKEHOLDERS))
    if (path === '/api/v1/projects/5/assumptions') return json(page(options.assumptions ?? []))
    if (path === '/api/v1/clients') return json(page([]))
    if (path === '/api/v1/requests/1/history') return json(page([]))
    if (path === '/api/v1/requests/1/value-case') return json({ detail: 'none' }, 404)
    if (path === '/api/v1/requests/1/gate') return json({ detail: 'not applied' }, 503)
    if (path === '/api/v1/requests/1') {
      return json({
        id: 1,
        title: 'Warehouse demand forecast',
        client_id: null,
        client_name: null,
        asked_for: 'A forecast',
        problem: 'Stock-outs',
        outcome: 'Fewer stock-outs',
        budget_low: null,
        budget_high: null,
        wanted_by: null,
        uses_ai: false,
        known_risks: null,
        requested_by: 7,
        requested_by_name: 'Pat Manager',
        status: 'approved',
        submitted_at: '2026-10-02T09:00:00',
        project_id: 5,
        version: 3,
        created_at: '2026-10-01T09:00:00',
        updated_at: '2026-10-01T09:00:00',
        ...options.request,
      })
    }
    return json({ detail: `unexpected ${path}` }, 404)
  })
}

async function mountAt(path: string, options: FakeOptions = {}) {
  vi.stubGlobal('fetch', fakeApi(options))
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter(true)
  router.push(path)
  await router.isReady()
  mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [pinia, router, makeVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return { router }
}

function find(testId: string) {
  return document.body.querySelector(`[data-test="${testId}"]`)
}

function findAll(testId: string) {
  return Array.from(document.body.querySelectorAll(`[data-test="${testId}"]`))
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.stubGlobal('visualViewport', new EventTarget())
})

describe('the project helpers', () => {
  it('labels, budgets and links plainly', () => {
    expect(label('on_track')).toBe('On track')
    expect(budgetText('10.00', '20.00')).toBe('10.00 to 20.00')
    expect(budgetText(null, '20.00')).toBe('up to 20.00')
    expect(budgetText('10.00', null)).toBe('from 10.00')
    expect(budgetText(null, null)).toBe('Not set')
    expect(risksLink(5)).toEqual({ name: 'risks', query: { project: '5' } })
  })
})

describe('the project page', () => {
  it('shows the list of projects', async () => {
    await mountAt('/projects')
    expect(find('placeholder')).toBeNull()
    expect(find('page-title')?.textContent).toBe('Projects')
    const [item] = findAll('project-item')
    expect(item.textContent).toContain('Warehouse demand forecast')
    expect(item.getAttribute('href')).toBe('/projects/5')
  })

  it('shows the brief', async () => {
    await mountAt('/projects/5')
    expect(find('page-title')?.textContent).toBe('Warehouse demand forecast')
    expect(find('project-summary')?.textContent).toContain('Planning, On track.')
    expect(find('project-summary')?.textContent).toContain('Project manager: Pat Manager.')
    expect(find('brief-purpose')?.textContent).toBe('Stock-outs every quarter cost sales')
    expect(find('brief-objectives')?.textContent).toContain('- Fewer stock-outs')
    expect(find('brief-budget')?.textContent).toBe('10000.00 to 20000.00')
    expect(find('decision-rights')?.textContent).toContain('Project sponsor: stage gates')
  })

  it('shows the plan section above the stakeholders', async () => {
    await mountAt('/projects/5')
    const plan = find('plan')
    expect(plan?.textContent).toContain('Plan')
    expect(plan?.compareDocumentPosition(find('stakeholders')!)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
    expect(find('stakeholders')?.id).toBe('stakeholders')
  })

  it('shows the stakeholders', async () => {
    await mountAt('/projects/5')
    const rows = findAll('stakeholder').map((row) => row.textContent)
    expect(rows).toHaveLength(3)
    expect(rows[0]).toContain('Sam Sponsor')
    expect(rows[2]).toContain('Robin Contact')
    expect(rows[2]).toContain('Client contact')
  })

  it('shows the assumptions, or that there are none yet', async () => {
    await mountAt('/projects/5')
    expect(find('no-assumptions')).not.toBeNull()
    document.body.innerHTML = ''
    await mountAt('/projects/5', { assumptions: [ASSUMPTION] })
    const [row] = findAll('assumption')
    expect(row.textContent).toContain('Three years of sales history exist')
    expect(row.textContent).toContain('Pat Manager')
    expect(row.textContent).toContain('2026-12-01')
    expect(row.textContent).toContain('Open')
  })

  it('links to the project risks and back to its request', async () => {
    await mountAt('/projects/5')
    expect(find('risks-link')?.getAttribute('href')).toBe('/risks?project=5')
    expect(find('request-link')?.getAttribute('href')).toBe('/intake/1')
  })

  it('says so when a project has no brief', async () => {
    await mountAt('/projects/5', { noBrief: true })
    expect(find('no-brief')?.textContent).toContain('this project has no brief')
    expect(findAll('stakeholder')).toHaveLength(3)
  })
})

describe('the request page', () => {
  it('has one way back, the breadcrumb trail, and no older back buttons', async () => {
    await mountAt('/intake/1')
    expect(find('page-title')).not.toBeNull()
    expect(find('back-to-project')).toBeNull()
    expect(find('back')).toBeNull()
    expect(document.body.textContent).not.toContain('Back to the project')
    expect(document.body.textContent).not.toContain('Back to the queue')
    expect(find('breadcrumbs')?.querySelector('a')?.getAttribute('href')).toBe('/intake')
    expect(find('back-to-inbox')).toBeNull()
  })
})

describe('ways back from a detail page', () => {
  const trail = () => find('breadcrumbs')?.textContent?.replace(/\s*›\s*/g, ' › ').trim()

  it('shows Projects › project on a project page, with Projects highlighted', async () => {
    await mountAt('/projects/5')
    expect(trail()).toBe('Projects › Warehouse demand forecast')
    expect(find('nav-projects')?.classList.contains('v-list-item--active')).toBe(true)
    expect(find('back-to-inbox')).toBeNull()
  })

  it('shows Intake › request on a request page, with Intake highlighted', async () => {
    await mountAt('/intake/1')
    expect(trail()).toBe('Intake › Warehouse demand forecast')
    expect(find('nav-intake')?.classList.contains('v-list-item--active')).toBe(true)
  })

  it.each(['/projects/5', '/intake/1'])(
    'shows Back to Inbox on %s when opened from the Inbox, returning with the item expanded',
    async (path) => {
      await mountAt(`${path}?from=inbox&item=item-3`)
      const back = find('back-to-inbox')
      expect(back?.textContent).toContain('Back to Inbox')
      expect(back?.getAttribute('href')).toBe('/inbox?open=item-3')
    },
  )
})
