// Delivery progress (item 7.2): what a project in delivery has reached, how its deliverables stand
// and what it has spent. The API measures days late in working days against the baseline target;
// the web only shows them, and hides what a role can't do. The API checks every rule again.
//
// Acceptance (item 7.6): the project manager offers a deliverable that is ready for acceptance (to
// whom, when, by what means; nothing is sent from here) and records the client's answer. With no
// client the sponsor accepts. A simulated client answers by itself, labelled "Simulated client".

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

export type AcceptanceAnswer = 'accepted' | 'not_accepted'

// One offer or answer in a deliverable's history; an offer and its answer share a round.
export interface AcceptanceRound {
  id: number
  round: number
  kind: 'offer' | 'answer'
  offered_to: string | null
  offered_on: string | null
  means: string | null
  answer: AcceptanceAnswer | null
  answered_on: string | null
  missing: string | null
  note: string | null
  source: 'person' | 'simulated'
  recorded_by: number | null
  recorded_by_name: string | null
  created_at: string
}

export interface DeliverableProgress {
  id: number
  name: string
  milestone_id: number | null
  status: DeliverableStatus
  version: number
  // Every offer and answer, oldest first; `awaiting_answer` while the latest is an offer.
  acceptance: AcceptanceRound[]
  awaiting_answer: boolean
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
  // Who answers an offer: the client (recorded by the project manager), the simulated client, or
  // with no client the sponsor.
  has_client: boolean
  client_simulated: boolean
  pm_user_id: number | null
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

export interface OfferInput {
  offered_to: string
  offered_on: string
  means: string
  note?: string
}

export interface AnswerInput {
  answer: AcceptanceAnswer
  answered_on: string
  missing?: string
  note?: string
}

// The API's limits (app/models/plan.py and app/models/delivery.py).
export const MAX_REACHED_NOTE = 500
export const MAX_SPEND_NOTE = 200
export const MAX_OFFERED_TO = 200
export const MAX_MEANS = 100
export const MAX_ACCEPTANCE_TEXT = 1000

export const SIMULATED_LABEL = 'Simulated client'

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

// Offer for acceptance: a project manager, on a deliverable ready for acceptance whose last offer
// isn't still waiting for its answer.
export function canOffer(
  delivery: Pick<Delivery, 'project_stage' | 'project_status'>,
  deliverable: Pick<DeliverableProgress, 'status' | 'awaiting_answer'>,
  canWrite: boolean,
): boolean {
  return (
    isOpen(delivery) &&
    canWrite &&
    deliverable.status === 'ready_for_acceptance' &&
    !deliverable.awaiting_answer
  )
}

// Record the answer: on an offer waiting for one. For a client project, a role with
// project:write records the client's answer; with no client, a sponsor (gate:decide) who doesn't
// manage the project accepts.
export function canAnswer(
  delivery: Pick<Delivery, 'project_stage' | 'project_status' | 'has_client' | 'pm_user_id'>,
  deliverable: Pick<DeliverableProgress, 'status' | 'awaiting_answer'>,
  user: { id: number; role: string } | null,
  can: (action: string) => boolean,
): boolean {
  if (!user || !isOpen(delivery) || !deliverable.awaiting_answer) return false
  if (deliverable.status !== 'ready_for_acceptance') return false
  if (delivery.has_client) return can('project:write')
  return user.role === 'sponsor' && can('gate:decide') && delivery.pm_user_id !== user.id
}

// Who answers this project's offers, in words.
export function answererText(delivery: Pick<Delivery, 'has_client' | 'client_simulated'>): string {
  if (!delivery.has_client) return 'This project has no client: the sponsor accepts its deliverables.'
  if (delivery.client_simulated) return 'The simulated client answers each offer by itself.'
  return "The project manager records the client's answer."
}

// One line of a deliverable's history.
export function roundText(round: AcceptanceRound): string {
  if (round.kind === 'offer') {
    return `Round ${round.round}: offered to ${round.offered_to} on ${round.offered_on} by ${round.means}`
  }
  const who =
    round.source === 'simulated' ? SIMULATED_LABEL : (round.recorded_by_name ?? 'Unknown')
  const answer = round.answer === 'accepted' ? 'Accepted' : 'Not accepted'
  const missing = round.missing ? `; missing: ${round.missing}` : ''
  return `Round ${round.round}: ${answer} on ${round.answered_on} (recorded by ${who})${missing}`
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
  offer: (projectId: number, deliverable: DeliverableProgress, body: OfferInput) =>
    call<Delivery>(`${base(projectId)}/deliverables/${deliverable.id}/offer`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  answer: (projectId: number, deliverable: DeliverableProgress, body: AnswerInput) =>
    call<Delivery>(`${base(projectId)}/deliverables/${deliverable.id}/answer`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}
