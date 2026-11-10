// The decision inbox (item 6.3): every decision waiting on the signed-in person, with the facts it
// rests on and a recommendation the API's code wrote from them. Each item is decided with the
// existing form for its step, which calls the existing route; the API checks every rule again.

import { call, type Page } from './risks'

export interface InboxFact {
  key: string
  label: string
  value: string
  // A check's result: true or false; null for a plain fact.
  met: boolean | null
}

export interface InboxEntry {
  // `item`: waiting on a person. `window`: happens at `due_at` unless the step's role holds it.
  kind: 'item' | 'window'
  id: number
  step: string
  subject_type: 'request' | 'project' | 'risk' | 'proposal' | 'change_request'
  subject_id: number
  subject_title: string
  project_id: number | null
  project_name: string | null
  who: string
  named_user_id: number | null
  named_user_name: string | null
  reason: string
  handoff: string | null
  created_at: string
  action_id: number | null
  due_at: string | null
  facts: InboxFact[]
  recommendation: string
}

export const POLL_MS = 15_000
export const MIN_HOLD_REASON = 10
export const NO_DECISIONS =
  "Your role doesn't decide anything here; nothing will appear in this inbox."
// On an item for someone who decides nothing (the admin, when every approver contributed to a
// proposal copy): they act on why it came to them, outside the inbox.
export const NOT_YOURS_TO_DECIDE =
  "You don't approve or return this. Act on why it came to you; a person who may decide it will."
export const EMPTY = 'Your inbox is empty: nothing is waiting on you.'

// The actions that let a role decide something; a role with none of them has an empty inbox.
const DECIDING_ACTIONS = [
  'project:write',
  'gate:decide',
  'intake:decide',
  'proposal:approve',
  'risk:accept',
]

export function decidesAnything(can: (action: string) => boolean): boolean {
  return DECIDING_ACTIONS.some(can)
}

// Whether the API can route any item to this user: anyone who decides something, and the admin,
// who gets a proposal decision every approver contributed to (item 6.10) but never decides it.
export function receivesItems(can: (action: string) => boolean, role: string | undefined) {
  return decidesAnything(can) || role === 'admin'
}

const STEP_TITLES: Record<string, string> = {
  draft_value_case: 'Write the value case',
  draft_readiness: 'Complete the readiness check',
  draft_plan: 'Complete the plan',
  propose_risks: 'Look for risks',
  score_risk: 'Score a risk',
  open_low_risk: 'Open a Low risk',
  accept_risk: 'Accept or treat a risk',
  draft_proposal: 'Write the proposal',
  submit_proposal: 'Submit the proposal',
  approve_proposal: 'Approve or return the proposal',
  send_proposal: 'Send the proposal',
  client_answer: "Record the client's answer",
  pass_gate: 'Decide the gate',
  decide_change_pm: 'Decide a change request',
  decide_change_sponsor: 'Decide a change request',
}

export function stepTitle(step: string): string {
  return STEP_TITLES[step] ?? step.replaceAll('_', ' ')
}

export function roleText(role: string): string {
  return role.replaceAll('_', ' ')
}

// "the sponsor (Ada Example)" or "the project manager".
export function waitsOnText(entry: Pick<InboxEntry, 'who' | 'named_user_name'>): string {
  const role = `the ${roleText(entry.who)}`
  return entry.named_user_name ? `${role} (${entry.named_user_name})` : role
}

// The API sends UTC times without a zone; read them as UTC.
export function parseUtc(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
}

export function clock(value: string): string {
  const time = parseUtc(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(time.getHours())}:${pad(time.getMinutes())}`
}

export function dueText(entry: Pick<InboxEntry, 'due_at'>): string | null {
  return entry.due_at ? `happens at ${clock(entry.due_at)} unless you hold it` : null
}

// Hold shows only for a window action, and only to the step's role; the API checks it too.
export function mayHold(entry: Pick<InboxEntry, 'kind' | 'who'>, role: string | undefined) {
  return entry.kind === 'window' && role === entry.who
}

// Which existing form decides the item in place.
export type DecisionForm = 'gate' | 'risk' | 'proposal' | 'changes' | 'link'

export function formFor(entry: Pick<InboxEntry, 'step' | 'subject_type'>): DecisionForm {
  if (entry.step === 'pass_gate') return 'gate'
  if (entry.subject_type === 'risk') return 'risk'
  if (entry.subject_type === 'proposal') return 'proposal'
  if (entry.subject_type === 'change_request') return 'changes'
  return 'link'
}

// An item's key in the inbox list, and in the `item` query a page opened from it carries.
export function inboxKey(entry: Pick<InboxEntry, 'kind' | 'id'>): string {
  return `${entry.kind}-${entry.id}`
}

// The query a link in the inbox adds, so the page it opens shows Back to Inbox, which returns to
// the inbox with this item expanded.
export function fromInbox(entry: Pick<InboxEntry, 'kind' | 'id'>) {
  return { from: 'inbox', item: inboxKey(entry) }
}

// Where the subject lives, for steps done on its own page (drafting a value case or a plan).
export function subjectLink(entry: InboxEntry) {
  const query = fromInbox(entry)
  if (entry.subject_type === 'request') {
    return { name: 'request', params: { id: entry.subject_id }, query }
  }
  if (entry.project_id !== null) return { name: 'project', params: { id: entry.project_id }, query }
  return null
}

// The risk register at one risk, with its drawer open (Edit and Close on a risk in the inbox).
export function riskLink(entry: InboxEntry) {
  return {
    name: 'risks',
    query: { project: entry.project_id, risk: entry.subject_id, ...fromInbox(entry) },
  }
}

// "3 waiting", on the Inbox page and in the menu dot's label.
export function waitingText(count: number): string {
  return `${count} waiting`
}

export const inboxApi = {
  list: (offset = 0, limit = 50) =>
    call<Page<InboxEntry>>(`/api/v1/inbox?limit=${limit}&offset=${offset}`),
  count: () => call<{ count: number }>('/api/v1/inbox/count'),
  hold: (actionId: number, reason: string) =>
    call<{ inbox_item_id: number }>(`/api/v1/orchestrator/actions/${actionId}/hold`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
}
