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
  // The role an open risk is escalated to and the band that sent it there; empty once it is
  // accepted, rejected or closed.
  escalated_to: string | null
  escalated_band: string | null
  // When an accepted risk is next looked at.
  review_date: string | null
  version: number
  created_at: string
  updated_at: string
}

// A risk in the signed-in role's escalations list, from any project.
export interface EscalatedRisk extends Risk {
  project_name: string
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
  // Each band's open count and the role it escalates to (from the risk policy).
  bands: { band: string; count: number; owner_role: string }[]
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

// Shown on every risk the risk agent proposed, so a person knows to decide on it.
export const AGENT_PROPOSAL = 'Agent proposal'

export function isAgentProposal(risk: Pick<Risk, 'source'>): boolean {
  return risk.source === 'agent'
}

export interface RiskProposal {
  kind: Kind
  category: Category
  title: string
  probability: number | null
  impact: number | null
  response: string
  needs_judgement: boolean
  reason: string
}

export interface RiskSuggestion {
  mode: 'shadow' | 'enforce'
  dry_run: boolean
  proposals: RiskProposal[]
  dropped: { index: number; title: string | null; reason: string }[]
  saved: Risk[]
}

// What a person sees after the risk agent runs.
export function suggestionMessage(result: RiskSuggestion): string {
  const count = result.proposals.length
  const risks = count === 1 ? '1 risk' : `${count} risks`
  if (!count) return 'The risk agent found no new risks to suggest.'
  if (result.mode === 'shadow') {
    return `The risk agent suggested ${risks} (shadow mode, so nothing was saved): ${result.proposals
      .map((p) => p.title)
      .join('; ')}.`
  }
  return `The risk agent proposed ${risks}. Each is marked "${AGENT_PROPOSAL}": open or reject it.`
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

// Accepting a risk needs a reason at least this long (the API checks it too).
export const MIN_ACCEPT_REASON = 20

// How senior a role is when accepting a risk, as in the API (app/authz.py): a role may accept a
// band whose owner ranks at or below it. The web only uses this to hide buttons.
const ROLE_RANK: Record<string, number> = { project_manager: 1, sponsor: 2 }

// Whether a role may accept a risk in `band`, given each band's owner from the heat map.
export function mayAcceptBand(
  role: string | undefined,
  band: string | null,
  bands: Heatmap['bands'],
): boolean {
  const owner = bands.find((b) => b.band === band)?.owner_role
  if (!role || !owner || !(owner in ROLE_RANK)) return false
  return (ROLE_RANK[role] ?? 0) >= ROLE_RANK[owner]
}

// The Accept button: an open, scored risk, a role holding risk:accept, at or above the band's owner.
export function canAcceptRisk(
  risk: Pick<Risk, 'status' | 'score' | 'band'>,
  role: string | undefined,
  holdsAccept: boolean,
  bands: Heatmap['bands'],
): boolean {
  return (
    holdsAccept &&
    risk.status === 'open' &&
    risk.score !== null &&
    mayAcceptBand(role, risk.band, bands)
  )
}

// Whether a role may close an escalated risk or move it to a lower band: only a role holding
// risk:accept at or above the escalation owner (the API's escalation hold). A risk that isn't
// escalated holds nobody back.
export function mayClearEscalation(
  risk: Pick<Risk, 'escalated_to' | 'escalated_band'>,
  role: string | undefined,
  holdsAccept: boolean,
  bands: Heatmap['bands'],
): boolean {
  if (!risk.escalated_to) return true
  return holdsAccept && mayAcceptBand(role, risk.escalated_band, bands)
}

// What a person who can't clear an escalation sees in place of Close risk.
export function escalationLine(risk: Pick<Risk, 'escalated_to'>): string {
  const owner = label(risk.escalated_to ?? 'owner').toLowerCase()
  return `Escalated to the ${owner}: only the ${owner} can accept or close it`
}

// A rejected risk is final: the API refuses any change to it.
export function canEditRisk(risk: Pick<Risk, 'status'>): boolean {
  return risk.status !== 'rejected'
}

// A PATCH may close an open or accepted risk; other moves have their own routes.
export function canCloseRisk(risk: Pick<Risk, 'status'>): boolean {
  return risk.status === 'open' || risk.status === 'accepted'
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

export async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
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
  suggest: (projectId: number) =>
    call<RiskSuggestion>(`${base(projectId)}/suggest`, { method: 'POST' }),
  update: (projectId: number, riskId: number, body: Partial<RiskInput> & { version: number }) =>
    call<Risk>(`${base(projectId)}/${riskId}`, { method: 'PATCH', body: JSON.stringify(body) }),
  accept: (
    projectId: number,
    riskId: number,
    body: { version: number; reason: string; review_date: string },
  ) =>
    call<Risk>(`${base(projectId)}/${riskId}/accept`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  open: (projectId: number, riskId: number, version: number) =>
    call<Risk>(`${base(projectId)}/${riskId}/open`, {
      method: 'POST',
      body: JSON.stringify({ version }),
    }),
  reject: (projectId: number, riskId: number, body: { version: number; reason: string }) =>
    call<Risk>(`${base(projectId)}/${riskId}/reject`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  escalations: (offset = 0) =>
    call<Page<EscalatedRisk>>(`/api/v1/risks/escalations?limit=50&offset=${offset}`),
  close: (projectId: number, riskId: number, version: number) =>
    call<Risk>(`${base(projectId)}/${riskId}`, {
      method: 'PATCH',
      body: JSON.stringify({ version, status: 'closed' }),
    }),
}
