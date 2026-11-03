// Project requests and the queue: shapes, API calls and the pure helpers the Intake pages use.
// Which buttons show is only a convenience: the API checks every permission and status again.

import { ApiError, call, type HistoryEntry, type Page } from './risks'

export type RequestStatus = 'draft' | 'submitted' | 'approved' | 'declined' | 'withdrawn'
export type Sort = 'submitted_at' | '-submitted_at' | 'wanted_by' | '-wanted_by'

export interface ProjectRequest {
  id: number
  title: string
  client_id: number | null
  client_name: string | null
  asked_for: string
  problem: string
  outcome: string
  // Money is a string with 2 decimal places.
  budget_low: string | null
  budget_high: string | null
  wanted_by: string | null
  uses_ai: boolean
  known_risks: string | null
  requested_by: number
  requested_by_name: string | null
  status: RequestStatus
  submitted_at: string | null
  version: number
  created_at: string
  updated_at: string
}

// What a project manager fills in to add or edit a request.
export interface RequestInput {
  title: string
  client_id: number | null
  asked_for: string
  problem: string
  outcome: string
  budget_low: string | null
  budget_high: string | null
  wanted_by: string | null
  uses_ai: boolean
  known_risks: string | null
}

export interface Benefit {
  description: string
  measure: string | null
  current_value: string | null
  target: string | null
  target_date: string | null
}

// Why a request is worth doing, how we will know, and what it will cost. Part of its request: it
// changes only while the request is a draft, and saving it bumps the request's version.
export interface ValueCase {
  request_id: number
  request_version: number
  benefits: Benefit[]
  // Our estimate of the cost, as 2-dp strings; not linked to the request's budget available.
  cost_low: string | null
  cost_high: string | null
  cost_method: string | null
  strategic_fit: string
  cost_of_not_doing: string | null
  source: 'person' | 'agent'
  confirmed_by: number | null
  confirmed_by_name: string | null
  confirmed_at: string | null
  created_at: string
  updated_at: string
}

export interface ValueCaseInput {
  benefits: Benefit[]
  cost_low: string | null
  cost_high: string | null
  cost_method: string | null
  strategic_fit: string
  cost_of_not_doing: string | null
}

export type Rating = 'green' | 'yellow' | 'red'
export type ReadinessAreaName = 'problem' | 'approach' | 'data'

// Owner and due date are empty only on an agent's draft, until a person adds them.
export interface ReadinessAction {
  description: string
  owner_id: number | null
  owner_name: string | null
  due_date: string | null
}

export interface ReadinessArea {
  // Empty only on an agent's draft that left the area unrated.
  rating: Rating | null
  note: string | null
  actions: ReadinessAction[]
}

// Whether a request that uses AI is ready: three areas rated green, yellow or red. Part of its
// request like the value case. `overall` is the worst area, or "unrated" until a person confirms.
export interface Readiness {
  request_id: number
  request_version: number
  problem: ReadinessArea
  approach: ReadinessArea
  data: ReadinessArea
  overall: Rating | 'unrated'
  source: 'person' | 'agent'
  confirmed_by: number | null
  confirmed_by_name: string | null
  confirmed_at: string | null
  created_at: string
  updated_at: string
}

export interface ReadinessActionInput {
  description: string
  owner_id: number | null
  due_date: string
}

export interface ReadinessAreaInput {
  rating: Rating | null
  note: string
  actions: ReadinessActionInput[]
}

export type ReadinessInput = Record<ReadinessAreaName, ReadinessAreaInput>

// What the intake agent proposed (POST /requests/{id}/draft), after the API checked it. A dry run
// has `request` and `cost_ceiling_usd` instead; `saved` names the sections stored as unconfirmed
// agent drafts, `skipped` the ones a person had confirmed and the agent left alone.
export interface DraftResult {
  mode: 'shadow' | 'enforce'
  dry_run: boolean
  model: string
  prompt_version: string
  cost_ceiling_usd: string | null
  saved: ('value_case' | 'readiness')[]
  skipped: { section: 'value_case' | 'readiness'; reason: string }[]
  dropped: { part: string; reason: string }[]
  adjusted: { part: string; change: string }[]
  audit_id: number | null
}

export interface Client {
  id: number
  name: string
}

export interface QueueFilters {
  status: RequestStatus | null
  client_id: number | null
  uses_ai: boolean | null
}

export const NO_QUEUE_FILTERS: QueueFilters = { status: null, client_id: null, uses_ai: null }

export const REQUEST_STATUSES: RequestStatus[] = [
  'draft',
  'submitted',
  'approved',
  'declined',
  'withdrawn',
]

