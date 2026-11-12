// The portfolio board (item 6.4): every request and project as a card in a column by stage, with
// what each is waiting on. The API computes it all fresh on each read, from the tables and the
// orchestrator's next steps; the page only shows it, polls it and links to each card's page.

import { label } from './projects'
import { call } from './risks'

export interface Waiting {
  step: string
  subject_type: string
  subject_id: number
  // `agent`: an agent has it (now, or after a window a person may hold). `person`: a person.
  holder: 'agent' | 'person'
  who: string
  text: string
  due_at: string | null
  simulated: boolean
}

export interface Card {
  key: string
  kind: 'request' | 'project'
  id: number
  column: string
  name: string
  client_id: number | null
  client_name: string | null
  project_manager: string | null
  health: string | null
  proposal_status: string | null
  top_risk_band: string | null
  open_risks: number
  plan_total: string | null
  top_budget: string | null
  stopped: boolean
  held_by: 'agent' | 'person' | null
  // The simulated client takes one of the card's steps, or the card's client is simulated.
  simulated: boolean
  client_simulated: boolean
  // The latest published status report (item 7.5): its overall rating, when it was published and
  // how many days ago.
  report_rating: 'green' | 'amber' | 'red' | null
  report_published_at: string | null
  report_age_days: number | null
  waiting: Waiting[]
}

export interface Column {
  id: string
  title: string
  cards: Card[]
}

export interface Summary {
  counts: Record<string, number>
  planning_value: string
  delivery_value: string
  open_medium_high_risks: number
  decisions_waiting: number
  stopped: number
}

export interface Board {
  columns: Column[]
  summary: Summary
  clients: { id: number; name: string }[]
  project_managers: string[]
}

export interface BoardFilters {
  client_id: number | null
  project_manager: string | null
  waiting: 'person' | 'agent' | null
  show_stopped: boolean
}

export const POLL_MS = 15_000
// How long a card that moved stays highlighted.
export const HIGHLIGHT_MS = 4_000
export const NOTHING_WAITING = 'Nothing waiting'

export const WAITING_CHOICES = [
  { title: 'Anyone', value: null },
  { title: 'Waiting on a person', value: 'person' },
  { title: 'Waiting on an agent', value: 'agent' },
]

export function boardQuery(filters: BoardFilters): string {
  const params = new URLSearchParams()
  if (filters.client_id !== null) params.set('client_id', String(filters.client_id))
  if (filters.project_manager) params.set('project_manager', filters.project_manager)
  if (filters.waiting) params.set('waiting', filters.waiting)
  if (filters.show_stopped) params.set('show_stopped', 'true')
  const query = params.toString()
  return query ? `?${query}` : ''
}

// The health chip's colour, from the project's status.
const HEALTH_COLORS: Record<string, string> = {
  on_track: 'success',
  at_risk: 'warning',
  off_track: 'error',
  on_hold: 'grey',
  complete: 'info',
  cancelled: 'grey',
}

export function healthColor(status: string): string {
  return HEALTH_COLORS[status] ?? 'grey'
}

export function healthText(status: string): string {
  return label(status)
}

// "Plan 110.00 of 150.50", or what is missing.
export function moneyText(card: Pick<Card, 'kind' | 'plan_total' | 'top_budget'>): string {
  const top = card.top_budget ? `top budget ${card.top_budget}` : 'no top budget'
  if (card.kind === 'request') return card.top_budget ? `Top budget ${card.top_budget}` : 'No top budget'
  if (card.plan_total === null) return `No plan yet; ${top}`
  return `Plan ${card.plan_total} of ${top}`
}

export function risksText(card: Pick<Card, 'open_risks' | 'top_risk_band'>): string {
  if (!card.open_risks) return 'No open risks'
  const noun = card.open_risks === 1 ? 'open risk' : 'open risks'
  return card.top_risk_band
    ? `${card.open_risks} ${noun}, highest ${card.top_risk_band}`
    : `${card.open_risks} ${noun}`
}

// "Last report: Amber, 7 days old", for a project with a published status report (item 7.5).
const RATING_WORDS = { green: 'Green', amber: 'Amber', red: 'Red' } as const

export function reportText(
  card: Pick<Card, 'report_rating' | 'report_age_days'>,
): string | null {
  if (card.report_rating === null || card.report_age_days === null) return null
  const days = card.report_age_days
  const age = days === 0 ? 'published today' : `${days} day${days === 1 ? '' : 's'} old`
  return `Last report: ${RATING_WORDS[card.report_rating]}, ${age}`
}

export const RATING_COLORS = { green: 'success', amber: 'warning', red: 'error' } as const

// Where a card leads: the request's page, or the project's.
export function cardLink(card: Pick<Card, 'kind' | 'id'>) {
  return { name: card.kind === 'request' ? 'request' : 'project', params: { id: card.id } }
}

// What a card's column and waiting state are, to tell when one has moved between reads.
export function cardState(card: Card): string {
  return [card.column, card.held_by ?? '', ...card.waiting.map((w) => `${w.holder}:${w.text}`)].join(
    '|',
  )
}

// The cards that were on the last board and have changed column or waiting state since.
export function movedKeys(before: Map<string, string>, board: Board): string[] {
  const moved = []
  for (const column of board.columns) {
    for (const card of column.cards) {
      const was = before.get(card.key)
      if (was !== undefined && was !== cardState(card)) moved.push(card.key)
    }
  }
  return moved
}

export function states(board: Board): Map<string, string> {
  return new Map(board.columns.flatMap((c) => c.cards.map((card) => [card.key, cardState(card)])))
}

export const boardApi = {
  get: (filters: BoardFilters) => call<Board>(`/api/v1/board${boardQuery(filters)}`),
}
