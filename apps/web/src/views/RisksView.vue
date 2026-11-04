<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDisplay } from 'vuetify'

import RiskDrawer from '../components/RiskDrawer.vue'
import RiskForm from '../components/RiskForm.vue'
import RiskHeatMap from '../components/RiskHeatMap.vue'
import {
  AGENT_PROPOSAL,
  ApiError,
  bandColor,
  canAcceptRisk,
  CATEGORIES,
  CONFLICT_MESSAGE,
  type EscalatedRisk,
  type Filters,
  type HeatCell,
  type Heatmap,
  type HistoryEntry,
  type Kind,
  isAgentProposal,
  KINDS,
  label,
  listQuery,
  mayClearEscalation,
  MIN_ACCEPT_REASON,
  NEEDS_JUDGEMENT,
  NO_FILTERS,
  openTotal,
  type Person,
  type Project,
  type Risk,
  riskApi,
  type RiskInput,
  type RiskTemplate,
  scoreText,
  STATUSES,
  suggestionMessage,
  toggleCell,
} from '../lib/risks'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const display = useDisplay()

// Only a role with project:write gets the add, edit, close, open and reject buttons, and only a
// role at or above a band's owner gets Accept for a risk in it; the API checks too.
const canWrite = computed(() => auth.can('project:write'))
const canAcceptSelected = computed(
  () =>
    !!selected.value &&
    canAcceptRisk(
      selected.value,
      auth.user?.role,
      auth.can('risk:accept'),
      heatmap.value?.bands ?? [],
    ),
)
// The escalation hold: only a role at or above the escalation owner may close an escalated risk.
const canClearSelected = computed(
  () =>
    !!selected.value &&
    mayClearEscalation(
      selected.value,
      auth.user?.role,
      auth.can('risk:accept'),
      heatmap.value?.bands ?? [],
    ),
)

const projects = ref<Project[]>([])
const projectId = ref<number | null>(null)
const people = ref<Person[]>([])
const pageError = ref('')

const filters = reactive<Filters>({ ...NO_FILTERS })
const page = ref(1)
const perPage = ref(25)
const risks = ref<Risk[]>([])
const total = ref(0)
const loading = ref(false)

const heatKind = ref<Kind | null>(null)
const heatmap = ref<Heatmap | null>(null)

const drawerOpen = ref(false)
const selected = ref<Risk | null>(null)
const history = ref<HistoryEntry[]>([])
const historyTotal = ref(0)
const message = ref('')
const messageType = ref<'error' | 'warning' | 'success'>('success')
const busy = ref(false)

const formOpen = ref(false)
const formRisk = ref<Risk | null>(null)
const formError = ref('')

const libraryOpen = ref(false)
const templates = ref<RiskTemplate[]>([])
const chosen = ref<string[]>([])
const libraryError = ref('')

const confirmClose = ref(false)

const acceptOpen = ref(false)
const acceptReason = ref('')
const reviewDate = ref('')
const rejectOpen = ref(false)
const rejectReason = ref('')
// The browser's local date as YYYY-MM-DD; toISOString would give the UTC day.
const today = () => {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}
const acceptReady = computed(
  () => acceptReason.value.trim().length >= MIN_ACCEPT_REASON && reviewDate.value >= today(),
)

// Open risks escalated to the signed-in role, across projects.
const escalations = ref<EscalatedRisk[]>([])
const escalationsTotal = ref(0)
const escalationsError = ref('')

const headers = [
  { title: 'Title', key: 'title', sortable: false },
  { title: 'Kind', key: 'kind', sortable: false },
  { title: 'Category', key: 'category', sortable: false },
  { title: 'Score', key: 'score', sortable: false },
  { title: 'Band', key: 'band', sortable: false },
  { title: 'Owner', key: 'owner_user_id', sortable: false },
  { title: 'Status', key: 'status', sortable: false },
  { title: 'Response', key: 'response', sortable: false },
]

const option = (value: string) => ({ title: label(value), value })
const bandItems = computed(() => (heatmap.value?.bands ?? []).map((b) => b.band))
const peopleById = computed(() => new Map(people.value.map((p) => [p.id, p.display_name])))
const cellFilter = computed(() => filters.probability !== null && filters.impact !== null)

function ownerName(id: number | null): string {
  if (id === null) return 'No owner'
  return peopleById.value.get(id) ?? `Person ${id}`
}