export const SORTS: { title: string; value: Sort }[] = [
  { title: 'Submitted, oldest first', value: 'submitted_at' },
  { title: 'Submitted, newest first', value: '-submitted_at' },
  { title: 'Wanted by, soonest first', value: 'wanted_by' },
  { title: 'Wanted by, latest first', value: '-wanted_by' },
]

// The same limits the API holds (app/models/intake.py).
export const MAX_TITLE = 200
export const MAX_TEXT = 2000
export const MAX_MEASURE = 500
export const MAX_TARGET = 200
export const MAX_BENEFITS = 10
export const MAX_ACTION = 500
// Actions per readiness area.
export const MAX_ACTIONS = 10

export const READINESS_AREAS: { key: ReadinessAreaName; title: string; question: string }[] = [
  { key: 'problem', title: 'Problem', question: 'The problem is real and worth solving.' },
  {
    key: 'approach',
    title: 'Approach',
    question: 'AI is the right tool, rather than simpler rules or software.',
  },
  {
    key: 'data',
    title: 'Data',
    question: 'The data the work needs exists, can be used, and is good enough.',
  },
]

export const RATINGS: Rating[] = ['green', 'yellow', 'red']

export const REQUEST_CONFLICT =
  'Someone changed this request after you opened it. The latest version is shown below; ' +
  'check it and make your change again.'

// A request may be edited and submitted only while it is a draft.
export function canEditRequest(request: Pick<ProjectRequest, 'status'>, canWrite: boolean) {
  return canWrite && request.status === 'draft'
}

export function canSubmitRequest(request: Pick<ProjectRequest, 'status'>, canWrite: boolean) {
  return canWrite && request.status === 'draft'
}

// The intake agent drafts only for a draft request, and only for a role with project:write.
export function canDraftRequest(request: Pick<ProjectRequest, 'status'>, canWrite: boolean) {
  return canWrite && request.status === 'draft'
}

// A section an agent drafted that no person has confirmed yet: it shows the "Agent draft" chip.
export function isAgentDraft(section: Pick<ValueCase, 'source' | 'confirmed_by'>): boolean {
  return section.source === 'agent' && section.confirmed_by === null
}

// What the page says after a draft run: what was saved, or that nothing was.
export function draftText(result: Pick<DraftResult, 'mode' | 'saved' | 'skipped'>): string {
  const names = { value_case: 'value case', readiness: 'readiness check' }
  if (result.mode === 'shadow') {
    return 'The agent ran in shadow mode: nothing was saved. Its proposal is in the audit trail.'
  }
  const skipped = result.skipped.length
    ? ` Left alone, because a person confirmed it: ${result.skipped.map((s) => names[s.section]).join(' and ')}.`
    : ''
  if (!result.saved.length) return `The agent saved nothing.${skipped}`
  return (
    `The agent drafted the ${result.saved.map((s) => names[s]).join(' and ')}. ` +
    `Check every part and save it to confirm it.${skipped}`
  )
}

// The value case changes with its request: only while the request is a draft.
export function canEditValueCase(request: Pick<ProjectRequest, 'status'>, canWrite: boolean) {
  return canWrite && request.status === 'draft'
}

export function costText(valueCase: Pick<ValueCase, 'cost_low' | 'cost_high'>): string {
  const low = moneyText(valueCase.cost_low)
  const high = moneyText(valueCase.cost_high)
  if (low && high) return `${low} to ${high}`
  if (low) return `From ${low}`
  if (high) return `Up to ${high}`
  return 'Not estimated yet'
}

// Who confirmed the value case, or that an agent's draft still needs a person to confirm it.
export function confirmationText(
  valueCase: Pick<ValueCase, 'source' | 'confirmed_by' | 'confirmed_by_name' | 'confirmed_at'>,
): string {
  if (valueCase.confirmed_by === null) {
    return valueCase.source === 'agent'
      ? 'Drafted by an agent; a person must confirm it before the request can be submitted'
      : 'Not confirmed'
  }
  const who = valueCase.confirmed_by_name ?? `User ${valueCase.confirmed_by}`
  const when = valueCase.confirmed_at ? ` on ${new Date(valueCase.confirmed_at).toLocaleString()}` : ''
  const drafted = valueCase.source === 'agent' ? ' (drafted by an agent)' : ''
  return `Confirmed by ${who}${when}${drafted}`
}

// The readiness check changes with its request: only while the request is a draft.
export function canEditReadiness(request: Pick<ProjectRequest, 'status'>, canWrite: boolean) {
  return canWrite && request.status === 'draft'
}

