// Project requests and the queue: shapes, API calls and the pure helpers the Intake pages use.
// Which buttons show is only a convenience: the API checks every permission and status again.

import { call, type HistoryEntry, type Page } from './risks'

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