function failed(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.'
}

async function loadRisks() {
  if (projectId.value === null) return
  loading.value = true
  try {
    const result = await riskApi.list(
      projectId.value,
      listQuery(filters, page.value, perPage.value),
    )
    risks.value = result.items
    total.value = result.total
    pageError.value = ''
  } catch (error) {
    pageError.value = failed(error)
  } finally {
    loading.value = false
  }
}

async function loadHeatmap() {
  if (projectId.value === null) return
  try {
    heatmap.value = await riskApi.heatmap(projectId.value, heatKind.value)
  } catch (error) {
    heatmap.value = null
    pageError.value = failed(error)
  }
}

async function loadEscalations() {
  try {
    const result = await riskApi.escalations()
    escalations.value = result.items
    escalationsTotal.value = result.total
    escalationsError.value = ''
  } catch (error) {
    escalationsError.value = failed(error)
  }
}

function refresh() {
  return Promise.all([loadRisks(), loadHeatmap(), loadEscalations()])
}

onMounted(async () => {
  try {
    const [projectPage, peoplePage] = await Promise.all([riskApi.projects(), riskApi.people()])
    projects.value = projectPage.items
    people.value = peoplePage.items
  } catch (error) {
    pageError.value = failed(error)
    return
  }
  const fromRoute = Number(route.query.project)
  const known = projects.value.some((p) => p.id === fromRoute)
  projectId.value = known ? fromRoute : (projects.value[0]?.id ?? null)
})

watch(projectId, (id) => {
  if (id === null) return
  drawerOpen.value = false
  selected.value = null
  if (Number(route.query.project) !== id) router.replace({ query: { ...route.query, project: id } })
  page.value = 1
  refresh()
})

// A change of filter goes back to the first page.
watch(
  () => ({ ...filters }),
  () => {
    if (page.value !== 1) page.value = 1
    else loadRisks()
  },
)
watch([page, perPage], () => loadRisks())
// The kind and status a cell click put in the table filter, so a switch of the heat map's
// kind can take them out again and the table stays in step with the map.
const cellSet = ref<Pick<Filters, 'kind' | 'status'> | null>(null)

watch(heatKind, () => {
  const set = cellSet.value
  const cleared: Partial<Filters> = cellFilter.value ? { probability: null, impact: null } : {}
  if (set && filters.kind === set.kind) cleared.kind = null
  if (set && filters.status === set.status) cleared.status = null
  cellSet.value = null
  if (Object.keys(cleared).length) Object.assign(filters, cleared)
  loadHeatmap()
})

function selectCell(cell: HeatCell) {
  const next = toggleCell(filters, cell, heatKind.value)
  if (next.probability !== null) cellSet.value = { kind: next.kind, status: next.status }
  Object.assign(filters, next)
}

function clearFilters() {
  cellSet.value = null
  Object.assign(filters, NO_FILTERS)
}

async function loadHistory(riskId: number, more = false) {
  if (projectId.value === null) return
  const result = await riskApi.history(projectId.value, riskId, more ? history.value.length : 0)
  history.value = more ? [...history.value, ...result.items] : result.items
  historyTotal.value = result.total
}

async function openRisk(risk: Risk) {
  selected.value = risk
  message.value = ''
  history.value = []
  historyTotal.value = 0
  drawerOpen.value = true
  try {
    await loadHistory(risk.id)
  } catch (error) {
    showMessage(failed(error), 'error')
  }
}

function showMessage(text: string, type: 'error' | 'warning' | 'success') {
  message.value = text
  messageType.value = type
}

// Fetch the stored risk again: after a save, or when someone else changed it first.
async function reloadSelected() {
  if (projectId.value === null || !selected.value) return
  selected.value = await riskApi.get(projectId.value, selected.value.id)
  await loadHistory(selected.value.id)
}

async function afterConflict() {
  showMessage(CONFLICT_MESSAGE, 'warning')
  try {
    await reloadSelected()
  } catch (error) {
    showMessage(failed(error), 'error')
  }
  await refresh()
}

function startAdd() {
  formRisk.value = null
  formError.value = ''
  formOpen.value = true
}

function startEdit() {
  formRisk.value = selected.value
  formError.value = ''
  formOpen.value = true
}

