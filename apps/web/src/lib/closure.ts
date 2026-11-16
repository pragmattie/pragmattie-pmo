// Closing a project (item 7.7): its closure report, each benefit as measured at close, and its
// lessons. The API reads every figure from a record; the web only shows them, links each figure
// to its source and hides what a role can't do. The API checks every rule again.

import { allPages } from './plans'
import { call, type Person } from './risks'
import type { Figure, Flag, SectionState } from './statusReports'

export type ClosureSectionName = 'delivered' | 'value_case' | 'remaining'
export type LessonCategory = 'scope' | 'schedule' | 'cost' | 'risk' | 'people' | 'ai'
export type LessonStatus = 'suggested' | 'recorded' | 'discarded'

export interface ClosureReport {
  id: number
  project_id: number
  figures: Figure[]
  figures_at: string
  sections: Partial<Record<ClosureSectionName, string | null>>
  section_states: Partial<Record<ClosureSectionName, SectionState>>
  flags: Flag[]
  // Set when the sponsor passed the close-out gate: the report is frozen as it was then.
  frozen_at: string | null
  version: number
  created_at: string
}

export interface Benefit {
  benefit_id: number
  position: number
  description: string
  measure: string | null
  value_before: string | null
  target: string | null
  target_date: string | null
  value_now: string | null
  value_now_on: string | null
  measure_owner_id: number | null
  measure_owner_name: string | null
  measure_by: string | null
  // 0 until the benefit is first measured or given an owner.
  version: number
}

export interface Lesson {
  id: number
  project_id: number
  category: LessonCategory
  what_happened: string
  do_again: string | null
  do_differently: string | null
  suggest_as_risk: boolean
  source: 'person' | 'agent'
  status: LessonStatus
  reviewed_by: number | null
  reviewed_at: string | null
  version: number
  created_at: string
}

export interface Closure {
  project_id: number
  project_name: string
  project_stage: string
  project_status: string
  report: ClosureReport | null
  benefits: Benefit[]
  lessons: Lesson[]
  accepted_risks: { id: number; title: string; band: string | null }[]
}

export interface BenefitInput {
  value_now: string | null
  value_now_on: string | null
  measure_owner_id: number | null
  measure_by: string | null
}

export interface LessonInput {
  category: LessonCategory
  what_happened: string
  do_again: string | null
  do_differently: string | null
  suggest_as_risk: boolean
}

export const MAX_SECTION = 4000

export const CLOSURE_SECTIONS: { name: ClosureSectionName; title: string }[] = [
  { name: 'delivered', title: 'What was delivered' },
  { name: 'value_case', title: 'Against the value case' },
  { name: 'remaining', title: 'What remains' },
]

export const CATEGORIES: { value: LessonCategory; title: string }[] = [
  { value: 'scope', title: 'Scope' },
  { value: 'schedule', title: 'Schedule' },
  { value: 'cost', title: 'Cost' },
  { value: 'risk', title: 'Risk' },
  { value: 'people', title: 'People' },
  { value: 'ai', title: 'AI' },
]

export const LESSON_STATUS_TEXT: Record<LessonStatus, string> = {
  suggested: 'Agent suggestion, not recorded',
  recorded: 'Recorded',
  discarded: 'Discarded',
}

// Writes are open only while the project is in closure (lessons also in delivery), never once
// it is closed; the API refuses the rest and records each refusal.
export function reportOpen(closure: Closure): boolean {
  return closure.project_status !== 'cancelled' && closure.project_stage === 'closure'
}

export function lessonsOpen(closure: Closure): boolean {
  return (
    closure.project_status !== 'cancelled' &&
    (closure.project_stage === 'delivery' || closure.project_stage === 'closure')
  )
}

export function unreviewedSections(report: ClosureReport): ClosureSectionName[] {
  return CLOSURE_SECTIONS.filter((s) => report.section_states?.[s.name]?.unreviewed).map(
    (s) => s.name,
  )
}

// Only the sections whose text a person changed, so saving never marks an agent's draft as
// reviewed without its text being touched (Confirm does that).
export function changedSections(
  report: ClosureReport,
  text: Partial<Record<ClosureSectionName, string>>,
): Partial<Record<ClosureSectionName, string>> {
  return Object.fromEntries(
    CLOSURE_SECTIONS.filter((s) => (text[s.name] ?? '') !== (report.sections[s.name] ?? '')).map(
      (s) => [s.name, text[s.name] ?? ''],
    ),
  ) as Partial<Record<ClosureSectionName, string>>
}

// How a benefit stands at close, in one line.
export function benefitStanding(benefit: Benefit): string {
  if (benefit.value_now !== null) return `${benefit.value_now} on ${benefit.value_now_on}`
  if (benefit.measure_owner_id !== null) {
    return `To be measured by ${benefit.measure_owner_name ?? 'its owner'} by ${benefit.measure_by}`
  }
  return 'Not measured, and no owner or date yet'
}

const base = (projectId: number) => `/api/v1/projects/${projectId}`
const post = (body: unknown) => ({ method: 'POST', body: JSON.stringify(body) })

export const closureApi = {
  get: (projectId: number) => call<Closure>(`${base(projectId)}/closure`),
  people: () => allPages<Person>('/api/v1/users'),
  create: (projectId: number) =>
    call<Closure>(`${base(projectId)}/closure-report`, { method: 'POST', body: '{}' }),
  edit: (
    projectId: number,
    report: ClosureReport,
    sections: Partial<Record<ClosureSectionName, string>>,
  ) =>
    call<Closure>(`${base(projectId)}/closure-report`, {
      method: 'PATCH',
      body: JSON.stringify({ version: report.version, sections }),
    }),
  refresh: (projectId: number, report: ClosureReport) =>
    call<Closure>(`${base(projectId)}/closure-report/refresh`, post({ version: report.version })),
  confirm: (projectId: number, report: ClosureReport, section: ClosureSectionName) =>
    call<Closure>(
      `${base(projectId)}/closure-report/sections/${section}/confirm`,
      post({ version: report.version }),
    ),
  draft: (projectId: number) =>
    call<unknown>(`${base(projectId)}/closure-report/draft`, { method: 'POST', body: '{}' }),
  measure: (projectId: number, benefit: Benefit, input: BenefitInput) =>
    call<Closure>(`${base(projectId)}/closure-report/benefits/${benefit.benefit_id}`, {
      method: 'PUT',
      body: JSON.stringify({ version: benefit.version, ...input }),
    }),
  addLesson: (projectId: number, input: LessonInput) =>
    call<Lesson>(`${base(projectId)}/lessons`, post(input)),
  recordLesson: (projectId: number, lesson: Lesson) =>
    call<Lesson>(`${base(projectId)}/lessons/${lesson.id}/record`, post({ version: lesson.version })),
  discardLesson: (projectId: number, lesson: Lesson) =>
    call<Lesson>(
      `${base(projectId)}/lessons/${lesson.id}/discard`,
      post({ version: lesson.version }),
    ),
}
