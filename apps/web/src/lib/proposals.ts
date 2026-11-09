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

// Who wrote a text section (item 5.3): the agent, a person, or nobody yet (empty, or prefilled
// from the plan). `unreviewed` is an agent's draft no person has edited or confirmed: it shows
// "Agent draft, not reviewed", and the API refuses submit while any section is one.
export interface SectionState {
  source: 'agent' | 'person' | null
  reviewed_by: number | null
  reviewed_at: string | null
  unreviewed: boolean
}

export interface Proposal extends ProposalContent {
  id: number
  project_id: number
  project_name: string
  client_name: string
  // The demo's simulated client (its contact email is a .example address): its answer comes
  // from the simulation, never from a person.
  client_simulated: boolean
  status: ProposalStatus
  plan_version_seen: number
  plan_changed: boolean
  flags: Flag[]
  sections: Record<TextSection, SectionState>
  unreviewed_sections: TextSection[]
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
  // Null only on the simulated client's answer.
  actor_user_id: number | null
  actor_name: string | null
  source: 'person' | 'simulated'
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
  // Null when the orchestrator submitted it.
  submitted_by: number | null
  submitted_by_name: string | null
  submitted_at: string
  events: ProposalEvent[]
}

// The answer of a proposal agent run. A shadow run saves nothing; `replaced` is then empty.
export interface DraftResult {
  mode: 'shadow' | 'enforce'
  dry_run: boolean
  sections: Partial<Record<TextSection, string>>
  removed: { section: TextSection; kind: 'money' | 'date' | 'token'; text: string }[]
  dropped: { part: string; reason: string }[]
  replaced: TextSection[]
  kept: { section: TextSection; reason: string }[]
  flags: Flag[]
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
  stray_money: "A money figure that is neither the plan's total nor one of its estimate lines",
  stray_date: 'A date that is not a milestone date',
  missing_deliverable: 'A deliverable in the plan that the proposal lacks',
  missing_milestone: 'A milestone in the plan that the proposal lacks',
  removed_deliverable: 'A deliverable the plan no longer has',
  removed_milestone: 'A milestone the plan no longer has',
  changed_deliverable: 'A deliverable whose description has changed in the plan',
  changed_milestone: 'A milestone whose description has changed in the plan',
  stale_price: 'The price no longer matches the plan',
  changed_estimate_line: 'An estimate line that has changed in the plan',
  missing_estimate_line: 'An estimate line in the plan that the proposal lacks',
  removed_estimate_line: 'An estimate line the plan no longer has',
  closed_risk: 'A risk shown that has since been closed or rejected',
  risk_changed: 'A risk shown, or ticked to show, has changed in the risk register',
}

export function ruleText(rule: string): string {
  return RULE_TEXT[rule] ?? rule
}

// The flags that concern one section, to show beside it.
export function flagsFor(flags: Flag[], section: string): Flag[] {
  return flags.filter((flag) => flag.section === section)
}

// The sections' titles, in the order the proposal shows them.
function titles(keys: TextSection[]): string {
  return TEXT_SECTIONS.filter((s) => keys.includes(s.key))
    .map((s) => s.title)
    .join(', ')
}

// What an agent run did, in a sentence or two for the project manager.
export function draftText(result: DraftResult): string {
  const parts: string[] = []
  if (result.mode === 'shadow') {
    parts.push('Shadow mode: the agent drafted the text but nothing was saved.')
  } else if (result.replaced.length) {
    parts.push(`The agent drafted: ${titles(result.replaced)}. Review or edit each one.`)
  } else {
    parts.push('The agent changed no section.')
  }
  if (result.kept.length) {
    const kept = result.kept.map((k) => `${titles([k.section])} (${k.reason})`)
    parts.push(`Kept: ${kept.join('; ')}.`)
  }
  if (result.removed.length) {
    const n = result.removed.length
    parts.push(`Code removed ${n} ${n === 1 ? 'figure, date or token' : 'figures, dates or tokens'} the agent wrote.`)
  }
  return parts.join(' ')
}

export interface ProposalButtons {
  edit: boolean
  // Drafting with the agent and confirming its sections: only on a draft.
  agent: boolean
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
    agent: writer && status === 'draft',
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
  if (proposal.status === 'withdrawn') {
    return `The sponsor approved version ${proposal.approved_version}, but the proposal was withdrawn.`
  }
  return (
    `The sponsor approved version ${proposal.approved_version}, an earlier version: ` +
    'the proposal has changed since.'
  )
}

export const SIMULATED_CLIENT = 'Simulated client'

// Who submitted a version: a person, or the orchestrator once every section was reviewed.
export function submitterText(version: ProposalVersion): string {
  if (version.submitted_by === null) return 'the orchestrator'
  return version.submitted_by_name ?? `User ${version.submitted_by}`
}

export function eventText(event: ProposalEvent): string {
  if (event.source === 'simulated') {
    return (
      `${SIMULATED_CLIENT}: ${event.answer} on ${event.answered_on}, answered by the demo's ` +
      'simulation, not a real client' +
      (event.note ? `: ${event.note}` : '')
    )
  }
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

// Why the approved copy may no longer be sent, or null while it is current. Only a proposal that
// is approved, sent or answered with no edit since its approval is current.
export function outOfDateText(proposal: Proposal): string | null {
  if (proposal.approved_version === null || proposal.approval_current) return null
  if (proposal.status === 'withdrawn') {
    return 'This proposal was withdrawn. This approved copy is out of date and must not be sent.'
  }
  return (
    'The proposal has been edited since this copy was approved. This approved copy is out of ' +
    'date and must not be sent.'
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
  draft: (id: number) => call<DraftResult>(`${base(id)}/draft`, { method: 'POST' }),
  confirm: (id: number, version: number, section: TextSection) =>
    post<Proposal>(`${base(id)}/sections/${section}/confirm`, { version }),
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
