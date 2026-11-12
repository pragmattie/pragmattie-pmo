// Status reports (item 7.3): a project's report for each period. The API reads every figure from
// a record and rates the report from the reporting policy; the web only shows them, links each
// figure to its source and hides what a role can't do. The API checks every rule again, and
// hides drafts from roles without `report:publish` (a 404).

import { call } from './risks'

export type ReportStatus = 'draft' | 'published' | 'withdrawn'
export type Rating = 'green' | 'amber' | 'red'
export type SectionName = 'summary' | 'done' | 'next' | 'issues' | 'decisions'

export interface Source {
  table: string
  id: number
  field: string
}

export interface Figure {
  key: string
  label: string
  kind: 'money' | 'percent' | 'date' | 'count' | 'days' | 'number'
  value: string | number | null
  note: string
  sources: Source[]
}

export interface RatingResult {
  rating: Rating
  measure: string | null
  rule: string
  text: string
}

export interface Flag {
  rule: string
  section: string
  text: string
}

export interface ReportSummary {
  id: number
  status: ReportStatus
  overall: Rating
  replaces_id: number | null
  published_at: string | null
  withdrawn_at: string | null
  version: number
}

export interface Period {
  start: string
  end: string
  due_on: string
  reports: ReportSummary[]
}

export interface PeriodsPage {
  project_id: number
  report_period: string | null
  report_due_day: number | null
  items: Period[]
  total: number
  limit: number
  offset: number
}

// Who wrote a section (item 7.4): the status report agent or a person. `unreviewed` is an agent's
// draft no person has edited or confirmed: it shows "Agent draft, not reviewed", and the API
// refuses to publish while any section is one.
export interface SectionState {
  source: 'agent' | 'person' | null
  reviewed_by: number | null
  reviewed_at: string | null
  unreviewed: boolean
}

export interface StatusReport {
  id: number
  project_id: number
  period_start: string
  period_end: string
  due_on: string
  status: ReportStatus
  figures: Figure[]
  ratings: Record<string, RatingResult>
  sections: Partial<Record<SectionName, string | null>>
  section_states: Partial<Record<SectionName, SectionState>>
  flags: Flag[]
  figures_at: string
  replaces_id: number | null
  replace_reason: string | null
  replaced_by_id: number | null
  // None for a draft the orchestrator made when the report fell due (item 7.4).
  created_by: number | null
  created_by_name: string | null
  published_by: number | null
  published_by_name: string | null
  published_at: string | null
  withdrawn_by: number | null
  withdrawn_at: string | null
  withdrawn_reason: string | null
  version: number
  created_at: string
  updated_at: string
}

// The API's limits (app/models/report.py).
export const MAX_SECTION = 4000
export const MAX_REASON = 500

export const SECTIONS: { name: SectionName; title: string }[] = [
  { name: 'summary', title: 'Summary' },
  { name: 'done', title: 'Done this period' },
  { name: 'next', title: 'Planned for next period' },
  { name: 'issues', title: 'Issues' },
  { name: 'decisions', title: 'Decisions needed from the sponsor' },
]

export const RATED: { name: string; title: string }[] = [
  { name: 'overall', title: 'Overall' },
  { name: 'schedule', title: 'Schedule' },
  { name: 'cost', title: 'Cost' },
  { name: 'scope', title: 'Scope' },
  { name: 'risk', title: 'Risk' },
]

export const RATING_TEXT: Record<Rating, string> = { green: 'Green', amber: 'Amber', red: 'Red' }
export const RATING_COLOR: Record<Rating, string> = {
  green: 'success',
  amber: 'warning',
  red: 'error',
}
export const STATUS_TEXT: Record<ReportStatus, string> = {
  draft: 'Draft',
  published: 'Published',
  withdrawn: 'Withdrawn',
}

const TABLE_TEXT: Record<string, string> = {
  project_baselines: 'Baseline',
  plan_milestones: 'Milestone',
  plan_deliverables: 'Deliverable',
  project_spend: 'Spend entry',
  change_requests: 'Change request',
  risks: 'Risk',
}

// Where a figure's source record is shown: a risk in the risk register, everything else in its
// section of the project page.
const SECTION_OF: Record<string, string> = {
  project_baselines: '#baseline',
  plan_milestones: '#delivery',
  plan_deliverables: '#delivery',
  project_spend: '#delivery',
  change_requests: '#changes',
}

export function sourceText(source: Source): string {
  return `${TABLE_TEXT[source.table] ?? source.table} ${source.id}, ${source.field}`
}

export function sourceLink(projectId: number, source: Source) {
  if (source.table === 'risks') {
    return { name: 'risks', query: { project: String(projectId), risk: String(source.id) } }
  }
  return { name: 'project', params: { id: String(projectId) }, hash: SECTION_OF[source.table] ?? '' }
}

export function figureText(figure: Figure): string {
  if (figure.value === null) return 'None recorded'
  switch (figure.kind) {
    case 'money':
      return Number(figure.value).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    case 'percent':
      return `${figure.value}%`
    case 'days':
      return `${figure.value} working day${figure.value === 1 ? '' : 's'}`
    default:
      return String(figure.value)
  }
}

// The sections still waiting for a person to confirm or edit the agent's draft.
export function unreviewedSections(report: StatusReport): SectionName[] {
  return SECTIONS.filter((s) => report.section_states?.[s.name]?.unreviewed).map((s) => s.name)
}

