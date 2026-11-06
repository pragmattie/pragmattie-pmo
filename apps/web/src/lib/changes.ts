// Change requests (item 5.5): once a project is in delivery, its scope, dates and cost change only
// through a change request a person decides. The API measures each change against the current
// baseline and names who decides it (the changes policy); the web only shows that, and hides what
// a role can't do. The API checks every rule again.

import { call, type Page } from './risks'

export type ChangeStatus = 'draft' | 'submitted' | 'approved' | 'rejected' | 'withdrawn'
export type ChangeDecider = 'project_manager' | 'sponsor'
export type ChangeOutcome = 'approve' | 'reject'

// What a change request changes; anything left empty stays as the baseline has it.
export interface ChangeSet {
  scope_in?: string | null
  scope_out?: string | null
  add_deliverables?: { name: string; description?: string | null; milestone_id: number | null }[]
  remove_deliverables?: number[]
  move_milestones?: { milestone_id: number; target_date: string }[]
  add_lines?: { description: string; category: string; amount: string }[]
  remove_lines?: number[]
  change_lines?: { line_id: number; description?: string; category?: string; amount?: string }[]
  contingency_pct?: number | null
}

// Computed by the API from the baseline; never sent by the web.
export interface ChangeEffect {
  baseline_number: number
  baseline_total: string
  new_total: string
  cost_difference: string
  cost_difference_pct: string | null
  max_later_working_days: number
  max_earlier_working_days: number
  scope_changes: boolean
  scope_changed: string[]
  escalations: string[]
  limits: { max_cost_increase_pct: string; max_date_move_working_days: number }
}

export interface ChangeRequest {
  id: number
  project_id: number
  title: string
  reason: string
  changes: ChangeSet
  effect: ChangeEffect
  decider: ChangeDecider
  measured_baseline_number: number
  status: ChangeStatus
  raised_by: number
  raised_by_name: string | null
  submitted_at: string | null
  decided_by: number | null
  decided_by_name: string | null
  decision_reason: string | null
  decided_at: string | null
  // The baseline an approved change produced.
  baseline_number: number | null
  version: number
  created_at: string
}

export interface ChangeLog extends Page<ChangeRequest> {
  project_stage: string
  project_status: string
  pm_user_id: number | null
  // The roles that decide a sponsor-level change (the go-ahead gate's deciders).
  sponsor_deciders: string[]
  current_baseline_number: number | null
  proposal_out_of_date: boolean
}

export interface ChangeInput {
  title: string
  reason: string
  changes: ChangeSet
}

// The API's limits (app/models/gate.py and app/models/change.py).
export const MIN_DECISION_REASON = 20
export const MAX_REASON = 2000
export const MAX_TITLE = 200

export const DECIDER_TEXT: Record<ChangeDecider, string> = {
  project_manager: 'Project manager',
  sponsor: 'Sponsor',
}

export const STATUS_TEXT: Record<ChangeStatus, string> = {
  draft: 'Draft',
  submitted: 'Submitted',
  approved: 'Approved',
  rejected: 'Rejected',
  withdrawn: 'Withdrawn',
}

export const ESCALATION_TEXT: Record<string, string> = {
  cost_above_limit: "the cost increase is above the project manager's limit",
  cost_increase_on_zero_baseline: 'it adds cost to a baseline of zero',
  dates_above_limit: "a milestone moves later than the project manager's limit",
  scope_change: 'it changes scope',
  raised_by_project_manager: 'the project manager raised it',
  no_project_manager: 'the project has no project manager',
}

function signed(amount: string): string {
  return amount.startsWith('-') ? amount : `+${amount}`
}

// The effect in one line: cost, dates and scope.
export function effectText(effect: ChangeEffect): string {
  const parts = [
    `Cost ${signed(effect.cost_difference)}` +
      (effect.cost_difference_pct !== null ? ` (${signed(effect.cost_difference_pct)}%)` : ''),
  ]
  if (effect.max_later_working_days > 0) {
    parts.push(`up to ${effect.max_later_working_days} working days later`)
  }
  if (effect.max_earlier_working_days > 0) {
    parts.push(`up to ${effect.max_earlier_working_days} working days earlier`)
  }
  parts.push(effect.scope_changes ? 'changes scope' : 'no scope change')
  return parts.join('; ')
}

// Who decides, and why it went past the project manager when it did.
export function deciderText(change: Pick<ChangeRequest, 'decider' | 'effect'>): string {
  const who = DECIDER_TEXT[change.decider]
  const why = change.effect.escalations.map((e) => ESCALATION_TEXT[e] ?? e)
  return why.length ? `${who}: ${why.join('; ')}` : `${who}: within their limits`
}

interface Viewer {
  id: number
  actions: string[]
  role: string
}

function has(user: Viewer, action: string): boolean {
  return user.actions.includes(action)
}

