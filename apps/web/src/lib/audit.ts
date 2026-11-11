// The audit log (item 6.12): every agent and human decision, newest first, read-only. The API
// pages and filters it; nothing here can change a row.

import { call, label, type Page, type Person } from './risks'

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
  // Leave out the sign-in and sign-out rows, so the auditor sees decisions first.
  hide_sign_ins: boolean
}

// The filters a page opens with: any value, sign-ins hidden.
export const NO_AUDIT_FILTERS: AuditFilters = {
  agent: null,
  subject_type: null,
  status: null,
  actor_user_id: null,
  hide_sign_ins: true,
}

// The values present in the log, for the filter lists (`GET /api/v1/audit/facets`). People
// include anyone who took a decision, deactivated or not.
export interface AuditFacets {
  agents: string[]
  subject_types: string[]
  statuses: string[]
  people: Person[]
}

// Plain words for internal names the generic label would get wrong.
const AUDIT_LABELS: Record<string, string> = {
  auth: 'Sign-in and accounts',
  ok: 'OK',
}

// An agent, subject type or status in plain words: `change_request` is "Change request".
export function auditLabel(value: string): string {
  return AUDIT_LABELS[value] ?? label(value)
}

// A facet's values as list items: plain words shown, the internal name sent.
export function facetItems(values: string[]): { title: string; value: string }[] {
  return values.map((value) => ({ title: auditLabel(value), value }))
}

// The query string for one page of the log.
export function auditQuery(filters: AuditFilters, page: number, perPage: number): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (key === 'hide_sign_ins') {
      if (value) params.set(key, 'true')
      continue
    }
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
  facets: () => call<AuditFacets>('/api/v1/audit/facets'),
}