async function save(input: RiskInput) {
  if (projectId.value === null) return
  busy.value = true
  formError.value = ''
  try {
    if (formRisk.value) {
      const updated = await riskApi.update(projectId.value, formRisk.value.id, {
        ...input,
        version: formRisk.value.version,
      })
      formOpen.value = false
      selected.value = updated
      showMessage('Saved.', 'success')
      await loadHistory(updated.id)
    } else {
      const created = await riskApi.create(projectId.value, input)
      formOpen.value = false
      await openRisk(created)
      showMessage('Risk added.', 'success')
    }
    await refresh()
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && formRisk.value) {
      formOpen.value = false
      await afterConflict()
    } else {
      formError.value = failed(error)
      // A refusal (such as the escalation hold on a lower band) shows in the drawer as well.
      if (error instanceof ApiError && error.status === 403 && formRisk.value) {
        showMessage(failed(error), 'error')
      }
    }
  } finally {
    busy.value = false
  }
}

async function closeSelected() {
  confirmClose.value = false
  if (projectId.value === null || !selected.value) return
  busy.value = true
  try {
    selected.value = await riskApi.close(projectId.value, selected.value.id, selected.value.version)
    showMessage('Risk closed.', 'success')
    await Promise.all([loadHistory(selected.value.id), refresh()])
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) await afterConflict()
    else showMessage(failed(error), 'error')
  } finally {
    busy.value = false
  }
}

// Accept, open and reject: each is one request, then the risk, its history and the lists reload.
async function decide(request: () => Promise<Risk>, done: string) {
  busy.value = true
  try {
    selected.value = await request()
    showMessage(done, 'success')
    await Promise.all([loadHistory(selected.value.id), refresh()])
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) await afterConflict()
    else showMessage(failed(error), 'error')
  } finally {
    busy.value = false
  }
}

function startAccept() {
  acceptReason.value = ''
  reviewDate.value = ''
  acceptOpen.value = true
}

async function acceptSelected() {
  const risk = selected.value
  if (projectId.value === null || !risk || !acceptReady.value) return
  const id = projectId.value
  const body = { version: risk.version, reason: acceptReason.value, review_date: reviewDate.value }
  acceptOpen.value = false
  await decide(() => riskApi.accept(id, risk.id, body), 'Risk accepted.')
}

async function openSelected() {
  const risk = selected.value
  if (projectId.value === null || !risk) return
  const id = projectId.value
  await decide(() => riskApi.open(id, risk.id, risk.version), 'Risk opened.')
}

function startReject() {
  rejectReason.value = ''
  rejectOpen.value = true
}

async function rejectSelected() {
  const risk = selected.value
  if (projectId.value === null || !risk || !rejectReason.value.trim()) return
  const id = projectId.value
  const body = { version: risk.version, reason: rejectReason.value }
  rejectOpen.value = false
  await decide(() => riskApi.reject(id, risk.id, body), 'Risk rejected.')
}

// An escalation may be on another project: switch to it (its watcher closes the drawer and
// reloads), then show the risk.
async function showEscalation(item: EscalatedRisk) {
  if (projectId.value !== item.project_id) {
    projectId.value = item.project_id
    await nextTick()
  }
  await openRisk(item)
}

// The risk agent: what it found, or why it could not run (it is off, or it failed).
const suggesting = ref(false)
const suggestion = ref('')
const suggestionType = ref<'error' | 'info' | 'success'>('info')

async function suggestRisks() {
  if (projectId.value === null) return
  suggesting.value = true
  suggestion.value = ''
  try {
    const result = await riskApi.suggest(projectId.value)
    suggestion.value = suggestionMessage(result)
    suggestionType.value = result.saved.length ? 'success' : 'info'
    if (result.saved.length) await refresh()
  } catch (error) {
    suggestion.value = failed(error)
    suggestionType.value = 'error'
  } finally {
    suggesting.value = false
  }
}

async function startLibrary() {
  chosen.value = []
  libraryError.value = ''
  libraryOpen.value = true
  try {
    templates.value = (await riskApi.library()).items
  } catch (error) {
    templates.value = []
    libraryError.value = failed(error)
  }
}