// Whether `user` may decide `change`: never their own; a sponsor-level change by a role the API
// lists in `sponsorDeciders` (the go-ahead gate's deciders), with `gate:decide`, who doesn't
// manage the project; a project-manager-level one only by the project's own project manager.
export function canDecideChange(
  change: Pick<ChangeRequest, 'decider' | 'raised_by' | 'status'>,
  user: Viewer | null,
  pmUserId: number | null,
  sponsorDeciders: readonly string[],
): boolean {
  if (!user || change.status !== 'submitted' || change.raised_by === user.id) return false
  if (change.decider === 'sponsor') {
    return (
      has(user, 'gate:decide') && sponsorDeciders.includes(user.role) && user.id !== pmUserId
    )
  }
  return has(user, 'project:write') && user.id === pmUserId
}

// Only its raiser may submit (or resubmit) a change, so nobody rewrites another person's change
// and then decides it.
export function canSubmitChange(
  change: Pick<ChangeRequest, 'raised_by'>,
  user: Viewer | null,
): boolean {
  return !!user && has(user, 'project:write') && user.id === change.raised_by
}

// Its raiser or the project's project manager may withdraw a draft or submitted change.
export function canWithdrawChange(
  change: Pick<ChangeRequest, 'raised_by' | 'status'>,
  user: Viewer | null,
  pmUserId: number | null,
): boolean {
  if (!user || !has(user, 'project:write')) return false
  if (change.status !== 'draft' && change.status !== 'submitted') return false
  return user.id === change.raised_by || user.id === pmUserId
}

// What a person fills in to raise a change; empty fields change nothing.
export interface ChangeForm {
  scopeIn: string
  scopeOut: string
  addDeliverable: string
  addDeliverableMilestone: number | null
  removeDeliverables: number[]
  moveMilestone: number | null
  moveTo: string
  addLineDescription: string
  addLineCategory: string
  addLineAmount: string
  changeLine: number | null
  changeLineAmount: string
  removeLines: number[]
  contingency: string
}

export function emptyChangeForm(): ChangeForm {
  return {
    scopeIn: '',
    scopeOut: '',
    addDeliverable: '',
    addDeliverableMilestone: null,
    removeDeliverables: [],
    moveMilestone: null,
    moveTo: '',
    addLineDescription: '',
    addLineCategory: 'people',
    addLineAmount: '',
    changeLine: null,
    changeLineAmount: '',
    removeLines: [],
    contingency: '',
  }
}

// The change set the form describes. It never carries an effect or decider: the API computes them.
export function changeSetFrom(form: ChangeForm): ChangeSet {
  const set: ChangeSet = {}
  if (form.scopeIn.trim()) set.scope_in = form.scopeIn.trim()
  if (form.scopeOut.trim()) set.scope_out = form.scopeOut.trim()
  if (form.addDeliverable.trim()) {
    set.add_deliverables = [
      { name: form.addDeliverable.trim(), milestone_id: form.addDeliverableMilestone },
    ]
  }
  if (form.removeDeliverables.length) set.remove_deliverables = [...form.removeDeliverables]
  if (form.moveMilestone !== null && form.moveTo) {
    set.move_milestones = [{ milestone_id: form.moveMilestone, target_date: form.moveTo }]
  }
  if (form.addLineDescription.trim() && form.addLineAmount.trim()) {
    set.add_lines = [
      {
        description: form.addLineDescription.trim(),
        category: form.addLineCategory,
        amount: form.addLineAmount.trim(),
      },
    ]
  }
  if (form.changeLine !== null && form.changeLineAmount.trim()) {
    set.change_lines = [{ line_id: form.changeLine, amount: form.changeLineAmount.trim() }]
  }
  if (form.removeLines.length) set.remove_lines = [...form.removeLines]
  if (form.contingency.trim() !== '') set.contingency_pct = Number(form.contingency)
  return set
}

export function isEmptyChangeSet(set: ChangeSet): boolean {
  return Object.keys(set).length === 0
}

const base = (projectId: number) => `/api/v1/projects/${projectId}/changes`

export const changeApi = {
  list: (projectId: number) => call<ChangeLog>(`${base(projectId)}?limit=100`),
  raise: (projectId: number, body: ChangeInput) =>
    call<ChangeRequest>(base(projectId), { method: 'POST', body: JSON.stringify(body) }),
  submit: (projectId: number, change: ChangeRequest) =>
    call<ChangeRequest>(`${base(projectId)}/${change.id}/submit`, {
      method: 'POST',
      body: JSON.stringify({ version: change.version }),
    }),
  withdraw: (projectId: number, change: ChangeRequest) =>
    call<ChangeRequest>(`${base(projectId)}/${change.id}/withdraw`, {
      method: 'POST',
      body: JSON.stringify({ version: change.version }),
    }),
  decide: (projectId: number, change: ChangeRequest, outcome: ChangeOutcome, reason: string) =>
    call<ChangeRequest>(`${base(projectId)}/${change.id}/decide`, {
      method: 'POST',
      body: JSON.stringify({ version: change.version, outcome, reason }),
    }),
}
