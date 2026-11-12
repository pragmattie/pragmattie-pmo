import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import {
  changedSections,
  type ExecutiveReport,
  figureText,
  liveReport,
  milestonesNotShown,
  milestoneText,
  type Period,
  reportLink,
  type StatusReport,
  sourceLink,
  sourceText,
  unreviewedSections,
} from '../lib/statusReports'
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
  auditor: ['audit:read', 'project:read', 'standards:read'],
}

const PROJECT = {
  id: 5,
  client_id: null,
  client_name: null,
  name: 'Warehouse demand forecast',
  stage: 'delivery',
  status: 'on_track',
  sponsor: 'Sam Sponsor',
  sponsor_user_id: 2,
  pm: 'Pat Manager',
  pm_user_id: 7,
  request_id: null,
  created_at: '2026-11-03T09:00:00',
}

function rating(value: 'green' | 'amber' | 'red', rule: string, text: string) {
  return { rating: value, measure: null, rule, text }
}

function report(changes: Partial<StatusReport> = {}): StatusReport {
  return {
    id: 31,
    project_id: 5,
    period_start: '2027-03-01',
    period_end: '2027-03-07',
    due_on: '2027-03-08',
    status: 'draft',
    figures: [
      {
        key: 'baseline_total',
        label: 'Baseline total',
        kind: 'money',
        value: '1000.00',
        note: "The current baseline's total, contingency included",
        sources: [{ table: 'project_baselines', id: 4, field: 'total' }],
      },
      {
        key: 'most_days_late',
        label: 'Most working days late',
        kind: 'days',
        value: 4,
        note: 'The most working days any milestone is late against its baseline target',
        sources: [{ table: 'plan_milestones', id: 11, field: 'reached_on' }],
      },
      {
        key: 'open_risks_high',
        label: 'Open risks: High',
        kind: 'count',
        value: 1,
        note: 'Open risks in the risk register whose band is High',
        sources: [{ table: 'risks', id: 8, field: 'band' }],
      },
    ],
    ratings: {
      overall: rating('amber', 'overall.worst_of_four', 'the worst of the four (schedule is amber)'),
      schedule: rating(
        'amber',
        'schedule.amber_above_working_days',
        'The most working days a milestone is late is 4, above the amber limit of 0',
      ),
      cost: rating('green', 'cost.no_spend', 'no spend recorded yet'),
      scope: rating('green', 'scope.amber_above_pct', 'within the amber limit of 5'),
      risk: rating('amber', 'risk.amber_at.High', '1 open High risks'),
    },
    sections: { summary: 'Pilot live is 4 working days late.', done: null },
    section_states: {},
    flags: [],
    figures_at: '2027-03-10T09:00:00',
    replaces_id: null,
    replace_reason: null,
    replaced_by_id: null,
    created_by: 7,
    created_by_name: 'Pat Manager',
    published_by: null,
    published_by_name: null,
    published_at: null,
    withdrawn_by: null,
    withdrawn_at: null,
    withdrawn_reason: null,
    version: 2,
    created_at: '2027-03-10T09:00:00',
    updated_at: '2027-03-10T09:00:00',
    ...changes,
  }
}

