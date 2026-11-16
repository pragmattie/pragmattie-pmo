<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import PageTrail from '../components/PageTrail.vue'
import {
  type Benefit,
  benefitStanding,
  CATEGORIES,
  changedSections,
  type Closure,
  type ClosureSectionName,
  CLOSURE_SECTIONS,
  closureApi,
  type Lesson,
  type LessonCategory,
  type LessonInput,
  LESSON_STATUS_TEXT,
  lessonsOpen,
  MAX_SECTION,
  reportOpen,
  unreviewedSections,
} from '../lib/closure'
import { ApiError, type Person } from '../lib/risks'
import { figureText, sourceLink, sourceText } from '../lib/statusReports'
import { useAuthStore } from '../stores/auth'

// The closure report (item 7.7): figures read from the records, each linked to its source; each
// value case benefit, whose value now only a person enters; the text, where a section the closure
// agent drafted shows "Agent draft, not reviewed" until a person confirms or edits it; and the
// lessons, an agent's suggestion counting only once a person records it. A project manager
// (`report:publish`) writes the report and measures benefits; anyone with `project:write`
// records lessons and asks the agent for a draft. Once the sponsor passes the close-out gate the
// report is frozen and nothing here changes. The API enforces all of it.
const route = useRoute()
const auth = useAuthStore()

const projectId = computed(() => Number(route.params.id))
const canReport = computed(() => auth.can('report:publish'))
const canWrite = computed(() => auth.can('project:write'))

const closure = ref<Closure | null>(null)
const people = ref<Person[]>([])
const text = ref<Partial<Record<ClosureSectionName, string>>>({})
const error = ref('')
const errorItems = ref<string[]>([])
const notice = ref('')
const busy = ref(false)

const report = computed(() => closure.value?.report ?? null)
const editable = computed(
  () => canReport.value && !!closure.value && reportOpen(closure.value) && !!report.value,
)
const lessonsEditable = computed(
  () => canWrite.value && !!closure.value && lessonsOpen(closure.value),
)
const unreviewed = computed(() => (report.value ? unreviewedSections(report.value) : []))

// The benefit being measured, and the lesson being written.
const measuring = ref<Benefit | null>(null)
const measure = ref({ value_now: '', value_now_on: '', owner: null as number | null, by: '' })
const lesson = ref({
  category: 'scope' as LessonCategory,
  what_happened: '',
  do_again: '',
  do_differently: '',
  suggest_as_risk: false,
})

function fail(e: unknown) {
  error.value = (e as Error).message
  errorItems.value = e instanceof ApiError ? e.missing : []
}

function clear() {
  error.value = ''
  errorItems.value = []
  notice.value = ''
}

function show(found: Closure) {
  closure.value = found
  text.value = Object.fromEntries(
    CLOSURE_SECTIONS.map((s) => [s.name, found.report?.sections[s.name] ?? '']),
  ) as Record<ClosureSectionName, string>
}

async function load() {
  clear()
  try {
    show(await closureApi.get(projectId.value))
  } catch (e) {
    fail(e)
  }
  if (canReport.value) {
    people.value = await closureApi.people().catch(() => [])
  }
}

watch(projectId, load, { immediate: true })

