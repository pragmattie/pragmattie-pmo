// The audit log (item 6.12): every agent and human decision, newest first, read-only. The API
// pages and filters it; nothing here can change a row.

import { call, type Page, type Person } from './risks'

export interface AuditRow {
  id: number
  agent: string
  trigger: string
  subject_type: string
  subject_id: string
  version: number
  attempt: number
  status: string
  tier: string | null
  signals: unknown
  output: unknown
  action_taken: unknown
  human_override: unknown
  model: string | null
  prompt_version: string | null
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: string | null
  error: string | null
  supersedes_id: number | null
  actor_user_id: number | null
  // The person's display name; null for an agent.
  actor_name: string | null
  created_at: string
}

export interface AuditFilters {
  agent: string | null
  subject_type: string | null
  status: string | null
  actor_user_id: number | null
}

export const NO_AUDIT_FILTERS: AuditFilters = {
  agent: null,
  subject_type: null,
  status: null,
  actor_user_id: null,
}

// The statuses an audit row may have (the API's closed set).
export const AUDIT_STATUSES = ['ok', 'error', 'rejected', 'skipped']

// The query string for one page of the log.
export function auditQuery(filters: AuditFilters, page: number, perPage: number): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    const text = typeof value === 'string' ? value.trim() : value
    if (text !== null && text !== '') params.set(key, String(text))
  }
  params.set('limit', String(perPage))
  params.set('offset', String((page - 1) * perPage))
  return params.toString()
}

// Who took the decision: the person's name, or the agent.
export function whoText(row: Pick<AuditRow, 'actor_user_id' | 'actor_name' | 'agent'>): string {
  if (row.actor_user_id === null) return `${row.agent} (agent)`
  return row.actor_name ?? `Person ${row.actor_user_id}`
}

// A recorded value as text for the detail drawer: JSON pretty-printed, or a dash when empty.
export function recorded(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
}

export const auditApi = {
  list: (query: string) => call<Page<AuditRow>>(`/api/v1/audit?${query}`),
  people: () => call<Page<Person>>('/api/v1/users?limit=100'),
}