async function addFromLibrary() {
  if (projectId.value === null || !chosen.value.length) return
  busy.value = true
  libraryError.value = ''
  try {
    await riskApi.fromLibrary(projectId.value, chosen.value)
    libraryOpen.value = false
    await refresh()
  } catch (error) {
    libraryError.value = failed(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <v-container fluid class="py-6">
    <div class="d-flex flex-wrap align-center ga-3 mb-4">
      <h1 class="text-h4 text-primary flex-grow-1" data-test="page-title">Risk register</h1>
      <template v-if="canWrite && projectId !== null">
        <v-btn color="primary" variant="flat" data-test="add-risk" @click="startAdd">
          Add a risk
        </v-btn>
        <v-btn
          color="secondary"
          variant="outlined"
          data-test="add-from-library"
          @click="startLibrary"
        >
          Add from the library
        </v-btn>
        <v-btn
          color="secondary"
          variant="outlined"
          :loading="suggesting"
          data-test="suggest-risks"
          @click="suggestRisks"
        >
          Suggest risks
        </v-btn>
      </template>
    </div>

    <div class="d-flex flex-wrap align-center column-gap-3">
      <v-select
        v-model="projectId"
        :items="projects"
        item-title="name"
        item-value="id"
        label="Project"
        :max-width="display.xs.value ? undefined : 420"
        data-test="project-picker"
      />
      <v-btn
        v-if="projectId !== null"
        variant="text"
        color="primary"
        class="mb-5"
        :to="{ name: 'project', params: { id: projectId } }"
        data-test="project-brief-link"
      >
        Open the project's brief
      </v-btn>
    </div>
    <v-alert
      v-if="suggestion"
      :type="suggestionType"
      variant="tonal"
      closable
      class="mb-4"
      data-test="suggest-result"
      @click:close="suggestion = ''"
    >
      {{ suggestion }}
    </v-alert>
    <v-alert v-if="pageError" type="error" variant="tonal" class="mb-4" data-test="page-error">
      {{ pageError }}
    </v-alert>
    <p v-if="!projects.length && !pageError" class="text-body-1" data-test="no-projects">
      There are no projects yet.
    </p>

    <v-card variant="outlined" class="pa-3 mb-4" data-test="escalations">
      <h2 class="text-subtitle-1 mb-2">
        Escalated to you ({{ label(auth.user?.role ?? '') }}): {{ escalationsTotal }}
      </h2>
      <v-alert v-if="escalationsError" type="error" variant="tonal" density="compact">
        {{ escalationsError }}
      </v-alert>
      <p v-else-if="!escalations.length" class="text-body-2" data-test="no-escalations">
        Nothing is escalated to your role.
      </p>
      <v-list v-else density="compact" class="py-0">
        <v-list-item
          v-for="item in escalations"
          :key="item.id"
          class="px-0"
          :data-test="`escalation-${item.id}`"
          @click="showEscalation(item)"
        >
          <v-list-item-title class="text-body-2">
            <v-chip :color="bandColor(item.band)" variant="flat" size="x-small" class="mr-2">
              {{ item.band }}
            </v-chip>
            {{ item.title }}
          </v-list-item-title>
          <v-list-item-subtitle>{{ item.project_name }}, score {{ item.score }}</v-list-item-subtitle>
        </v-list-item>
      </v-list>
    </v-card>

    <v-row v-if="projectId !== null">
      <v-col cols="12" md="4" order-md="2">
        <v-card variant="outlined" class="pa-3" data-test="heat-map-card">
          <div class="d-flex flex-wrap align-center ga-2 mb-2">
            <h2 class="text-subtitle-1 flex-grow-1">Open risks</h2>
            <v-btn-toggle
              v-model="heatKind"
              density="compact"
              variant="outlined"
              divided
              data-test="heat-kind"
            >
              <v-btn :value="null" size="small" data-test="heat-kind-all">All</v-btn>
              <v-btn
                v-for="kind in KINDS"
                :key="kind"
                :value="kind"
                size="small"
                :data-test="`heat-kind-${kind}`"
              >
                {{ kind === 'threat' ? 'Threats' : 'Opportunities' }}
              </v-btn>
            </v-btn-toggle>
          </div>
          <RiskHeatMap
            :cells="heatmap?.cells ?? []"
            :selected="{ probability: filters.probability, impact: filters.impact }"
            @select="selectCell"
          />
          <div class="d-flex flex-wrap ga-2 mt-3" data-test="band-totals">
            <span class="text-body-2 mr-1" data-test="open-total">
              {{ openTotal(heatmap) }} open
            </span>
            <v-chip
              v-for="band in heatmap?.bands ?? []"
              :key="band.band"
              :color="bandColor(band.band)"
              variant="flat"
              size="small"
              :data-test="`band-total-${band.band}`"
            >
              {{ band.band }}: {{ band.count }}
            </v-chip>
            <v-chip
              v-if="heatmap?.unscored"
              variant="outlined"
              size="small"
              data-test="band-total-unscored"
            >
              {{ NEEDS_JUDGEMENT }}: {{ heatmap.unscored }}
            </v-chip>
          </div>
        </v-card>
      </v-col>

      <v-col cols="12" md="8" order-md="1">
        <v-row density="compact" data-test="filters">
          <v-col cols="6" sm="3">
            <v-select
              v-model="filters.status"
              label="Status"
              :items="STATUSES.map(option)"
              clearable
              density="compact"
              data-test="filter-status"
            />
          </v-col>
          <v-col cols="6" sm="3">
            <v-select
              v-model="filters.band"
              label="Band"
              :items="bandItems"
              clearable
              density="compact"
              data-test="filter-band"
            />
          </v-col>
          <v-col cols="6" sm="3">
            <v-select
              v-model="filters.category"
              label="Category"
              :items="CATEGORIES.map(option)"
              clearable
              density="compact"
              data-test="filter-category"
            />
          </v-col>
          <v-col cols="6" sm="3">
            <v-select
              v-model="filters.kind"
              label="Kind"
              :items="KINDS.map(option)"
              clearable
              density="compact"
              data-test="filter-kind"
            />
          </v-col>
        </v-row>
        <div v-if="cellFilter" class="d-flex align-center ga-2 mb-2" data-test="cell-filter">
          <span class="text-body-2">
            Showing open risks at probability {{ filters.probability }}, impact
            {{ filters.impact }}.
          </span>
          <v-btn variant="text" size="small" data-test="clear-filters" @click="clearFilters">
            Clear filters
          </v-btn>
        </div>

        <v-data-table-server
          v-model:page="page"
          v-model:items-per-page="perPage"
          :headers="headers"
          :items="risks"
          :items-length="total"
          :loading="loading"
          :items-per-page-options="[10, 25, 50, 100]"
          item-value="id"
          mobile-breakpoint="sm"
          hover
          no-data-text="No risks match."
          data-test="risk-table"
          @click:row="(_event: Event, row: { item: Risk }) => openRisk(row.item)"
        >
          <template #[`item.title`]="{ item }">
            <button
              type="button"
              class="risk-title text-left"
              :data-test="`risk-row-${item.id}`"
              @click.stop="openRisk(item)"
            >
              {{ item.title }}
            </button>
            <v-chip
              v-if="isAgentProposal(item)"
              color="secondary"
              variant="tonal"
              size="x-small"
              class="ml-2"
              :data-test="`agent-chip-${item.id}`"
            >
              {{ AGENT_PROPOSAL }}
            </v-chip>
          </template>
          <template #[`item.kind`]="{ item }">{{ label(item.kind) }}</template>
          <template #[`item.category`]="{ item }">{{ label(item.category) }}</template>
          <template #[`item.score`]="{ item }">
            <span :data-test="`score-${item.id}`">{{ scoreText(item) }}</span>
          </template>
          <template #[`item.band`]="{ item }">
            <v-chip
              v-if="item.band"
              :color="bandColor(item.band)"
              variant="flat"
              size="small"
              :data-test="`band-${item.id}`"
            >
              {{ item.band }}
            </v-chip>
            <span v-else>—</span>
          </template>
          <template #[`item.owner_user_id`]="{ item }">
            {{ ownerName(item.owner_user_id) }}
          </template>
          <template #[`item.status`]="{ item }">{{ label(item.status) }}</template>
          <template #[`item.response`]="{ item }">{{ label(item.response) }}</template>
        </v-data-table-server>
      </v-col>
    </v-row>

    <v-navigation-drawer
      v-model="drawerOpen"
      location="end"
      temporary
      :width="display.xs.value ? display.width.value : 440"
      data-test="drawer"
    >
      <RiskDrawer
        :risk="selected"
        :history="history"
        :history-total="historyTotal"
        :owner-name="ownerName"
        :can-write="canWrite"
        :can-accept="canAcceptSelected"
        :can-clear-escalation="canClearSelected"
        :busy="busy"
        :message="message"
        :message-type="messageType"
        @close="drawerOpen = false"
        @edit="startEdit"
        @close-risk="confirmClose = true"
        @accept="startAccept"
        @open-proposed="openSelected"
        @reject="startReject"
        @more-history="selected && loadHistory(selected.id, true)"
      />
    </v-navigation-drawer>

    <v-dialog v-if="auth.can('risk:accept')" v-model="acceptOpen" max-width="480">
      <v-card data-test="accept-dialog">
        <v-card-title>Accept this risk?</v-card-title>
        <v-card-text>
          <p class="text-body-2 mb-3">
            Accepting means taking no further action beyond watching it. Your name, the reason and
            the review date are recorded.
          </p>
          <v-textarea
            v-model="acceptReason"
            label="Why accept it"
            :hint="`At least ${MIN_ACCEPT_REASON} characters`"
            persistent-hint
            rows="3"
            counter
            maxlength="4000"
            data-test="accept-reason"
          />
          <v-text-field
            v-model="reviewDate"
            label="Review date"
            type="date"
            :min="today()"
            class="mt-2"
            data-test="accept-review-date"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="acceptOpen = false">Cancel</v-btn>
          <v-btn
            color="secondary"
            variant="flat"
            :disabled="!acceptReady"
            :loading="busy"
            data-test="accept-yes"
            @click="acceptSelected"
          >
            Accept risk
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <template v-if="canWrite">
      <v-dialog v-model="rejectOpen" max-width="480">
        <v-card data-test="reject-dialog">
          <v-card-title>Reject this proposed risk?</v-card-title>
          <v-card-text>
            <v-textarea
              v-model="rejectReason"
              label="Why reject it"
              rows="3"
              counter
              maxlength="4000"
              data-test="reject-reason"
            />
          </v-card-text>
          <v-card-actions>
            <v-spacer />
            <v-btn variant="text" @click="rejectOpen = false">Cancel</v-btn>
            <v-btn
              color="primary"
              variant="flat"
              :disabled="!rejectReason.trim()"
              :loading="busy"
              data-test="reject-yes"
              @click="rejectSelected"
            >
              Reject risk
            </v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>

      <RiskForm
        v-model="formOpen"
        :risk="formRisk"
        :people="people"
        :busy="busy"
        :error="formError"
        @save="save"
      />

      <v-dialog v-model="confirmClose" max-width="420">
        <v-card data-test="confirm-close">
          <v-card-title>Close this risk?</v-card-title>
          <v-card-text>
            A closed risk leaves the heat map and the open totals. The change is recorded.
          </v-card-text>
          <v-card-actions>
            <v-spacer />
            <v-btn variant="text" @click="confirmClose = false">Cancel</v-btn>
            <v-btn
              color="primary"
              variant="flat"
              data-test="confirm-close-yes"
              @click="closeSelected"
            >
              Close risk
            </v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>

      <v-dialog v-model="libraryOpen" max-width="640" scrollable>
        <v-card data-test="library">
          <v-card-title>Add from the risk library</v-card-title>
          <v-card-text>
            <p class="text-body-2 mb-2">
              Added risks start as proposed and unscored, for the project's people to score.
            </p>
            <v-alert v-if="libraryError" type="error" variant="tonal" class="mb-2">
              {{ libraryError }}
            </v-alert>
            <v-checkbox
              v-for="template in templates"
              :key="template.id"
              v-model="chosen"
              :value="template.id"
              :label="`${template.title} (${label(template.kind)}, ${label(template.category)})`"
              density="compact"
              hide-details
              :data-test="`template-${template.id}`"
            />
          </v-card-text>
          <v-card-actions>
            <v-spacer />
            <v-btn variant="text" @click="libraryOpen = false">Cancel</v-btn>
            <v-btn
              color="primary"
              variant="flat"
              :disabled="!chosen.length"
              :loading="busy"
              data-test="library-add"
              @click="addFromLibrary"
            >
              Add {{ chosen.length || '' }}
            </v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>
    </template>
  </v-container>
</template>

<style scoped>
.risk-title {
  color: rgb(var(--v-theme-primary));
  font-weight: 500;
  text-decoration: underline;
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
}
</style>