async function write(step: () => Promise<unknown>, done: string) {
  clear()
  busy.value = true
  try {
    const result = await step()
    if (result && typeof result === 'object' && 'benefits' in result) {
      show(result as Closure)
    } else {
      show(await closureApi.get(projectId.value))
    }
    notice.value = done
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}

const create = () => write(() => closureApi.create(projectId.value), 'Closure report started.')
const refresh = () =>
  write(() => closureApi.refresh(projectId.value, report.value!), 'Figures read again.')
const draft = () =>
  write(() => closureApi.draft(projectId.value), 'The agent drafted the empty sections.')
const confirmSection = (name: ClosureSectionName) =>
  write(() => closureApi.confirm(projectId.value, report.value!, name), 'Section confirmed.')

function save() {
  const changed = changedSections(report.value!, text.value)
  write(() => closureApi.edit(projectId.value, report.value!, changed), 'Text saved.')
}

function startMeasuring(benefit: Benefit) {
  measuring.value = benefit
  measure.value = {
    value_now: benefit.value_now ?? '',
    value_now_on: benefit.value_now_on ?? '',
    owner: benefit.measure_owner_id,
    by: benefit.measure_by ?? '',
  }
}

function saveMeasure() {
  const benefit = measuring.value!
  const input = {
    value_now: measure.value.value_now.trim() || null,
    value_now_on: measure.value.value_now_on || null,
    measure_owner_id: measure.value.owner,
    measure_by: measure.value.by || null,
  }
  measuring.value = null
  write(() => closureApi.measure(projectId.value, benefit, input), 'Benefit saved.')
}

function addLesson() {
  const input = {
    category: lesson.value.category,
    what_happened: lesson.value.what_happened.trim(),
    do_again: lesson.value.do_again.trim() || null,
    do_differently: lesson.value.do_differently.trim() || null,
    suggest_as_risk: lesson.value.suggest_as_risk,
  }
  write(async () => {
    await closureApi.addLesson(projectId.value, input)
    lesson.value = { ...lesson.value, what_happened: '', do_again: '', do_differently: '' }
  }, 'Lesson recorded.')
}

// Only lessons flagged as a risk for future projects reach the risk agent, so recording an
// agent's suggestion asks whether to offer it; the choice is off unless a person ticks it.
const offerAsRisk = ref<Record<number, boolean>>({})

const recordLesson = (item: Lesson) =>
  write(
    () => closureApi.recordLesson(projectId.value, item, !!offerAsRisk.value[item.id]),
    'Lesson recorded.',
  )

// The lesson being edited: any lesson not discarded, the flag included, while the project is open.
const editing = ref<Lesson | null>(null)
const edited = ref<LessonInput>({
  category: 'scope',
  what_happened: '',
  do_again: null,
  do_differently: null,
  suggest_as_risk: false,
})

function startEditing(item: Lesson) {
  editing.value = item
  edited.value = {
    category: item.category,
    what_happened: item.what_happened,
    do_again: item.do_again,
    do_differently: item.do_differently,
    suggest_as_risk: item.suggest_as_risk,
  }
}

function saveLesson() {
  const item = editing.value!
  const input = {
    category: edited.value.category,
    what_happened: edited.value.what_happened.trim(),
    do_again: edited.value.do_again?.trim() || null,
    do_differently: edited.value.do_differently?.trim() || null,
    suggest_as_risk: edited.value.suggest_as_risk,
  }
  editing.value = null
  write(() => closureApi.editLesson(projectId.value, item, input), 'Lesson saved.')
}
const discardLesson = (item: Lesson) =>
  write(() => closureApi.discardLesson(projectId.value, item), 'Lesson discarded.')

const flagsBySection = computed(() => {
  const found: Record<string, string[]> = {}
  for (const flag of report.value?.flags ?? []) {
    ;(found[flag.section] ??= []).push(flag.text)
  }
  return found
})
</script>

<template>
  <div>
    <PageTrail
      :crumbs="[
        { title: 'Projects', to: { name: 'projects' } },
        {
          title: closure?.project_name ?? 'Project',
          to: { name: 'project', params: { id: projectId } },
        },
        { title: 'Closure report' },
      ]"
    />
    <h1 class="text-h4 text-primary mb-2" data-test="page-title">Closure report</h1>
    <p class="text-body-2 text-medium-emphasis mb-4">
      Every figure comes from a record. A person enters each benefit's value now, reviews every
      section and records the lessons; the sponsor closes the project at the close-out gate.
    </p>

    <v-alert v-if="error" type="warning" variant="tonal" class="mb-4" data-test="error">
      {{ error }}
      <ul v-if="errorItems.length" class="pl-4 mt-1">
        <li v-for="item in errorItems" :key="item">{{ item }}</li>
      </ul>
    </v-alert>
    <v-alert v-if="notice" type="success" variant="tonal" class="mb-4" data-test="notice">
      {{ notice }}
    </v-alert>

    <template v-if="closure">
      <v-alert
        v-if="report?.frozen_at"
        type="info"
        variant="tonal"
        class="mb-4"
        data-test="frozen"
      >
        The project is closed. This is the record as it stood on
        {{ new Date(report.frozen_at).toLocaleString() }}, and it no longer changes.
      </v-alert>

      <section v-if="!report" class="mb-6" data-test="no-report">
        <p class="text-body-2 mb-2">
          No closure report yet.
          <span v-if="!reportOpen(closure)">One is made once the project is in closure.</span>
        </p>
        <v-btn
          v-if="canReport && reportOpen(closure)"
          color="primary"
          :disabled="busy"
          data-test="create-report"
          @click="create"
        >
          Start the closure report
        </v-btn>
      </section>

      <section v-if="report" class="mb-6" data-test="figures-section">
        <div class="d-flex flex-wrap align-center ga-2 mb-2">
          <h2 class="text-h6">Figures</h2>
          <v-spacer />
          <v-btn
            v-if="editable"
            size="small"
            variant="outlined"
            :disabled="busy"
            data-test="refresh"
            @click="refresh"
          >
            Read figures again
          </v-btn>
        </div>
        <p class="text-caption text-medium-emphasis mb-2">
          Read {{ new Date(report.figures_at).toLocaleString() }}.
        </p>
        <v-table density="compact" data-test="figures">
          <tbody>
            <tr v-for="figure in report.figures" :key="figure.key" data-test="figure">
              <th scope="row">{{ figure.label }}</th>
              <td data-test="figure-value">{{ figureText(figure) }}</td>
              <td>
                <div class="text-caption">{{ figure.note }}</div>
                <router-link
                  v-for="source in figure.sources"
                  :key="`${source.table}-${source.id}-${source.field}`"
                  :to="sourceLink(projectId, source)"
                  class="text-caption mr-2"
                  data-test="figure-source"
                >
                  {{ sourceText(source) }}
                </router-link>
              </td>
            </tr>
          </tbody>
        </v-table>
        <div v-if="closure.accepted_risks.length" class="mt-2" data-test="accepted-risks">
          <span class="text-body-2">Accepted risks still standing:</span>
          <router-link
            v-for="risk in closure.accepted_risks"
            :key="risk.id"
            :to="{ name: 'risks', query: { project: String(projectId), risk: String(risk.id) } }"
            class="text-body-2 ml-2"
          >
            {{ risk.title }}
          </router-link>
        </div>
      </section>

      <section class="mb-6" data-test="benefits">
        <h2 class="text-h6 mb-2">Benefits</h2>
        <p v-if="closure.benefits.length === 0" class="text-body-2 text-medium-emphasis">
          The project has no value case benefits.
        </p>
        <v-table v-else density="compact">
          <thead>
            <tr>
              <th>Measure</th>
              <th>Before</th>
              <th>Target</th>
              <th>Now</th>
              <th v-if="editable"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="benefit in closure.benefits" :key="benefit.benefit_id" data-test="benefit">
              <td>{{ benefit.measure ?? benefit.description }}</td>
              <td>{{ benefit.value_before ?? 'Not recorded' }}</td>
              <td>{{ benefit.target ?? 'Not recorded' }} by {{ benefit.target_date ?? '?' }}</td>
              <td data-test="benefit-standing">{{ benefitStanding(benefit) }}</td>
              <td v-if="editable">
                <v-btn
                  size="small"
                  variant="text"
                  :disabled="busy"
                  data-test="measure"
                  @click="startMeasuring(benefit)"
                >
                  Enter
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>
        <v-card v-if="measuring" variant="outlined" class="pa-3 mt-2" data-test="measure-form">
          <p class="text-body-2 mb-2">
            {{ measuring.measure ?? measuring.description }}: the value now with the day it was
            measured, or who measures it later and by when.
          </p>
          <div class="d-flex flex-wrap ga-2">
            <v-text-field
              v-model="measure.value_now"
              label="Value now"
              density="compact"
              maxlength="200"
              data-test="value-now"
            />
            <v-text-field
              v-model="measure.value_now_on"
              label="Measured on"
              type="date"
              density="compact"
              data-test="value-now-on"
            />
            <v-select
              v-model="measure.owner"
              :items="people"
              item-title="display_name"
              item-value="id"
              label="Owner to measure it"
              clearable
              density="compact"
              data-test="measure-owner"
            />
            <v-text-field
              v-model="measure.by"
              label="Measure by"
              type="date"
              density="compact"
              data-test="measure-by"
            />
          </div>
          <v-btn color="primary" size="small" :disabled="busy" data-test="save-measure" @click="saveMeasure">
            Save
          </v-btn>
          <v-btn size="small" variant="text" @click="measuring = null">Cancel</v-btn>
        </v-card>
      </section>

      <section v-if="report" class="mb-6" data-test="sections">
        <div class="d-flex flex-wrap align-center ga-2 mb-2">
          <h2 class="text-h6">Text</h2>
          <v-spacer />
          <v-btn
            v-if="canWrite && reportOpen(closure)"
            size="small"
            variant="outlined"
            :disabled="busy"
            data-test="draft"
            @click="draft"
          >
            Draft with the agent
          </v-btn>
        </div>
        <v-alert
          v-if="unreviewed.length"
          type="info"
          variant="tonal"
          class="mb-2"
          data-test="unreviewed"
        >
          The close-out gate waits until a person confirms or edits every agent draft.
        </v-alert>
        <div v-for="section in CLOSURE_SECTIONS" :key="section.name" class="mb-3">
          <div class="d-flex align-center ga-2">
            <h3 class="text-subtitle-1">{{ section.title }}</h3>
            <v-chip
              v-if="report.section_states?.[section.name]?.unreviewed"
              size="small"
              color="warning"
              :data-test="`agent-draft-${section.name}`"
            >
              Agent draft, not reviewed
            </v-chip>
            <v-btn
              v-if="editable && report.section_states?.[section.name]?.unreviewed"
              size="small"
              variant="text"
              :disabled="busy"
              :data-test="`confirm-${section.name}`"
              @click="confirmSection(section.name)"
            >
              Confirm
            </v-btn>
          </div>
          <v-textarea
            v-if="editable"
            v-model="text[section.name]"
            :counter="MAX_SECTION"
            :maxlength="MAX_SECTION"
            rows="3"
            auto-grow
            density="compact"
            :data-test="`section-${section.name}`"
          />
          <p v-else class="text-body-2" :data-test="`section-${section.name}`">
            {{ report.sections[section.name] || 'Not written.' }}
          </p>
          <p
            v-for="flagged in flagsBySection[section.name] ?? []"
            :key="flagged"
            class="text-caption text-warning"
            data-test="flag"
          >
            {{ flagged }} isn't one of the report's figures
          </p>
        </div>
        <v-btn v-if="editable" color="primary" :disabled="busy" data-test="save" @click="save">
          Save text
        </v-btn>
      </section>

      <section class="mb-6" data-test="lessons">
        <h2 class="text-h6 mb-2">Lessons</h2>
        <p v-if="closure.lessons.length === 0" class="text-body-2 text-medium-emphasis">
          No lessons yet.
        </p>
        <v-card
          v-for="item in closure.lessons"
          :key="item.id"
          variant="outlined"
          class="pa-3 mb-2"
          data-test="lesson"
        >
          <div class="d-flex flex-wrap align-center ga-2">
            <v-chip size="small">{{ CATEGORIES.find((c) => c.value === item.category)?.title }}</v-chip>
            <v-chip
              size="small"
              :color="item.status === 'suggested' ? 'warning' : undefined"
              data-test="lesson-status"
            >
              {{ LESSON_STATUS_TEXT[item.status] }}
            </v-chip>
            <v-chip v-if="item.suggest_as_risk" size="small" data-test="lesson-as-risk">
              Offered as a risk for future projects
            </v-chip>
          </div>
          <p class="text-body-2 mt-2">{{ item.what_happened }}</p>
          <p v-if="item.do_again" class="text-body-2">Do again: {{ item.do_again }}</p>
          <p v-if="item.do_differently" class="text-body-2">
            Do differently: {{ item.do_differently }}
          </p>
          <v-checkbox
            v-if="lessonsEditable && item.status === 'suggested' && editing?.id !== item.id"
            v-model="offerAsRisk[item.id]"
            label="Suggest as a risk for future projects"
            density="compact"
            hide-details
            data-test="record-as-risk"
          />
          <div v-if="lessonsEditable && item.status !== 'discarded' && editing?.id !== item.id">
            <template v-if="item.status === 'suggested'">
              <v-btn
                size="small"
                variant="text"
                :disabled="busy"
                data-test="record-lesson"
                @click="recordLesson(item)"
              >
                Record
              </v-btn>
              <v-btn
                size="small"
                variant="text"
                :disabled="busy"
                data-test="discard-lesson"
                @click="discardLesson(item)"
              >
                Discard
              </v-btn>
            </template>
            <v-btn
              size="small"
              variant="text"
              :disabled="busy"
              data-test="edit-lesson"
              @click="startEditing(item)"
            >
              Edit
            </v-btn>
          </div>
          <div v-if="lessonsEditable && editing?.id === item.id" class="mt-2" data-test="lesson-edit">
            <v-select
              v-model="edited.category"
              :items="CATEGORIES"
              item-title="title"
              item-value="value"
              label="Category"
              density="compact"
            />
            <v-textarea
              v-model="edited.what_happened"
              label="What happened"
              rows="2"
              density="compact"
              data-test="edit-what"
            />
            <v-textarea
              v-model="edited.do_again"
              label="What we would do again"
              rows="2"
              density="compact"
              data-test="edit-again"
            />
            <v-textarea
              v-model="edited.do_differently"
              label="What we would do differently"
              rows="2"
              density="compact"
              data-test="edit-differently"
            />
            <v-checkbox
              v-model="edited.suggest_as_risk"
              label="Suggest as a risk for future projects"
              density="compact"
              data-test="edit-as-risk"
            />
            <v-btn color="primary" size="small" :disabled="busy" data-test="save-lesson" @click="saveLesson">
              Save
            </v-btn>
            <v-btn size="small" variant="text" @click="editing = null">Cancel</v-btn>
          </div>
        </v-card>
        <v-card v-if="lessonsEditable" variant="outlined" class="pa-3" data-test="lesson-form">
          <v-select
            v-model="lesson.category"
            :items="CATEGORIES"
            item-title="title"
            item-value="value"
            label="Category"
            density="compact"
          />
          <v-textarea
            v-model="lesson.what_happened"
            label="What happened"
            rows="2"
            density="compact"
            data-test="lesson-what"
          />
          <v-textarea v-model="lesson.do_again" label="What we would do again" rows="2" density="compact" />
          <v-textarea
            v-model="lesson.do_differently"
            label="What we would do differently"
            rows="2"
            density="compact"
            data-test="lesson-differently"
          />
          <v-checkbox
            v-model="lesson.suggest_as_risk"
            label="Offer it to future projects as a suggested risk"
            density="compact"
          />
          <v-btn color="primary" size="small" :disabled="busy" data-test="add-lesson" @click="addLesson">
            Record lesson
          </v-btn>
        </v-card>
      </section>
    </template>
  </div>
</template>