function periods(reports: Period['reports'] = []) {
  return {
    project_id: 5,
    report_period: 'weekly',
    report_due_day: 1,
    items: [
      { start: '2027-03-08', end: '2027-03-14', due_on: '2027-03-15', reports: [] },
      { start: '2027-03-01', end: '2027-03-07', due_on: '2027-03-08', reports },
    ],
    total: 2,
    limit: 20,
    offset: 0,
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

interface Fake {
  role?: string
  found?: StatusReport | null
  listed?: Period['reports']
  sent?: Sent[]
  answer?: (method: string, path: string) => Response | null
}

async function mountAt(path: string, fake: Fake = {}) {
  const role = fake.role ?? 'project_manager'
  const found = fake.found === undefined ? report() : fake.found
  const sent = fake.sent ?? []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input, 'http://localhost')
      const method = init.method ?? 'GET'
      if (method !== 'GET') sent.push({ method, path: url.pathname, body: JSON.parse(String(init.body)) })
      const answered = fake.answer?.(method, url.pathname)
      if (answered) return answered
      if (url.pathname === '/api/v1/auth/me') {
        return json({ id: 7, email: 'u@pmo.example', display_name: 'U', role, actions: ACTIONS[role] })
      }
      if (url.pathname === '/api/v1/projects/5') return json(PROJECT)
      if (url.pathname === '/api/v1/projects/5/status-reports' && method === 'GET') {
        return json(periods(fake.listed ?? []))
      }
      if (url.pathname.startsWith('/api/v1/projects/5/status-reports/')) {
        if (found === null) return json({ detail: 'status report not found' }, 404)
        return json(found)
      }
      if (url.pathname === '/api/v1/projects/5/status-reports') return json(found, 201)
      return json({ detail: `unexpected ${url.pathname}` }, 404)
    }),
  )
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

const PUBLISHED = { id: 31, status: 'published' as const, overall: 'amber' as const, replaces_id: null, published_at: '2027-03-10T10:00:00', withdrawn_at: null, version: 3 }

describe('the status report helpers', () => {
  it('shows figures plainly and links each source to its record', () => {
    expect(figureText(report().figures[0])).toBe('1,000.00')
    expect(figureText(report().figures[1])).toBe('4 working days')
    expect(figureText({ ...report().figures[1], value: 1 })).toBe('1 working day')
    expect(figureText({ ...report().figures[0], value: null })).toBe('None recorded')
    expect(figureText({ ...report().figures[0], kind: 'percent', value: '12.50' })).toBe('12.50%')
    const risk = { table: 'risks', id: 8, field: 'band' }
    expect(sourceText(risk)).toBe('Risk 8, band')
    expect(sourceLink(5, risk)).toEqual({ name: 'risks', query: { project: '5', risk: '8' } })
    expect(sourceLink(5, { table: 'plan_milestones', id: 11, field: 'reached_on' })).toEqual({
      name: 'project',
      params: { id: '5' },
      hash: '#delivery',
    })
    expect(sourceLink(5, { table: 'change_requests', id: 3, field: 'decider' }).hash).toBe('#changes')
  })

  it('picks the live report of a period before a withdrawn one', () => {
    const withdrawn = { ...PUBLISHED, id: 30, status: 'withdrawn' as const }
    const base = { start: 'a', end: 'b', due_on: 'c' }
    expect(liveReport({ ...base, reports: [PUBLISHED, withdrawn] })?.id).toBe(31)
    expect(liveReport({ ...base, reports: [withdrawn] })?.id).toBe(30)
    expect(liveReport({ ...base, reports: [] })).toBeNull()
  })
})

// A draft the status report agent wrote (item 7.4): summary and next are its unreviewed drafts,
// done is a person's.
const AGENT_STATE = { source: 'agent' as const, reviewed_by: null, reviewed_at: null, unreviewed: true }
const PERSON_STATE = { source: 'person' as const, reviewed_by: 7, reviewed_at: '2027-03-10T09:30:00', unreviewed: false }

function agentDraft(changes: Partial<StatusReport> = {}) {
  return report({
    sections: {
      summary: 'Spend to date is 400.00.',
      done: 'The pilot went live.',
      next: 'From 8 March 2027 the team prepares the handover.',
      issues: null,
      decisions: null,
    },
    section_states: { summary: AGENT_STATE, done: PERSON_STATE, next: AGENT_STATE },
    ...changes,
  })
}

describe('the agent draft helpers (item 7.4)', () => {
  it('names the unreviewed sections and only the changed text', () => {
    const draft = agentDraft()
    expect(unreviewedSections(draft)).toEqual(['summary', 'next'])
    expect(unreviewedSections(report())).toEqual([])
    expect(
      changedSections(draft, {
        summary: 'Spend to date is 400.00.',
        done: 'The pilot went live on time.',
        next: 'From 8 March 2027 the team prepares the handover.',
        issues: '',
        decisions: '',
      }),
    ).toEqual({ done: 'The pilot went live on time.' })
  })
})