// Only the sections whose text a person changed, so saving never marks an agent's draft as
// reviewed without its text being touched (Confirm does that).
export function changedSections(
  report: StatusReport,
  text: Partial<Record<SectionName, string>>,
): Partial<Record<SectionName, string>> {
  return Object.fromEntries(
    SECTIONS.filter((s) => (text[s.name] ?? '') !== (report.sections[s.name] ?? '')).map((s) => [
      s.name,
      text[s.name] ?? '',
    ]),
  ) as Partial<Record<SectionName, string>>
}

// The period's report a reader should look at first: the live one (a draft or published), else
// the newest withdrawn one.
export function liveReport(period: Period): ReportSummary | null {
  return period.reports.find((r) => r.status !== 'withdrawn') ?? period.reports[0] ?? null
}

// The executive view (item 7.5): a published report on one page, from the figures it froze.
export interface ExecutiveMilestone {
  id: number
  name: string
  target: string | null
  plan_date: string | null
  reached_on: string | null
  days_late: number
  due_next: boolean
}

export interface ExecutiveReport {
  id: number
  project_id: number
  project_name: string
  period_start: string
  period_end: string
  published_at: string
  published_by_name: string | null
  figures_at: string
  ratings: Record<string, RatingResult>
  summary: string | null
  decisions: string | null
  milestones: ExecutiveMilestone[]
  milestones_late: number
  milestones_due_next: number
  spend: {
    spend_to_date: string | null
    spend_as_of: string | null
    baseline_total: string | null
    spent_pct: string | null
    time_elapsed_pct: string | null
    spend_ahead_points: string | null
  }
  high_risks: number
  high_risk_ids: number[]
}

export interface Opened {
  recorded: boolean
  opened_at: string | null
}

export function money(value: string | null): string {
  if (value === null) return 'None recorded'
  return Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// One line for a milestone on the executive view.
export function milestoneText(m: ExecutiveMilestone): string {
  if (m.days_late > 0) {
    const days = `${m.days_late} working day${m.days_late === 1 ? '' : 's'} late`
    return m.reached_on ? `${days}, reached ${m.reached_on}` : `${days}, target ${m.target}`
  }
  return `Due next period, on ${m.plan_date}`
}

// How many late or due-next milestones the report counts but the page doesn't show (it shows at
// most three), so the page can say the report holds no more when it shows them all.
export function milestonesNotShown(view: ExecutiveReport): number {
  const counted = view.milestones_late + view.milestones_due_next
  return Math.max(0, counted - view.milestones.length)
}

// Where a person opening a report lands (item 7.5): a sponsor on a published report's one-page
// view, everyone else on the detailed report, which is one click from the one-page view.
export function reportLink(
  projectId: number,
  reportId: number,
  role: string | undefined,
  status: ReportStatus,
  query: Record<string, string> = {},
) {
  if (role === 'sponsor' && status === 'published') {
    return {
      name: 'status-report-executive',
      params: { id: String(projectId), reportId: String(reportId) },
      query,
    }
  }
  return {
    name: 'status-reports',
    params: { id: String(projectId) },
    query: { ...query, report: String(reportId) },
  }
}

const base = (projectId: number) => `/api/v1/projects/${projectId}/status-reports`

export const statusReportApi = {
  periods: (projectId: number, offset = 0) =>
    call<PeriodsPage>(`${base(projectId)}?limit=20&offset=${offset}`),
  get: (projectId: number, reportId: number) =>
    call<StatusReport>(`${base(projectId)}/${reportId}`),
  create: (projectId: number, periodEnd: string, replaces?: { id: number; reason: string }) =>
    call<StatusReport>(base(projectId), {
      method: 'POST',
      body: JSON.stringify({
        period_end: periodEnd,
        ...(replaces ? { replaces_id: replaces.id, reason: replaces.reason } : {}),
      }),
    }),
  edit: (projectId: number, report: StatusReport, sections: Partial<Record<SectionName, string>>) =>
    call<StatusReport>(`${base(projectId)}/${report.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ version: report.version, sections }),
    }),
  refresh: (projectId: number, report: StatusReport) =>
    call<StatusReport>(`${base(projectId)}/${report.id}/refresh`, {
      method: 'POST',
      body: JSON.stringify({ version: report.version }),
    }),
  publish: (projectId: number, report: StatusReport) =>
    call<StatusReport>(`${base(projectId)}/${report.id}/publish`, {
      method: 'POST',
      body: JSON.stringify({ version: report.version }),
    }),
  confirm: (projectId: number, report: StatusReport, section: SectionName) =>
    call<StatusReport>(`${base(projectId)}/${report.id}/sections/${section}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ version: report.version }),
    }),
  executive: (projectId: number, reportId: number) =>
    call<ExecutiveReport>(`${base(projectId)}/${reportId}/executive`),
  // The page says it was opened; the API keeps only the project sponsor's first opening.
  opened: (projectId: number, reportId: number) =>
    call<Opened>(`${base(projectId)}/${reportId}/opened`, { method: 'POST', body: '{}' }),
  withdraw: (projectId: number, report: StatusReport, reason: string) =>
    call<StatusReport>(`${base(projectId)}/${report.id}/withdraw`, {
      method: 'POST',
      body: JSON.stringify({ version: report.version, reason }),
    }),
}
