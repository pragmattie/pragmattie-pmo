// A project and the records planning starts from (item 4.6): its brief, stakeholders and
// assumptions. Passing the approval gate creates them; the project page shows them. The API
// enforces who may read and edit them.

import type { Page } from './risks'
import { call } from './risks'

export interface ProjectDetail {
  id: number
  client_id: number | null
  client_name: string | null
  name: string
  stage: string
  status: string
  sponsor: string
  sponsor_user_id: number | null
  pm: string
  pm_user_id: number | null
  request_id: number | null
  created_at: string
}

export interface DecisionRight {
  role: string
  title: string
  decides: string
}

export interface Brief {
  id: number
  project_id: number
  purpose: string
  objectives: string
  scope_in: string | null
  scope_out: string | null
  wanted_by: string | null
  // Money is a string with 2 decimal places.
  budget_low: string | null
  budget_high: string | null
  decision_rights: DecisionRight[]
  sponsor: string
  sponsor_user_id: number | null
  pm: string
  pm_user_id: number | null
  version: number
  updated_at: string
}

export interface Stakeholder {
  id: number
  project_id: number
  name: string
  user_id: number | null
  role_or_interest: string
  needs_to_hear: string | null
  how_often: string | null
  version: number
}

export type AssumptionStatus = 'open' | 'confirmed' | 'wrong'

export interface Assumption {
  id: number
  project_id: number
  text: string
  owner_user_id: number
  owner_name: string | null
  confirm_by: string
  status: AssumptionStatus
  version: number
}

export interface ProjectSummary {
  id: number
  name: string
  stage: string
  status: string
}

// "on_track" reads "On track".
export function label(value: string): string {
  const text = value.replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

// The rough budget as a range; either end may be missing.
export function budgetText(low: string | null, high: string | null): string {
  if (low && high) return `${low} to ${high}`
  if (low) return `from ${low}`
  if (high) return `up to ${high}`
  return 'Not set'
}

// The risk register, opened on this project.
export function risksLink(projectId: number) {
  return { name: 'risks', query: { project: String(projectId) } }
}

const base = (id: number) => `/api/v1/projects/${id}`

export const projectApi = {
  list: () => call<Page<ProjectSummary>>('/api/v1/projects?limit=100'),
  get: (id: number) => call<ProjectDetail>(base(id)),
  brief: (id: number) => call<Brief>(`${base(id)}/brief`),
  stakeholders: (id: number) => call<Page<Stakeholder>>(`${base(id)}/stakeholders?limit=100`),
  assumptions: (id: number) => call<Page<Assumption>>(`${base(id)}/assumptions?limit=100`),
}