describe('the Status reports page', () => {
  it('lists the periods and shows a report with its ratings, rules, sourced figures and text', async () => {
    await mountAt('/projects/5/status-reports?report=31', { role: 'sponsor', found: report({ status: 'published', published_by_name: 'Pat Manager' }), listed: [PUBLISHED] })
    expect(find('page-title')?.textContent).toContain('Status reports')
    expect(findAll('period')).toHaveLength(2)
    expect(find('period-report')?.textContent).toContain('Published, Amber')
    expect(find('report-title')?.textContent).toContain('2027-03-01 to 2027-03-07')
    expect(find('rating-overall')?.textContent).toContain('Amber')
    expect(find('rating-schedule')?.textContent).toContain('above the amber limit of 0')
    expect(find('rating-schedule')?.textContent).toContain('Rule: schedule.amber_above_working_days')
    expect(find('rating-cost')?.textContent).toContain('Green')
    const values = findAll('figure-value').map((v) => v.textContent?.trim())
    expect(values).toEqual(['1,000.00', '4 working days', '1'])
    const sources = findAll('figure-source') as HTMLAnchorElement[]
    expect(sources.map((s) => s.textContent?.trim())).toEqual([
      'Baseline 4, total',
      'Milestone 11, reached_on',
      'Risk 8, band',
    ])
    expect(sources[2].getAttribute('href')).toBe('/risks?project=5&risk=8')
    expect(sources[1].getAttribute('href')).toBe('/projects/5#delivery')
    expect(find('section-summary')?.textContent).toContain('Pilot live is 4 working days late.')
    expect(find('section-done')?.textContent).toContain('Nothing written.')
  })

  it.each(['sponsor', 'auditor'])('%s gets no actions and the not-found for a draft', async (role) => {
    await mountAt('/projects/5/status-reports?report=31', { role, found: null })
    expect(find('report')).toBeNull()
    expect(find('error')?.textContent).toContain('status report not found')
    expect(find('start-draft')).toBeNull()
    expect(find('actions')).toBeNull()
  })

  it('lets the project manager start a draft for a period without one', async () => {
    const { router, sent } = await mountAt('/projects/5/status-reports', { found: report() })
    const starts = findAll('start-draft')
    expect(starts).toHaveLength(2)
    ;(starts[1] as HTMLElement).click()
    await flushPromises()
    expect(sent).toEqual([
      { method: 'POST', path: '/api/v1/projects/5/status-reports', body: { period_end: '2027-03-07' } },
    ])
    expect(router.currentRoute.value.query.report).toBe('31')
    expect(find('notice')?.textContent).toContain('Draft started.')
  })

  it('saves the text and shows the checker flags, which keep Publish off', async () => {
    const flagged = report({
      version: 3,
      flags: [{ rule: 'stray_number', section: 'summary', text: '7' }],
      sections: { summary: 'Ran 7 workshops.' },
    })
    const { sent } = await mountAt('/projects/5/status-reports?report=31', {
      answer: (method) => (method === 'PATCH' ? json(flagged) : null),
    })
    await type('section-input-summary', 'Ran 7 workshops.')
    await click('save')
    expect(sent[0].method).toBe('PATCH')
    expect(sent[0].body).toMatchObject({ version: 2, sections: { summary: 'Ran 7 workshops.' } })
    expect(find('flags-summary')?.textContent).toContain("7 isn't one of the report's figures")
    expect((find('publish') as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a refused publish with its flags', async () => {
    const { sent } = await mountAt('/projects/5/status-reports?report=31', {
      answer: (method, path) =>
        method === 'POST' && path.endsWith('/publish')
          ? json(
              {
                detail: {
                  message: "the report can't be published while the checker has flags open",
                  flags: [{ rule: 'stray_date', section: 'issues', text: '2027-03-05' }],
                },
              },
              409,
            )
          : null,
    })
    await click('publish')
    expect(sent[0]).toEqual({
      method: 'POST',
      path: '/api/v1/projects/5/status-reports/31/publish',
      body: { version: 2 },
    })
    expect(find('error')?.textContent).toContain('checker has flags open')
    expect(find('error-items')?.textContent).toContain('issues: 2027-03-05')
  })

  it('refreshes a draft and starts a correction of a published report with a reason', async () => {
    const { sent } = await mountAt('/projects/5/status-reports?report=31')
    await click('refresh')
    expect(sent[0]).toMatchObject({ path: '/api/v1/projects/5/status-reports/31/refresh', body: { version: 2 } })
    document.body.innerHTML = ''

    const published = await mountAt('/projects/5/status-reports?report=31', {
      found: report({ status: 'published', version: 3 }),
      listed: [PUBLISHED],
    })
    expect(find('save')).toBeNull()
    expect(find('section-input-summary')).toBeNull()
    expect((find('correct') as HTMLButtonElement).disabled).toBe(true)
    await type('reason', 'Spend was misread')
    await click('correct')
    expect(published.sent[0]).toEqual({
      method: 'POST',
      path: '/api/v1/projects/5/status-reports',
      body: { period_end: '2027-03-07', replaces_id: 31, reason: 'Spend was misread' },
    })
  })

  it('shows a withdrawn report with its reason and what replaced it', async () => {
    await mountAt('/projects/5/status-reports?report=30', {
      role: 'sponsor',
      found: report({ id: 30, status: 'withdrawn', withdrawn_reason: 'Spend was misread', replaced_by_id: 31 }),
    })
    expect(find('report-withdrawn')?.textContent).toContain('Spend was misread')
    expect(find('replaced-by')?.textContent).toContain('report 31')
  })

  it("marks the agent's sections and confirms each one, with Publish off until all are", async () => {
    const confirmed = agentDraft({
      version: 3,
      section_states: { summary: PERSON_STATE, done: PERSON_STATE, next: AGENT_STATE },
    })
    const { sent } = await mountAt('/projects/5/status-reports?report=31', {
      found: agentDraft(),
      answer: (method, path) => (method === 'POST' && path.endsWith('/confirm') ? json(confirmed) : null),
    })
    expect(find('agent-chip-summary')?.textContent).toContain('Agent draft, not reviewed')
    expect(find('agent-chip-next')).not.toBeNull()
    expect(find('agent-chip-done')).toBeNull()
    expect(find('confirm-done')).toBeNull()
    expect((find('publish') as HTMLButtonElement).disabled).toBe(true)
    expect(find('unreviewed')?.textContent).toContain('Confirm or edit each')
    await click('confirm-summary')
    expect(sent[0]).toEqual({
      method: 'POST',
      path: '/api/v1/projects/5/status-reports/31/sections/summary/confirm',
      body: { version: 2 },
    })
    expect(find('notice')?.textContent).toContain('Section confirmed.')
    expect(find('agent-chip-summary')).toBeNull()
    expect(find('agent-chip-next')).not.toBeNull()
    expect((find('publish') as HTMLButtonElement).disabled).toBe(true)
  })

  it('turns Publish on once every agent section is confirmed', async () => {
    await mountAt('/projects/5/status-reports?report=31', {
      found: agentDraft({ section_states: { summary: PERSON_STATE, next: PERSON_STATE } }),
    })
    expect(find('agent-chip-summary')).toBeNull()
    expect((find('publish') as HTMLButtonElement).disabled).toBe(false)
  })

  it("saves only the sections a person changed, so the agent's others stay unreviewed", async () => {
    const { sent } = await mountAt('/projects/5/status-reports?report=31', {
      found: agentDraft(),
      answer: (method) => (method === 'PATCH' ? json(agentDraft({ version: 3 })) : null),
    })
    await type('section-input-next', 'The team hands over in the next period.')
    await click('save')
    expect(sent[0].method).toBe('PATCH')
    expect(sent[0].body).toEqual({
      version: 2,
      sections: { next: 'The team hands over in the next period.' },
    })
  })

  it("shows no Confirm on a report the reader can't edit", async () => {
    await mountAt('/projects/5/status-reports?report=31', {
      role: 'sponsor',
      found: agentDraft({ status: 'published', published_at: '2027-03-10T10:00:00' }),
    })
    expect(find('confirm-summary')).toBeNull()
  })

  it('names the project in the page trail', async () => {
    await mountAt('/projects/5/status-reports')
    const trail = document.body.textContent ?? ''
    expect(trail).toContain('Warehouse demand forecast')
  })
})

// --- The executive view (item 7.5)

const EXECUTIVE: ExecutiveReport = {
  id: 31,
  project_id: 5,
  project_name: 'Warehouse demand forecast',
  period_start: '2027-03-01',
  period_end: '2027-03-07',
  published_at: '2027-03-10T10:00:00',
  published_by_name: 'Pat Manager',
  figures_at: '2027-03-10T09:00:00',
  ratings: report().ratings,
  summary: 'Pilot live is 4 working days late.',
  decisions: 'Agree a new pilot date.',
  milestones: [
    { id: 11, name: 'Pilot live', target: '2027-03-01', plan_date: '2027-03-01', reached_on: null, days_late: 4, due_next: false },
    { id: 12, name: 'Handover', target: '2027-03-12', plan_date: '2027-03-12', reached_on: null, days_late: 0, due_next: true },
  ],
  milestones_late: 1,
  milestones_due_next: 1,
  spend: {
    spend_to_date: '400.00',
    spend_as_of: '2027-03-05',
    baseline_total: '1000.00',
    spent_pct: '40.00',
    time_elapsed_pct: '35.00',
    spend_ahead_points: '5.00',
  },
  high_risks: 1,
  high_risk_ids: [8],
}

function executiveAnswer(view: ExecutiveReport | null) {
  return (method: string, path: string) => {
    if (path === '/api/v1/projects/5/status-reports/31/executive') {
      return view ? json(view) : json({ detail: 'status report not found' }, 404)
    }
    if (method === 'POST' && path === '/api/v1/projects/5/status-reports/31/opened') {
      return json({ recorded: true, opened_at: '2027-03-10T11:00:00' })
    }
    return null
  }
}

const EXECUTIVE_PATH = '/projects/5/status-reports/31/executive'

describe('the executive view', () => {
  it('shows every section of a published report on one page, with a print stylesheet', async () => {
    await mountAt(EXECUTIVE_PATH, { role: 'sponsor', answer: executiveAnswer(EXECUTIVE) })
    expect(find('executive')!.classList.contains('print-one-page')).toBe(true)
    for (const section of [
      'executive-ratings',
      'executive-summary',
      'executive-decisions',
      'executive-milestones',
      'executive-spend',
      'executive-risks',
    ]) {
      expect(find(section), section).not.toBeNull()
    }
    for (const name of ['overall', 'schedule', 'cost', 'scope', 'risk']) {
      expect(find(`executive-rating-${name}`), name).not.toBeNull()
    }
    expect(find('executive-rating-overall')?.textContent).toContain('Overall: Amber')
    expect(find('executive-rating-cost')?.textContent).toContain('Cost: Green')
    expect(find('executive-summary')?.textContent).toContain('Pilot live is 4 working days late.')
    expect(find('executive-decisions')?.textContent).toContain('Agree a new pilot date.')
    const milestones = findAll('executive-milestone').map((m) => m.textContent)
    expect(milestones).toHaveLength(2)
    expect(milestones[0]).toContain('Pilot live: 4 working days late, target 2027-03-01')
    expect(milestones[1]).toContain('Handover: Due next period, on 2027-03-12')
    expect(find('executive-milestones-all')?.textContent).toContain('holds no more')
    expect(find('executive-spend')?.textContent).toContain('400.00 spent of a 1,000.00 baseline')
    expect(find('executive-risks')?.textContent).toContain('1 open High risk')
    expect(find('executive-risk')?.getAttribute('href')).toBe('/risks?project=5&risk=8')
    // The detailed report is one click away, and Print is the browser's.
    expect(find('executive-detailed')?.getAttribute('href')).toBe(
      '/projects/5/status-reports?report=31',
    )
    expect(find('executive-print')).not.toBeNull()
  })

  it('tells the API it was opened, once per visit', async () => {
    const { sent } = await mountAt(EXECUTIVE_PATH, {
      role: 'sponsor',
      answer: executiveAnswer(EXECUTIVE),
    })
    expect(sent.map((s) => `${s.method} ${s.path}`)).toEqual([
      'POST /api/v1/projects/5/status-reports/31/opened',
    ])
  })

  it('says when there are more milestones than it shows, and when there are none', async () => {
    const more = { ...EXECUTIVE, milestones_late: 4, milestones_due_next: 1 }
    await mountAt(EXECUTIVE_PATH, { role: 'sponsor', answer: executiveAnswer(more) })
    expect(find('executive-milestones-more')?.textContent).toContain('3 more')
    document.body.innerHTML = ''
    const none = { ...EXECUTIVE, milestones: [], milestones_late: 0, milestones_due_next: 0, high_risks: 0, high_risk_ids: [] }
    await mountAt(EXECUTIVE_PATH, { role: 'sponsor', answer: executiveAnswer(none) })
    expect(find('executive-milestones')?.textContent).toContain('No milestone was late')
    expect(find('executive-risks')?.textContent).toContain('No open High risks')
  })

  it('shows only a published report: anything else is an error and nothing is recorded', async () => {
    const { sent } = await mountAt(EXECUTIVE_PATH, { role: 'sponsor', answer: executiveAnswer(null) })
    expect(find('load-error')?.textContent).toContain('status report not found')
    expect(find('executive-summary')).toBeNull()
    expect(sent).toEqual([])
  })

  it('lands a sponsor on the one-page view and a project manager on the detailed report', async () => {
    expect(reportLink(5, 31, 'sponsor', 'published')).toEqual({
      name: 'status-report-executive',
      params: { id: '5', reportId: '31' },
      query: {},
    })
    expect(reportLink(5, 31, 'project_manager', 'published')).toEqual({
      name: 'status-reports',
      params: { id: '5' },
      query: { report: '31' },
    })
    expect(reportLink(5, 31, 'sponsor', 'withdrawn').name).toBe('status-reports')

    const asSponsor = await mountAt('/projects/5/status-reports', {
      role: 'sponsor',
      listed: [PUBLISHED],
      answer: executiveAnswer(EXECUTIVE),
    })
    await click('period-report')
    expect(asSponsor.router.currentRoute.value.name).toBe('status-report-executive')
    document.body.innerHTML = ''

    const asManager = await mountAt('/projects/5/status-reports', { listed: [PUBLISHED] })
    await click('period-report')
    expect(asManager.router.currentRoute.value.name).toBe('status-reports')
    expect(asManager.router.currentRoute.value.query.report).toBe('31')
  })

  it('links a published report to its one-page view, and not a draft', async () => {
    await mountAt('/projects/5/status-reports?report=31', {
      found: report({ status: 'published', published_at: '2027-03-10T10:00:00' }),
    })
    expect(find('executive-link')?.getAttribute('href')).toBe(EXECUTIVE_PATH)
    document.body.innerHTML = ''
    await mountAt('/projects/5/status-reports?report=31')
    expect(find('executive-link')).toBeNull()
  })

  it('words a milestone and counts the ones not shown', () => {
    const [late, next] = EXECUTIVE.milestones
    expect(milestoneText(late)).toBe('4 working days late, target 2027-03-01')
    expect(milestoneText({ ...late, days_late: 1, reached_on: '2027-03-02' })).toBe(
      '1 working day late, reached 2027-03-02',
    )
    expect(milestoneText(next)).toBe('Due next period, on 2027-03-12')
    expect(milestonesNotShown(EXECUTIVE)).toBe(0)
    expect(milestonesNotShown({ ...EXECUTIVE, milestones_late: 3, milestones_due_next: 2 })).toBe(3)
  })
})
