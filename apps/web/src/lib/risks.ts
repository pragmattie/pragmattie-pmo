// The risk register's shapes, API calls and the pure helpers the Risks page uses. Score and band
// always come from the API (the risk policy); the web never works them out.

import { apiFetch } from './api'

export type Kind = 'threat' | 'opportunity'
export type Category = 'ai' | 'project'
export type Objective = 'schedule' | 'cost' | 'quality'
export type Status = 'proposed' | 'open' | 'accepted' | 'closed' | 'rejected'

export interface Risk {
  id: number
  project_id: number
  kind: Kind
  title: string
  cause: string
  effect: string
  category: Category
  objectives: Objective[]
  probability: number | null
  impact: number | null
  score: number | null
  band: string | null
  response: string
  response_plan: string
  owner_user_id: number | null
  status: Status
  needs_judgement: boolean
  source: string
  version: number
  created_at: string
  updated_at: string
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface HeatCell {
  probability: number
  impact: number
  band: string
  count: number
}

export interface Heatmap {
  kind: Kind | null
  cells: HeatCell[]
  bands: { band: string; count: number }[]
  unscored: number
}

export interface HistoryEntry {
  id: number
  version: number
  action: string
  actor_user_id: number | null
  actor_name: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  created_at: string
}

export interface Project {
  id: number
  name: string
}

export interface Person {
  id: number
  display_name: string
}

export interface RiskTemplate {
  id: string
  kind: Kind
  category: Category
  title: string
  response: string
}

// What a person fills in to add or edit a risk.
export interface RiskInput {
  kind: Kind
  title: string
  cause: string
  effect: string
  category: Category
  objectives: Objective[]
  probability: number | null
  impact: number | null
  response: string
  response_plan: string
  owner_user_id: number | null
  needs_judgement: boolean
}

export interface Filters {
  status: Status | null
  band: string | null
  category: Category | null
  kind: Kind | null
  // One heat map cell, set by clicking it.
  probability: number | null
  impact: number | null
}

export const NO_FILTERS: Filters = {
  status: null,
  band: null,
  category: null,
  kind: null,
  probability: null,
  impact: null,
}

export const LEVELS = [1, 2, 3, 4, 5] as const
export const STATUSES: Status[] = ['proposed', 'open', 'accepted', 'closed', 'rejected']
export const KINDS: Kind[] = ['threat', 'opportunity']
export const CATEGORIES: Category[] = ['ai', 'project']
export const OBJECTIVES: Objective[] = ['schedule', 'cost', 'quality']
export const RESPONSES_BY_KIND: Record<Kind, string[]> = {
  threat: ['avoid', 'reduce', 'share', 'accept'],
  opportunity: ['pursue', 'increase', 'share', 'accept'],
}

export const NEEDS_JUDGEMENT = "Needs a person's judgement"
export const NOT_SCORED = 'Not scored yet'
export const CONFLICT_MESSAGE =
  'Someone changed this risk after you opened it. The latest version is shown below; ' +
  'check it and make your change again.'

const LABELS: Record<string, string> = {
  ai: 'AI',
  project: 'Project',
  threat: 'Threat',
  opportunity: 'Opportunity',
}

export function label(value: string): string {
  return LABELS[value] ?? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, ' ')
}

// The theme colour for a band (`band-low`, `band-medium`, `band-high` in the Vuetify theme).
export function bandColor(band: string | null | undefined): string {
  return band ? `band-${band.toLowerCase().replace(/[^a-z0-9]+/g, '-')}` : 'grey'
}

// What the score column shows: a number, or why there isn't one.
export function scoreText(risk: Pick<Risk, 'score' | 'needs_judgement'>): string {
  if (risk.score !== null) return String(risk.score)
  return risk.needs_judgement ? NEEDS_JUDGEMENT : NOT_SCORED
}

// The heat map as rows, highest impact first, each row probability 1 to 5. A missing cell is
// shown empty with no band.
export function heatGrid(cells: HeatCell[]): HeatCell[][] {
  const byKey = new Map(cells.map((cell) => [`${cell.probability}:${cell.impact}`, cell]))
  return [...LEVELS].reverse().map((impact) =>
    LEVELS.map(
      (probability) =>
        byKey.get(`${probability}:${impact}`) ?? { probability, impact, band: '', count: 0 },
    ),
  )
}

export function openTotal(heatmap: Heatmap | null): number {
  if (!heatmap) return 0
  return heatmap.bands.reduce((sum, band) => sum + band.count, 0) + heatmap.unscored
}

// The query string for one page of the register.
export function listQuery(filters: Filters, page: number, perPage: number): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== null && value !== '') params.set(key, String(value))
  }
  params.set('limit', String(perPage))
  params.set('offset', String((page - 1) * perPage))
  return params.toString()
}

// Clicking a heat map cell shows that cell's open risks; clicking it again clears it.
export function toggleCell(
  filters: Filters,
  cell: Pick<HeatCell, 'probability' | 'impact'>,
  kind: Kind | null,
): Filters {
  const same = filters.probability === cell.probability && filters.impact === cell.impact
  if (same) return { ...filters, probability: null, impact: null }
  return { ...filters, probability: cell.probability, impact: cell.impact, status: 'open', kind }
}

// The fields an audit row changed, for the history list.
export function changedFields(entry: HistoryEntry): string[] {
  if (!entry.before || !entry.after) return []
  const before = entry.before
  const after = entry.after
  return Object.keys(after).filter(
    (key) => key !== 'version' && JSON.stringify(before[key]) !== JSON.stringify(after[key]),
  )
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json()
    if (typeof body.detail === 'string') return body.detail
    if (Array.isArray(body.detail)) {
      return body.detail.map((item: { msg?: string }) => item.msg ?? '').join('; ')
    }
  } catch {
    // Not JSON: fall through to the status text.
  }
  return `The request failed (${response.status}).`
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init)
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response))
  return (await response.json()) as T
}

const base = (projectId: number) => `/api/v1/projects/${projectId}/risks`

export const riskApi = {
  projects: () => call<Page<Project>>('/api/v1/projects?limit=100'),
  people: () => call<Page<Person>>('/api/v1/users?limit=100'),
  library: () => call<Page<RiskTemplate>>('/api/v1/risk-library?limit=100'),
  list: (projectId: number, query: string) => call<Page<Risk>>(`${base(projectId)}?${query}`),
  heatmap: (projectId: number, kind: Kind | null) =>
    call<Heatmap>(`${base(projectId)}/heatmap${kind ? `?kind=${kind}` : ''}`),
  get: (projectId: number, riskId: number) => call<Risk>(`${base(projectId)}/${riskId}`),
  history: (projectId: number, riskId: number, offset = 0) =>
    call<Page<HistoryEntry>>(`${base(projectId)}/${riskId}/history?limit=50&offset=${offset}`),
  create: (projectId: number, body: RiskInput) =>
    call<Risk>(base(projectId), { method: 'POST', body: JSON.stringify(body) }),
  fromLibrary: (projectId: number, templateIds: string[]) =>
    call<Risk[]>(`${base(projectId)}/from-library`, {
      method: 'POST',
      body: JSON.stringify({ template_ids: templateIds }),
    }),
  update: (projectId: number, riskId: number, body: Partial<RiskInput> & { version: number }) =>
    call<Risk>(`${base(projectId)}/${riskId}`, { method: 'PATCH', body: JSON.stringify(body) }),
  close: (projectId: number, riskId: number, version: number) =>
    call<Risk>(`${base(projectId)}/${riskId}`, {
      method: 'PATCH',
      body: JSON.stringify({ version, status: 'closed' }),
    }),
}
