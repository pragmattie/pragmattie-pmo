// Stage gates (item 4.4): the approval gate on a submitted project request. The gate, its
// criteria and who decides it come from the API (the gates policy); the web only shows them and
// hides the decision buttons from roles that can't use them. The API checks everything again.

import { call } from './risks'

export type GateOutcome = 'pass' | 'return' | 'decline'

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

export interface Gate {
  gate: GateInfo
  request_id: number
  request_version: number
  request_status: string
  requested_by: number
  criteria: GateCriterion[]
  checks_met: boolean
  policy_version: string
  latest_decision: GateDecision | null
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
}

// The API's limits (app/models/gate.py).
export const MIN_GATE_REASON = 20
export const MAX_GATE_REASON = 2000
export const MAX_GATE_NOTE = 500

export const OUTCOMES: { value: GateOutcome; title: string; color: string; done: string }[] = [
  { value: 'pass', title: 'Pass', color: 'primary', done: 'Request approved at the gate.' },
  { value: 'return', title: 'Return', color: 'secondary', done: 'Request returned to draft.' },
  { value: 'decline', title: 'Decline', color: 'error', done: 'Request declined.' },
]

const OUTCOME_TEXT: Record<GateOutcome, string> = {
  pass: 'Passed',
  return: 'Returned to draft',
  decline: 'Declined',
}

export function outcomeText(outcome: GateOutcome): string {
  return OUTCOME_TEXT[outcome] ?? outcome
}

// The decision buttons: a role holding gate:decide that the gate lists, on a submitted request
// the person didn't raise themselves.
export function canDecideGate(
  gate: Pick<Gate, 'gate' | 'request_status' | 'requested_by'>,
  user: { id: number; role: string } | null,
  holdsDecide: boolean,
): boolean {
  if (!user || !holdsDecide) return false
  return (
    gate.gate.deciders.includes(user.role) &&
    gate.request_status === 'submitted' &&
    gate.requested_by !== user.id
  )
}

// Pass needs every automatic check met and every person criterion ticked with a note.
export function readyToPass(gate: Pick<Gate, 'criteria'>, ticks: PersonTick[]): boolean {
  return gate.criteria.every((criterion) => {
    if (criterion.kind === 'check') return criterion.met === true
    const tick = ticks.find((t) => t.id === criterion.id)
    return !!tick && tick.met && !!tick.note?.trim()
  })
}

const base = '/api/v1/requests'

export const gateApi = {
  get: (id: number) => call<Gate>(`${base}/${id}/gate`),
  decide: (id: number, body: GateDecisionInput) =>
    call<Gate>(`${base}/${id}/gate`, { method: 'POST', body: JSON.stringify(body) }),
}
