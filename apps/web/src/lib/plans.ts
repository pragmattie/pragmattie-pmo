// A project's plan (item 5.1): scope and deliverables, milestones, the estimate, who does what and
// the reporting plan. The API computes the total and the `missing` list; the web only shows them.

import { apiFetch } from './api'
import type { Page, Person } from './risks'
import { apiError, call } from './risks'

export type ReportPeriod = 'weekly' | 'fortnightly' | 'monthly'
export type BudgetFit = 'within' | 'below' | 'above' | 'no_range'
export type EstimateCategory = 'people' | 'software' | 'services' | 'other'
// Who wrote a plan item (item 6.5): the planner agent's draft, or a person who added or edited it.
export type ItemSource = 'agent' | 'person'

// How the plan marks an item the planner agent drafted (item 6.5) until a person edits or
// confirms it (item 6.6), so a person can tell it apart.
export const DRAFTED_BY_AGENT = 'Agent draft'

export interface Plan {
  id: number
  project_id: number
  scope_in: string | null
  scope_out: string | null
  contingency_pct: number
  report_period: ReportPeriod | null
  report_due_day: number | null
  // Money is a string with 2 decimal places, computed by the API.
  lines_total: string
  contingency_amount: string
  total: string
  budget_low: string | null
  budget_high: string | null
  budget_fit: BudgetFit
  missing: string[]
  editable: boolean
  version: number
  updated_at: string
}

export interface Milestone {
  id: number
  plan_id: number
  name: string
  target_date: string
  owner_user_id: number | null
  owner_name: string | null
  owner_active: boolean
  description: string | null
  contributors: string | null
  source: ItemSource
  version: number
}

export interface Deliverable {
  id: number
  plan_id: number
  name: string
  description: string | null
  milestone_id: number | null
  milestone_name: string | null
  source: ItemSource
  version: number
}

export interface EstimateLine {
  id: number
  plan_id: number
  description: string
  category: EstimateCategory
  amount: string
  source: ItemSource
  version: number
}

export const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

function ordinal(day: number): string {
  if (day % 100 >= 11 && day % 100 <= 13) return `${day}th`
  return `${day}${{ 1: 'st', 2: 'nd', 3: 'rd' }[day % 10] ?? 'th'}`
}

// "Weekly, due on Friday" or "Monthly, due on the 15th". The due day is a weekday (1 is Monday)
// for weekly and fortnightly reports, and a day of the month for monthly ones.
export function reportingText(period: ReportPeriod | null, day: number | null): string {
  if (!period || !day) return 'Not set'
  const name = period.charAt(0).toUpperCase() + period.slice(1)
  if (period === 'monthly') return `${name}, due on the ${ordinal(day)}`
  return `${name}, due on ${WEEKDAYS[day - 1] ?? `day ${day}`}`
}

// How the computed total compares with the brief's budget range.
export function budgetFitText(fit: BudgetFit): string {
  return {
    within: 'Within the budget range',
    below: 'Below the budget range',
    above: 'Above the budget range',
    no_range: 'The brief has no budget range',
  }[fit]
}

export function ownerText(milestone: Milestone): string {
  if (milestone.owner_user_id === null) return 'No owner'
  const name = milestone.owner_name ?? `User ${milestone.owner_user_id}`
  return milestone.owner_active ? name : `${name} (no longer active)`
}

// The kinds of plan item: the name the confirm route uses, and the path of each one's list.
export type ItemKind = 'milestone' | 'deliverable' | 'estimate_line'
export const ITEM_PATHS: Record<ItemKind, string> = {
  milestone: 'milestones',
  deliverable: 'deliverables',
  estimate_line: 'estimate-lines',
}

export const REPORT_PERIODS: ReportPeriod[] = ['weekly', 'fortnightly', 'monthly']
export const CATEGORIES: EstimateCategory[] = ['people', 'software', 'services', 'other']
// The API's limits (app/models/plan.py and the brief's long text).
export const CONTINGENCY_MAX = 50
export const NAME_MAX = 200
export const DESCRIPTION_MAX = 500
export const LONG_TEXT_MAX = 2000

