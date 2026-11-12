<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import PageTrail from '../components/PageTrail.vue'
import { type ProjectDetail, projectApi } from '../lib/projects'
import { ApiError } from '../lib/risks'
import {
  changedSections,
  figureText,
  liveReport,
  MAX_REASON,
  MAX_SECTION,
  type Period,
  type PeriodsPage,
  RATED,
  reportLink,
  type ReportSummary,
  RATING_COLOR,
  RATING_TEXT,
  SECTIONS,
  type SectionName,
  sourceLink,
  sourceText,
  STATUS_TEXT,
  type StatusReport,
  statusReportApi,
  unreviewedSections,
} from '../lib/statusReports'
import { useAuthStore } from '../stores/auth'

// Status reports (item 7.3): a project's periods, and one report: its ratings with the rule that
// set each, its figures each linked to its source record, and its text. A project manager
// (`report:publish`) starts a draft, writes the text, refreshes the figures, publishes, withdraws
// and corrects; everyone else reads published reports. The API enforces all of it and hides
// drafts from other roles. A section the status report agent drafted (item 7.4) shows "Agent
// draft, not reviewed" with Confirm until a person confirms or edits it; publishing waits for
// every one.
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const projectId = computed(() => Number(route.params.id))
const reportId = computed(() => (route.query.report ? Number(route.query.report) : null))
const canWrite = computed(() => auth.can('report:publish'))

const project = ref<ProjectDetail | null>(null)
const periods = ref<PeriodsPage | null>(null)
const report = ref<StatusReport | null>(null)
const draftText = ref<Partial<Record<SectionName, string>>>({})
const reason = ref('')
const error = ref('')
const errorItems = ref<string[]>([])
const notice = ref('')
const busy = ref(false)

const isDraft = computed(() => report.value?.status === 'draft')
const editable = computed(() => canWrite.value && isDraft.value)

function fail(e: unknown) {
  error.value = (e as Error).message
  errorItems.value = e instanceof ApiError ? e.missing : []
}

function clearMessages() {
  error.value = ''
  errorItems.value = []
  notice.value = ''
}

function showReport(found: StatusReport | null) {
  report.value = found
  reason.value = ''
  draftText.value = Object.fromEntries(
    SECTIONS.map((s) => [s.name, found?.sections[s.name] ?? '']),
  ) as Record<SectionName, string>
}

async function loadPeriods() {
  periods.value = await statusReportApi.periods(projectId.value)
}

async function loadReport() {
  if (reportId.value === null) return showReport(null)
  try {
    showReport(await statusReportApi.get(projectId.value, reportId.value))
  } catch (e) {
    showReport(null)
    fail(e)
  }
}

async function load() {
  clearMessages()
  try {
    project.value = await projectApi.get(projectId.value)
    await loadPeriods()
  } catch (e) {
    fail(e)
  }
  await loadReport()
}

watch(projectId, load, { immediate: true })
watch(reportId, () => {
  clearMessages()
  loadReport()
})

function open(id: number) {
  router.push({ query: { report: String(id) } })
}

// A sponsor lands on a published report's one-page view (item 7.5); everyone else on this page.
function openReport(item: ReportSummary) {
  if (auth.user?.role === 'sponsor' && item.status === 'published') {
    router.push(reportLink(projectId.value, item.id, auth.user.role, item.status))
  } else {
    open(item.id)
  }
}

