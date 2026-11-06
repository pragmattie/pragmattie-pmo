// Stage gates: the approval gate on a submitted project request (item 4.4), and the go-ahead gate
// on a project in planning (item 5.4). The gate, its criteria and who decides it come from the API
// (the gates policy); the web only shows them and hides the decision buttons from roles that
// can't use them. The API checks everything again.

import { call, type Page, type Person } from './risks'

export type GateOutcome = 'pass' | 'return' | 'decline'
// What a gate decides: a project request, or a project.
export type GateSubject = 'request' | 'project'

export interface GateCriterion {
  id: string
  text: string
  kind: 'check' | 'person'
  check: string | null
  // An automatic check's result now; a person criterion's tick (null until a decision records one).
  met: boolean | null
  detail: string | null
  note: string | null
}

export interface GateDecision {
  id: number
  gate_id: string
  outcome: GateOutcome
  decided_by: number
  decided_by_name: string | null
  reason: string
  criteria: GateCriterion[]
  policy_version: string
  created_at: string
}

export interface GateInfo {
  id: string
  name: string
  from_stage: string
  to_stage: string
  applies_to: string
  deciders: string[]
}

interface GateBase {
  gate: GateInfo
  criteria: GateCriterion[]
  checks_met: boolean
  policy_version: string
  latest_decision: GateDecision | null
}

// A request at the approval gate.
export interface Gate extends GateBase {
  request_id: number
  request_version: number
  request_status: string
  requested_by: number
  // The project a pass made.
  project_id?: number | null
}

// A frozen copy of a project's plan, made by passing the go-ahead gate. Money is a 2-decimal
// string. It never changes.
export interface BaselinePlan {
  scope_in: string | null
  scope_out: string | null
  milestones: { id: number; name: string; target_date: string; owner_user_id: number | null }[]
  deliverables: { id: number; name: string; milestone_name: string | null }[]
  estimate_lines: { id: number; description: string; category: string; amount: string }[]
  contingency_pct: number
  lines_total: string
  contingency_amount: string
  total: string
}

export interface Baseline {
  id: number
  project_id: number
  number: number
  gate_decision_id: number
  plan: BaselinePlan
  total: string
  created_at: string
}

// A project at the go-ahead gate, with its latest baseline once there is one.
export interface ProjectGate extends GateBase {
  project_id: number
  project_version: number
  project_stage: string
  project_status: string
  pm_user_id: number | null
  baseline: Baseline | null
}

export type AnyGate = Gate | ProjectGate

function isProjectGate(gate: Pick<AnyGate, 'gate'>): gate is ProjectGate {
  return gate.gate.applies_to === 'project'
}

// The subject's version a decision sends back.
export function gateVersion(gate: AnyGate): number {
  return isProjectGate(gate) ? gate.project_version : gate.request_version
}

export interface PersonTick {
  id: string
  met: boolean
  note: string | null
}

export interface GateDecisionInput {
  version: number
  outcome: GateOutcome
  reason: string
  criteria: PersonTick[]
  // Only a pass names the new project's project manager; the API refuses one on the others.
  project_manager_id?: number
}

// The API's limits (app/models/gate.py).
export const MIN_GATE_REASON = 20
export const MAX_GATE_REASON = 2000
export const MAX_GATE_NOTE = 500

export interface OutcomeChoice {
  value: GateOutcome
  title: string
  color: string
  done: string // the message once it is decided
  explain: string // what the decision does, shown in its dialog
}

const CHOICES: Record<GateSubject, OutcomeChoice[]> = {
  request: [
    {
      value: 'pass',
      title: 'Pass',
      color: 'primary',
      done: 'Request approved at the gate, and its project created.',
      explain:
        'Passing creates the project, its brief, stakeholders and first risks. Name its project manager.',
    },
    {
      value: 'return',
      title: 'Return',
      color: 'secondary',
      done: 'Request returned to draft.',
      explain: 'The request goes back to draft so its project manager can work on it.',
    },
    {
      value: 'decline',
      title: 'Decline',
      color: 'error',
      done: 'Request declined.',
      explain: "A declined request is final: it can't be changed or submitted again.",
    },
  ],
  project: [
    {
      value: 'pass',
      title: 'Pass',
      color: 'primary',
      done: 'Project passed the go-ahead gate: it is in delivery and its plan is the baseline.',
      explain:
        'Passing moves the project to delivery and freezes its plan as the baseline. After that, ' +
        'the plan changes only through change requests.',
    },
    {
      value: 'return',
      title: 'Return',
      color: 'secondary',
      done: 'Project returned: it stays in planning.',
      explain: 'The project stays in planning so its project manager can work on the plan.',
    },
    {
      value: 'decline',
      title: 'Decline',
      color: 'error',
      done: 'Project declined and cancelled.',
      explain:
        "A declined project is cancelled: its plan, proposal and gate can't change after this.",
    },
  ],
}

export function outcomeChoices(subject: GateSubject): OutcomeChoice[] {
  return CHOICES[subject]
}

// The request's choices, as before the go-ahead gate.
export const OUTCOMES = CHOICES.request

const OUTCOME_TEXT: Record<GateSubject, Record<GateOutcome, string>> = {
  request: { pass: 'Passed', return: 'Returned to draft', decline: 'Declined' },
  project: { pass: 'Passed', return: 'Returned', decline: 'Declined' },
}

export function outcomeText(outcome: GateOutcome, subject: GateSubject = 'request'): string {
  return OUTCOME_TEXT[subject][outcome] ?? outcome
}

// The decision buttons: a role holding gate:decide that the gate lists, on a subject that can be
// decided now and isn't the person's own: a submitted request they didn't raise, or a project in
// planning, not cancelled, that they don't manage.
export function canDecideGate(
  gate: AnyGate,
  user: { id: number; role: string } | null,
  holdsDecide: boolean,
): boolean {
  if (!user || !holdsDecide || !gate.gate.deciders.includes(user.role)) return false
  if (isProjectGate(gate)) {
    return (
      gate.project_stage === 'planning' &&
      gate.project_status !== 'cancelled' &&
      gate.pm_user_id !== user.id
    )
  }
  return gate.request_status === 'submitted' && gate.requested_by !== user.id
}

// Pass needs every automatic check met and every person criterion ticked with a note.
export function readyToPass(gate: Pick<AnyGate, 'criteria'>, ticks: PersonTick[]): boolean {
  return gate.criteria.every((criterion) => {
    if (criterion.kind === 'check') return criterion.met === true
    const tick = ticks.find((t) => t.id === criterion.id)
    return !!tick && tick.met && !!tick.note?.trim()
  })
}

function gateUrl(subject: GateSubject, id: number): string {
  return `/api/v1/${subject === 'project' ? 'projects' : 'requests'}/${id}/gate`
}

export const gateApi = {
  // The people a sponsor may name as the new project's project manager.
  projectManagers: () => call<Page<Person>>('/api/v1/users?role=project_manager&limit=100'),
  get: (subject: GateSubject, id: number) => call<AnyGate>(gateUrl(subject, id)),
  decide: (subject: GateSubject, id: number, body: GateDecisionInput) =>
    call<AnyGate>(gateUrl(subject, id), { method: 'POST', body: JSON.stringify(body) }),
}
