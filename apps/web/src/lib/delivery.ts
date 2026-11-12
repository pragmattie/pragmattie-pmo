// Delivery progress (item 7.2): what a project in delivery has reached, how its deliverables stand
// and what it has spent. The API measures days late in working days against the baseline target;
// the web only shows them, and hides what a role can't do. The API checks every rule again.

import { call, type Page } from './risks'

export type DeliverableStatus = 'not_started' | 'in_progress' | 'ready_for_acceptance' | 'accepted'

export interface MilestoneProgress {
  id: number
  name: string
  // The target in the current baseline; null for a milestone the baseline doesn't have.
  target_date: string | null
  plan_date: string
  reached_on: string | null
  reached_note: string | null
  days_late: number | null
  version: number
}

export interface DeliverableProgress {
  id: number
  name: string
  milestone_id: number | null
  status: DeliverableStatus
  version: number
}

export interface SpendEntry {
  id: number
  amount: string
  as_of: string
  note: string
  supersedes_id: number | null
  created_by: number
  created_by_name: string | null
  created_at: string
}

export interface Delivery {
  project_id: number
  project_stage: string
  project_status: string
  baseline_number: number | null
  baseline_total: string | null
  today: string
  milestones: MilestoneProgress[]
  deliverables: DeliverableProgress[]
  current_spend: SpendEntry | null
  spend: Page<SpendEntry>
}

export interface SpendInput {
  amount: string
  as_of: string
  note: string
  supersedes_id?: number
}

// The API's limits (app/models/plan.py and app/models/delivery.py).
export const MAX_REACHED_NOTE = 500
export const MAX_SPEND_NOTE = 200

export const STATUS_TEXT: Record<DeliverableStatus, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  ready_for_acceptance: 'Ready for acceptance',
  accepted: 'Accepted',
}

// The statuses a project manager may set here; only the acceptance step sets `accepted`.
export const SETTABLE_STATUSES: DeliverableStatus[] = [
  'not_started',
  'in_progress',
  'ready_for_acceptance',
]

function days(count: number): string {
  return `${count} working day${count === 1 ? '' : 's'}`
}

// Days late in words: late, early or on time against the baseline target.
export function daysLateText(milestone: Pick<MilestoneProgress, 'target_date' | 'days_late' | 'reached_on'>): string {
  if (milestone.target_date === null || milestone.days_late === null) return 'No baseline target'
  const late = milestone.days_late
  if (late > 0) return `${days(late)} late`
  if (late < 0) return `${days(-late)} early`
  return milestone.reached_on ? 'On time' : 'Not due yet'
}

export function isOpen(delivery: Pick<Delivery, 'project_stage' | 'project_status'>): boolean {
  return delivery.project_stage === 'delivery' && delivery.project_status !== 'cancelled'
}

const base = (projectId: number) => `/api/v1/projects/${projectId}/delivery`

export const deliveryApi = {
  get: (projectId: number, offset = 0) =>
    call<Delivery>(`${base(projectId)}?limit=50&offset=${offset}`),
  reach: (
    projectId: number,
    milestone: MilestoneProgress,
    reachedOn: string | null,
    note: string | null,
  ) =>
    call<Delivery>(`${base(projectId)}/milestones/${milestone.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        version: milestone.version,
        reached_on: reachedOn,
        reached_note: reachedOn ? note : null,
      }),
    }),
  setStatus: (projectId: number, deliverable: DeliverableProgress, status: DeliverableStatus) =>
    call<Delivery>(`${base(projectId)}/deliverables/${deliverable.id}`, {
      method: 'PUT',
      body: JSON.stringify({ version: deliverable.version, status }),
    }),
  spend: (projectId: number, body: SpendInput) =>
    call<Delivery>(`${base(projectId)}/spend`, { method: 'POST', body: JSON.stringify(body) }),
}
