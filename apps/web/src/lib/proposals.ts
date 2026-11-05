// A client project's proposal (item 5.2): the offer the client sees, built from the plan. The API
// copies the price, dates, deliverables and shown risks from the plan, runs the checker and
// decides who may move the proposal on; the web only shows them and hides what a role can't do.

import { allPages } from './plans'
import { call } from './risks'

export type ProposalStatus =
  | 'draft'
  | 'submitted'
  | 'approved'
  | 'sent'
  | 'accepted'
  | 'declined'
  | 'withdrawn'

export type TextSection = 'summary' | 'scope_in' | 'scope_out' | 'assumptions' | 'terms'

export interface Flag {
  rule: string
  // A text section, or `price`, `deliverables`, `milestones` or `risks`.
  section: string
  text: string
}

export interface ProposalContent {
  summary: string | null
  scope_in: string | null
  scope_out: string | null
  assumptions: string | null
  terms: string | null
  // Money is a string with 2 decimal places, copied from the plan by the API.
  price: string
  estimate_lines: { description: string; category: string; amount: string }[]
  milestones: { name: string; target_date: string; description: string | null }[]
  deliverables: { name: string; description: string | null; milestone: string | null }[]
  // Title and response only: a proposal never shows a risk's score or band.
  risks: { risk_id: number; title: string; response: string; response_plan: string }[]
}

export interface Proposal extends ProposalContent {
  id: number
  project_id: number
  project_name: string
  client_name: string
  status: ProposalStatus
  plan_version_seen: number
  plan_changed: boolean
  flags: Flag[]
  latest_version: number | null
  submitted_by: number | null
  submitted_by_name: string | null
  approved_version: number | null
  approval_current: boolean
  version: number
  updated_at: string
}

export interface ProposalEvent {
  id: number
  kind: 'approve' | 'return' | 'sent' | 'answer' | 'withdraw'
  actor_user_id: number
  actor_name: string | null
  reason: string | null
  sent_to: string | null
  sent_on: string | null
  means: string | null
  answer: 'accepted' | 'declined' | null
  answered_on: string | null
  note: string | null
  created_at: string
}

export interface ProposalVersion {
  id: number
  number: number
  content: ProposalContent & { project_name?: string; client_name?: string }
  submitted_by: number
  submitted_by_name: string | null
  submitted_at: string
  events: ProposalEvent[]
}

export const TEXT_SECTIONS: { key: TextSection; title: string }[] = [
  { key: 'summary', title: 'Summary' },
  { key: 'scope_in', title: 'Scope in' },
  { key: 'scope_out', title: 'Scope out' },
  { key: 'assumptions', title: 'Assumptions' },
  { key: 'terms', title: 'Terms' },
]

export const STATUS_TEXT: Record<ProposalStatus, string> = {
  draft: 'Draft',
  submitted: 'Submitted to the sponsor',
  approved: 'Approved by the sponsor',
  sent: 'Sent to the client',
  accepted: 'Accepted by the client',
  declined: 'Declined by the client',
  withdrawn: 'Withdrawn',
}

const RULE_TEXT: Record<string, string> = {
  stray_money: 'A money figure that is neither the total nor an estimate line',
  stray_date: 'A date that is not a milestone date',
  missing_deliverable: 'A deliverable in the plan that the proposal lacks',
  missing_milestone: 'A milestone in the plan that the proposal lacks',
  removed_deliverable: 'A deliverable the plan no longer has',
  removed_milestone: 'A milestone the plan no longer has',
  stale_price: 'The price no longer matches the plan',
  closed_risk: 'A risk shown that has since been closed or rejected',
  risk_changed: 'The risks ticked to show have changed',
}

export function ruleText(rule: string): string {
  return RULE_TEXT[rule] ?? rule
}

// The flags that concern one section, to show beside it.
export function flagsFor(flags: Flag[], section: string): Flag[] {
  return flags.filter((flag) => flag.section === section)
}

