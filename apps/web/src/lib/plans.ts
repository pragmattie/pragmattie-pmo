// A project's plan (item 5.1): scope and deliverables, milestones, the estimate, who does what and
// the reporting plan. The API computes the total and the `missing` list; the web only shows them.

import type { Page } from './risks'
import { call } from './risks'

export type ReportPeriod = 'weekly' | 'fortnightly' | 'monthly'
export type BudgetFit = 'within' | 'below' | 'above' | 'no_range'
export type EstimateCategory = 'people' | 'software' | 'services' | 'other'
// Who wrote a plan item (item 6.5): the planner agent's draft, or a person who added or edited it.
export type ItemSource = 'agent' | 'person'

// How the plan marks an item the planner agent drafted, so a person can tell it apart.
export const DRAFTED_BY_AGENT = 'Drafted by the planner agent'

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

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

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

export const planApi = {
  get: (id: number) => call<Plan>(base(id)),
  milestones: (id: number) => allPages<Milestone>(`${base(id)}/milestones`),
  deliverables: (id: number) => allPages<Deliverable>(`${base(id)}/deliverables`),
  estimateLines: (id: number) => allPages<EstimateLine>(`${base(id)}/estimate-lines`),
}