// Run one write; on success show the report it returns and read the periods again.
async function write(step: () => Promise<StatusReport>, done: string) {
  clearMessages()
  busy.value = true
  try {
    const result = await step()
    await loadPeriods()
    if (result.id !== reportId.value) {
      showReport(result)
      await router.push({ query: { report: String(result.id) } })
    } else {
      showReport(result)
    }
    notice.value = done
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}

function startDraft(period: Period) {
  write(() => statusReportApi.create(projectId.value, period.end), 'Draft started.')
}

function save() {
  const current = report.value!
  const changed = changedSections(current, draftText.value)
  write(() => statusReportApi.edit(projectId.value, current, changed), 'Text saved.')
}

function confirmSection(section: SectionName) {
  write(
    () => statusReportApi.confirm(projectId.value, report.value!, section),
    'Section confirmed.',
  )
}

const unreviewed = computed(() => (report.value ? unreviewedSections(report.value) : []))

function refresh() {
  write(() => statusReportApi.refresh(projectId.value, report.value!), 'Figures read again.')
}

function publish() {
  write(() => statusReportApi.publish(projectId.value, report.value!), 'Report published.')
}

function withdraw() {
  write(
    () => statusReportApi.withdraw(projectId.value, report.value!, reason.value),
    'Report withdrawn.',
  )
}

function correct() {
  const current = report.value!
  write(
    () =>
      statusReportApi.create(projectId.value, current.period_end, {
        id: current.id,
        reason: reason.value,
      }),
    'Correction started. Publishing it withdraws the report it replaces.',
  )
}

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
        { title: project?.name ?? 'Project', to: { name: 'project', params: { id: projectId } } },
        { title: 'Status reports' },
      ]"
    />
    <h1 class="text-h4 text-primary mb-2" data-test="page-title">Status reports</h1>
    <p class="text-body-2 text-medium-emphasis mb-4">
      Every figure comes from a record and every rating from the reporting policy. A project
      manager writes the text and publishes; the sponsor sees published reports.
    </p>

    <v-alert v-if="error" type="warning" variant="tonal" class="mb-4" data-test="error">
      {{ error }}
      <ul v-if="errorItems.length" class="pl-4 mt-1" data-test="error-items">
        <li v-for="item in errorItems" :key="item">{{ item }}</li>
      </ul>
    </v-alert>
    <v-alert v-if="notice" type="success" variant="tonal" class="mb-4" data-test="notice">
      {{ notice }}
    </v-alert>

    <section class="mb-6" data-test="periods">
      <h2 class="text-subtitle-1 mb-2">Periods</h2>
      <v-alert
        v-if="periods && periods.report_period === null"
        type="info"
        variant="tonal"
        data-test="no-period"
      >
        The plan has no reporting period, so there are no reports yet.
      </v-alert>
      <v-alert
        v-else-if="periods && periods.items.length === 0"
        type="info"
        variant="tonal"
        data-test="no-periods"
      >
        Reports start once the project is in delivery.
      </v-alert>
      <v-table v-else-if="periods" density="compact">
        <thead>
          <tr>
            <th>Period</th>
            <th>Due</th>
            <th>Reports</th>
            <th v-if="canWrite"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="period in periods.items" :key="period.end" data-test="period">
            <td>{{ period.start }} to {{ period.end }}</td>
            <td>{{ period.due_on }}</td>
            <td>
              <span v-if="period.reports.length === 0" class="text-medium-emphasis">None</span>
              <v-chip
                v-for="item in period.reports"
                :key="item.id"
                size="small"
                class="mr-1"
                :color="item.status === 'withdrawn' ? undefined : RATING_COLOR[item.overall]"
                :variant="item.id === reportId ? 'flat' : 'tonal'"
                data-test="period-report"
                @click="openReport(item)"
              >
                {{ STATUS_TEXT[item.status] }}, {{ RATING_TEXT[item.overall] }}
              </v-chip>
            </td>
            <td v-if="canWrite">
              <v-btn
                v-if="!liveReport(period) || liveReport(period)!.status === 'withdrawn'"
                size="small"
                variant="outlined"
                :disabled="busy"
                data-test="start-draft"
                @click="startDraft(period)"
              >
                Start draft
              </v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>
    </section>

    <section v-if="report" data-test="report">
      <div class="d-flex flex-wrap align-center ga-2 mb-2">
        <h2 class="text-h6" data-test="report-title">
          {{ report.period_start }} to {{ report.period_end }}
        </h2>
        <v-chip size="small" data-test="report-status">{{ STATUS_TEXT[report.status] }}</v-chip>
        <v-btn
          v-if="report.status === 'published'"
          size="small"
          variant="text"
          :to="{
            name: 'status-report-executive',
            params: { id: String(projectId), reportId: String(report.id) },
          }"
          data-test="executive-link"
        >
          One-page view
        </v-btn>
      </div>
      <p class="text-caption text-medium-emphasis mb-2" data-test="report-meta">
        Due {{ report.due_on }}. Figures read {{ new Date(report.figures_at).toLocaleString() }}.
        <span v-if="report.published_by_name">Published by {{ report.published_by_name }}.</span>
      </p>
      <v-alert
        v-if="report.replaces_id"
        type="info"
        variant="tonal"
        class="mb-2"
        data-test="report-replaces"
      >
        Corrects report {{ report.replaces_id }}: {{ report.replace_reason }}
      </v-alert>
      <v-alert
        v-if="report.status === 'withdrawn'"
        type="warning"
        variant="tonal"
        class="mb-2"
        data-test="report-withdrawn"
      >
        Withdrawn: {{ report.withdrawn_reason }}
        <span v-if="report.replaced_by_id">
          Replaced by
          <a href="#" data-test="replaced-by" @click.prevent="open(report.replaced_by_id!)">
            report {{ report.replaced_by_id }}</a
          >.
        </span>
      </v-alert>

      <h3 class="text-subtitle-1 mt-4 mb-2">Ratings</h3>
      <v-table density="compact" data-test="ratings">
        <tbody>
          <tr v-for="item in RATED" :key="item.name" :data-test="`rating-${item.name}`">
            <th scope="row">{{ item.title }}</th>
            <td>
              <v-chip size="small" :color="RATING_COLOR[report.ratings[item.name].rating]">
                {{ RATING_TEXT[report.ratings[item.name].rating] }}
              </v-chip>
            </td>
            <td>
              {{ report.ratings[item.name].text }}
              <div class="text-caption text-medium-emphasis" data-test="rating-rule">
                Rule: {{ report.ratings[item.name].rule }}
              </div>
            </td>
          </tr>
        </tbody>
      </v-table>

      <h3 class="text-subtitle-1 mt-4 mb-2">Figures</h3>
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

      <h3 class="text-subtitle-1 mt-4 mb-2">Text</h3>
      <div v-for="section in SECTIONS" :key="section.name" class="mb-3">
        <div
          v-if="report.section_states?.[section.name]?.unreviewed"
          class="d-flex align-center ga-2 mb-1"
        >
          <v-chip
            size="small"
            color="warning"
            variant="tonal"
            :data-test="`agent-chip-${section.name}`"
          >
            Agent draft, not reviewed
          </v-chip>
          <v-btn
            v-if="editable"
            size="small"
            variant="text"
            :disabled="busy"
            :data-test="`confirm-${section.name}`"
            @click="confirmSection(section.name)"
          >
            Confirm
          </v-btn>
        </div>
        <template v-if="editable">
          <v-textarea
            v-model="draftText[section.name]"
            :label="section.title"
            :counter="MAX_SECTION"
            :maxlength="MAX_SECTION"
            rows="2"
            auto-grow
            hide-details="auto"
            :data-test="`section-input-${section.name}`"
          />
        </template>
        <template v-else>
          <h4 class="text-body-2 font-weight-bold">{{ section.title }}</h4>
          <p class="pre text-body-2" :data-test="`section-${section.name}`">
            {{ report.sections[section.name] || 'Nothing written.' }}
          </p>
        </template>
        <ul
          v-if="flagsBySection[section.name]"
          class="pl-4 text-caption text-warning"
          :data-test="`flags-${section.name}`"
        >
          <li v-for="text in flagsBySection[section.name]" :key="text">
            {{ text }} isn't one of the report's figures
          </li>
        </ul>
      </div>

      <div v-if="canWrite && report.status !== 'withdrawn'" class="mt-4" data-test="actions">
        <div v-if="editable" class="d-flex flex-wrap ga-2 mb-3">
          <v-btn color="primary" :disabled="busy" data-test="save" @click="save">Save text</v-btn>
          <v-btn variant="outlined" :disabled="busy" data-test="refresh" @click="refresh">
            Read figures again
          </v-btn>
          <v-btn
            color="secondary"
            :disabled="busy || report.flags.length > 0 || unreviewed.length > 0"
            data-test="publish"
            @click="publish"
          >
            Publish
          </v-btn>
        </div>
        <p v-if="editable && unreviewed.length" class="text-caption mb-3" data-test="unreviewed">
          Confirm or edit each of the agent's sections before publishing.
        </p>
        <v-text-field
          v-model="reason"
          :label="isDraft ? 'Reason to withdraw' : 'Reason for a correction or withdrawal'"
          :maxlength="MAX_REASON"
          density="compact"
          hide-details="auto"
          class="mb-2"
          data-test="reason"
        />
        <div class="d-flex flex-wrap ga-2">
          <v-btn
            v-if="!isDraft"
            variant="outlined"
            :disabled="busy || !reason.trim()"
            data-test="correct"
            @click="correct"
          >
            Start a correction
          </v-btn>
          <v-btn
            color="error"
            variant="outlined"
            :disabled="busy || !reason.trim()"
            data-test="withdraw"
            @click="withdraw"
          >
            Withdraw
          </v-btn>
        </div>
      </div>
    </section>
  </div>
</template>