// The highest due day each period allows: a weekday (1 is Monday), or a day every month has.
export function dueDayMax(period: ReportPeriod): number {
  return period === 'monthly' ? 28 : 7
}

// An amount as a person types it: digits with up to 2 decimal places, above zero and within the
// 12 digits the API stores. The API checks it again.
const MONEY = /^\d{1,10}(\.\d{1,2})?$/
export function amountError(value: string | null | undefined): string | null {
  const text = (value ?? '').trim()
  if (!text) return 'Required'
  if (!MONEY.test(text)) return 'An amount such as 25000 or 25000.50'
  if (Number(text) <= 0) return 'The amount must be above zero'
  return null
}

// Whether a person may change the plan here: project:write, and the API says it is editable (in
// planning and not cancelled). The web only hides the controls; the API checks both again.
export function canEditPlan(plan: Pick<Plan, 'editable'> | null, canWrite: boolean): boolean {
  return !!plan && plan.editable && canWrite
}

export interface PlanFieldsInput {
  scope_in: string | null
  scope_out: string | null
  contingency_pct: number
  report_period: ReportPeriod | null
  report_due_day: number | null
}

export interface MilestoneInput {
  name: string
  target_date: string
  owner_user_id: number | null
  description: string | null
  contributors: string | null
}

export interface DeliverableInput {
  name: string
  description: string | null
  milestone_id: number | null
}

export interface EstimateLineInput {
  description: string
  category: EstimateCategory
  amount: string
}

export type ItemInput = MilestoneInput | DeliverableInput | EstimateLineInput

const base = (id: number) => `/api/v1/projects/${id}/plan`
const PAGE = 100 // The most the API sends in one page.

// Every item of a paged list: follow `total` page by page. An empty page ends it early, so a list
// that shrinks while it is read can't loop.
export async function allPages<T>(path: string): Promise<T[]> {
  const items: T[] = []
  for (;;) {
    const page = await call<Page<T>>(`${path}?limit=${PAGE}&offset=${items.length}`)
    items.push(...page.items)
    if (!page.items.length || items.length >= page.total) return items
  }
}

// A DELETE answers 204 with no body.
async function remove(path: string): Promise<void> {
  const response = await apiFetch(path, { method: 'DELETE' })
  if (!response.ok) throw await apiError(response)
}

export const planApi = {
  get: (id: number) => call<Plan>(base(id)),
  milestones: (id: number) => allPages<Milestone>(`${base(id)}/milestones`),
  deliverables: (id: number) => allPages<Deliverable>(`${base(id)}/deliverables`),
  estimateLines: (id: number) => allPages<EstimateLine>(`${base(id)}/estimate-lines`),
  people: () => allPages<Person>('/api/v1/users'),
  // Every write carries a version: the plan's for the plan itself or an addition, the item's for
  // an edit, delete or confirmation of one item. A stale one is a 409.
  update: (id: number, version: number, body: Partial<PlanFieldsInput>) =>
    call<Plan>(base(id), { method: 'PATCH', body: JSON.stringify({ ...body, version }) }),
  addItem: (id: number, kind: ItemKind, version: number, body: ItemInput) =>
    call<unknown>(`${base(id)}/${ITEM_PATHS[kind]}`, {
      method: 'POST',
      body: JSON.stringify({ ...body, version }),
    }),
  updateItem: (id: number, kind: ItemKind, itemId: number, version: number, body: ItemInput) =>
    call<unknown>(`${base(id)}/${ITEM_PATHS[kind]}/${itemId}`, {
      method: 'PATCH',
      body: JSON.stringify({ ...body, version }),
    }),
  deleteItem: (id: number, kind: ItemKind, itemId: number, version: number) =>
    remove(`${base(id)}/${ITEM_PATHS[kind]}/${itemId}?version=${version}`),
  // Marks the agent's items a person's without changing them: one item, or all with the plan's
  // version.
  confirm: (id: number, version: number, item?: { kind: ItemKind; id: number }) =>
    call<Plan>(`${base(id)}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ ...(item ?? {}), version }),
    }),
}