export interface ProposalButtons {
  edit: boolean
  submit: boolean
  decide: boolean
  sent: boolean
  answer: boolean
  withdraw: boolean
}

// The buttons a role sees for a proposal's status. The API decides; this only hides. A sponsor
// never sees the decision buttons on a copy they submitted themselves.
export function buttonsFor(
  proposal: Proposal,
  can: (action: string) => boolean,
  userId: number | null,
): ProposalButtons {
  const writer = can('project:write')
  const status = proposal.status
  return {
    edit: writer && ['draft', 'submitted', 'approved'].includes(status),
    submit: writer && status === 'draft',
    decide:
      can('proposal:approve') && status === 'submitted' && proposal.submitted_by !== userId,
    sent: writer && status === 'approved',
    answer: writer && status === 'sent',
    withdraw: writer && ['draft', 'submitted', 'approved', 'sent'].includes(status),
  }
}

// "Approved as version 2", and whether that approval still covers what the proposal says now.
export function approvalText(proposal: Proposal): string | null {
  if (proposal.approved_version === null) return null
  if (proposal.approval_current) return `Approved as version ${proposal.approved_version}.`
  return (
    `The sponsor approved version ${proposal.approved_version}, an earlier version: ` +
    'the proposal has changed since.'
  )
}

export function eventText(event: ProposalEvent): string {
  const who = event.actor_name ?? `User ${event.actor_user_id}`
  switch (event.kind) {
    case 'approve':
      return `Approved by ${who}: ${event.reason}`
    case 'return':
      return `Returned by ${who}: ${event.reason}`
    case 'sent':
      return `Sent by ${who} to ${event.sent_to} on ${event.sent_on} by ${event.means}`
    case 'answer':
      return (
        `The client ${event.answer} it on ${event.answered_on} (recorded by ${who})` +
        (event.note ? `: ${event.note}` : '')
      )
    case 'withdraw':
      return `Withdrawn by ${who}: ${event.reason}`
  }
}

// The approved copy the print view shows: the newest version with an approval, if any.
export function approvedCopy(versions: ProposalVersion[]): ProposalVersion | null {
  const approved = versions.filter((v) => v.events.some((e) => e.kind === 'approve'))
  return approved.reduce<ProposalVersion | null>(
    (best, v) => (best === null || v.number > best.number ? v : best),
    null,
  )
}

const base = (id: number) => `/api/v1/projects/${id}/proposal`
const post = <T>(path: string, body: unknown) =>
  call<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const proposalApi = {
  get: (id: number) => call<Proposal>(base(id)),
  create: (id: number) => call<Proposal>(base(id), { method: 'POST' }),
  update: (id: number, version: number, changes: Partial<Record<TextSection, string | null>>) =>
    call<Proposal>(base(id), { method: 'PATCH', body: JSON.stringify({ version, ...changes }) }),
  refresh: (id: number, version: number) => post<Proposal>(`${base(id)}/refresh`, { version }),
  submit: (id: number, version: number) => post<Proposal>(`${base(id)}/submit`, { version }),
  approve: (id: number, version: number, reason: string) =>
    post<Proposal>(`${base(id)}/approve`, { version, reason }),
  return: (id: number, version: number, reason: string) =>
    post<Proposal>(`${base(id)}/return`, { version, reason }),
  sent: (id: number, version: number, sentTo: string, sentOn: string, means: string) =>
    post<Proposal>(`${base(id)}/sent`, { version, sent_to: sentTo, sent_on: sentOn, means }),
  answer: (
    id: number,
    version: number,
    answer: 'accepted' | 'declined',
    answeredOn: string,
    note: string | null,
  ) =>
    post<Proposal>(`${base(id)}/answer`, { version, answer, answered_on: answeredOn, note }),
  withdraw: (id: number, version: number, reason: string) =>
    post<Proposal>(`${base(id)}/withdraw`, { version, reason }),
  // Every page of the history, newest first.
  versions: (id: number) => allPages<ProposalVersion>(`${base(id)}/versions`),
}