// The Vuetify colour of a rating chip.
export function ratingColor(rating: Rating | 'unrated' | null): string {
  if (rating === 'green') return 'success'
  if (rating === 'yellow') return 'warning'
  if (rating === 'red') return 'error'
  return 'grey'
}

export function ratingText(rating: Rating | 'unrated' | null): string {
  if (rating === null || rating === 'unrated') return 'Unrated'
  return rating[0].toUpperCase() + rating.slice(1)
}

// Today as YYYY-MM-DD in the browser's own time zone.
export function localDate(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

// An action whose due date has passed. Past dates are allowed, so they are flagged, not refused.
// An action with no date yet (an agent's draft) is never overdue.
export function isOverdue(action: Pick<ReadinessAction, 'due_date'>, now: Date = new Date()) {
  return action.due_date !== null && action.due_date < localDate(now)
}

export function ownerText(action: Pick<ReadinessAction, 'owner_id' | 'owner_name'>): string {
  if (action.owner_id === null) return 'No owner yet'
  return `owner ${action.owner_name ?? `User ${action.owner_id}`}`
}

export function dueText(action: Pick<ReadinessAction, 'due_date'>): string {
  return action.due_date === null ? 'No date yet' : `due ${action.due_date}`
}

// A draft or submitted request may be withdrawn; declined and withdrawn are final.
export function canWithdrawRequest(request: Pick<ProjectRequest, 'status'>, canWrite: boolean) {
  return canWrite && (request.status === 'draft' || request.status === 'submitted')
}

// The query string for one page of the queue.
export function queueQuery(filters: QueueFilters, sort: Sort, page: number, perPage: number) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== null && value !== '') params.set(key, String(value))
  }
  params.set('sort', sort)
  params.set('limit', String(perPage))
  params.set('offset', String((page - 1) * perPage))
  return params.toString()
}

// "10000.00" as "$10,000.00"; empty when there is no amount.
export function moneyText(value: string | null): string {
  if (value === null) return ''
  const [whole, cents = '00'] = value.split('.')
  return `$${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${cents}`
}

// What the client has in mind ("Budget available"); our own estimate is the value case's cost.
export function budgetText(request: Pick<ProjectRequest, 'budget_low' | 'budget_high'>): string {
  const low = moneyText(request.budget_low)
  const high = moneyText(request.budget_high)
  if (low && high) return `${low} to ${high}`
  if (low) return `From ${low}`
  if (high) return `Up to ${high}`
  return 'Not given'
}

// A request with no client is internal work.
export function clientText(request: Pick<ProjectRequest, 'client_name'>): string {
  return request.client_name ?? 'Internal'
}

const base = '/api/v1/requests'

export const intakeApi = {
  clients: () => call<Page<Client>>('/api/v1/clients?limit=100'),
  list: (query: string) => call<Page<ProjectRequest>>(`${base}?${query}`),
  get: (id: number) => call<ProjectRequest>(`${base}/${id}`),
  history: (id: number, offset = 0) =>
    call<Page<HistoryEntry>>(`${base}/${id}/history?limit=50&offset=${offset}`),
  create: (body: RequestInput) =>
    call<ProjectRequest>(base, { method: 'POST', body: JSON.stringify(body) }),
  update: (id: number, body: RequestInput & { version: number }) =>
    call<ProjectRequest>(`${base}/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  // Resolves to null when the request has no value case yet.
  valueCase: async (id: number): Promise<ValueCase | null> => {
    try {
      return await call<ValueCase>(`${base}/${id}/value-case`)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  },
  saveValueCase: (id: number, body: ValueCaseInput & { version: number }) =>
    call<ValueCase>(`${base}/${id}/value-case`, { method: 'PUT', body: JSON.stringify(body) }),
  // Resolves to null when the request has no readiness check yet.
  readiness: async (id: number): Promise<Readiness | null> => {
    try {
      return await call<Readiness>(`${base}/${id}/readiness`)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  },
  saveReadiness: (id: number, body: ReadinessInput & { version: number }) =>
    call<Readiness>(`${base}/${id}/readiness`, { method: 'PUT', body: JSON.stringify(body) }),
  // Runs the intake agent; the API refuses (409) when AGENT_MODE is off.
  draft: (id: number) => call<DraftResult>(`${base}/${id}/draft`, { method: 'POST' }),
  submit: (id: number, version: number) =>
    call<ProjectRequest>(`${base}/${id}/submit`, {
      method: 'POST',
      body: JSON.stringify({ version }),
    }),
  withdraw: (id: number, version: number) =>
    call<ProjectRequest>(`${base}/${id}/withdraw`, {
      method: 'POST',
      body: JSON.stringify({ version }),
    }),
}
