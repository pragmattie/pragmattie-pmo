<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useDisplay } from 'vuetify'

import {
  AUDIT_STATUSES,
  auditApi,
  type AuditFilters,
  auditQuery,
  type AuditRow,
  NO_AUDIT_FILTERS,
  recorded,
  whoText,
} from '../lib/audit'
import { parseUtc } from '../lib/inbox'
import type { Person } from '../lib/risks'

// The audit log, read-only, for roles with audit:read (the API refuses everyone else): every
// agent and human decision, newest first, paged by the API, filtered by agent, subject type,
// status and person. A row opens a drawer with everything it recorded. Nothing here changes a row.
const display = useDisplay()

const filters = reactive<AuditFilters>({ ...NO_AUDIT_FILTERS })
const page = ref(1)
const perPage = ref(25)
const rows = ref<AuditRow[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const people = ref<Person[]>([])

const drawerOpen = ref(false)
const selected = ref<AuditRow | null>(null)

const headers = [
  { title: 'When', key: 'created_at', sortable: false },
  { title: 'Agent', key: 'agent', sortable: false },
  { title: 'Subject', key: 'subject', sortable: false },
  { title: 'Status', key: 'status', sortable: false },
  { title: 'Who', key: 'who', sortable: false },
]

function failed(err: unknown): string {
  return err instanceof Error ? err.message : 'Something went wrong. Please try again.'
}

const when = (value: string) => parseUtc(value).toLocaleString()

async function load() {
  loading.value = true
  try {
    const result = await auditApi.list(auditQuery(filters, page.value, perPage.value))
    rows.value = result.items
    total.value = result.total
    error.value = ''
  } catch (err) {
    error.value = failed(err)
  } finally {
    loading.value = false
  }
}

function open(row: AuditRow) {
  selected.value = row
  drawerOpen.value = true
}

function clearFilters() {
  Object.assign(filters, NO_AUDIT_FILTERS)
}

// Everything the row recorded, in the order a reader checks it.
const details = computed(() => {
  const row = selected.value
  if (!row) return []
  const tokens = (value: number | null) => (value === null ? '—' : String(value))
  return [
    ['When', when(row.created_at)],
    ['Who', whoText(row)],
    ['Agent', row.agent],
    ['Trigger', row.trigger],
    ['Subject', `${row.subject_type} ${row.subject_id}`],
    ['Version', `${row.version} (attempt ${row.attempt})`],
    ['Status', row.status],
    ['Tier', recorded(row.tier)],
    ['Inputs', recorded(row.signals)],
    ['Output', recorded(row.output)],
    ['Action taken', recorded(row.action_taken)],
    ['Override', recorded(row.human_override)],
    ['Model', recorded(row.model)],
    ['Prompt version', recorded(row.prompt_version)],
    ['Input tokens', tokens(row.input_tokens)],
    ['Output tokens', tokens(row.output_tokens)],
    ['Cost (USD)', recorded(row.cost_usd)],
    ['Error', recorded(row.error)],
    ['Supersedes', row.supersedes_id === null ? '—' : `Row ${row.supersedes_id}`],
    ['Row', String(row.id)],
  ]
})

// A change of filter goes back to the first page.
watch(
  () => ({ ...filters }),
  () => {
    if (page.value !== 1) page.value = 1
    else load()
  },
)
watch([page, perPage], () => load())

onMounted(async () => {
  load()
  try {
    people.value = (await auditApi.people()).items
  } catch {
    // The person filter stays empty; the log still loads.
  }
})
</script>

<template>
  <v-container fluid class="py-6">
    <h1 class="text-h4 text-primary mb-4" data-test="page-title">Audit</h1>
    <p class="text-body-2 text-medium-emphasis mb-4">
      Every agent and human decision, newest first. The log is read-only: a correction is a new
      row that supersedes the old one.
    </p>

    <v-alert v-if="error" type="error" variant="tonal" class="mb-4" data-test="audit-error">
      {{ error }}
    </v-alert>

    <v-row density="compact" class="mb-2">
      <v-col cols="12" sm="6" md="3">
        <v-text-field
          v-model="filters.agent"
          label="Agent"
          clearable
          hide-details
          density="compact"
          data-test="filter-agent"
        />
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-text-field
          v-model="filters.subject_type"
          label="Subject type"
          clearable
          hide-details
          density="compact"
          data-test="filter-subject-type"
        />
      </v-col>
      <v-col cols="12" sm="6" md="2">
        <v-select
          v-model="filters.status"
          label="Status"
          :items="AUDIT_STATUSES"
          clearable
          hide-details
          density="compact"
          data-test="filter-status"
        />
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-select
          v-model="filters.actor_user_id"
          label="Person"
          :items="people"
          item-title="display_name"
          item-value="id"
          clearable
          hide-details
          density="compact"
          data-test="filter-person"
        />
      </v-col>
      <v-col cols="12" md="1" class="d-flex align-center">
        <v-btn variant="text" data-test="filter-clear" @click="clearFilters">Clear</v-btn>
      </v-col>
    </v-row>

    <v-data-table-server
      v-model:page="page"
      v-model:items-per-page="perPage"
      :headers="headers"
      :items="rows"
      :items-length="total"
      :loading="loading"
      :items-per-page-options="[10, 25, 50, 100]"
      item-value="id"
      mobile-breakpoint="sm"
      hover
      no-data-text="No audit rows match."
      data-test="audit-table"
      @click:row="(_event: Event, row: { item: AuditRow }) => open(row.item)"
    >
      <template #[`item.created_at`]="{ item }">
        <span :data-test="`audit-row-${item.id}`">{{ when(item.created_at) }}</span>
      </template>
      <template #[`item.subject`]="{ item }">{{ item.subject_type }} {{ item.subject_id }}</template>
      <template #[`item.who`]="{ item }">{{ whoText(item) }}</template>
    </v-data-table-server>

    <v-navigation-drawer
      v-model="drawerOpen"
      location="end"
      temporary
      :width="display.xs.value ? display.width.value : 480"
      data-test="audit-drawer"
    >
      <div v-if="selected" class="pa-4" data-test="audit-detail">
        <div class="d-flex align-start mb-2">
          <h2 class="text-h6 flex-grow-1">Audit row {{ selected.id }}</h2>
          <v-btn
            icon="$close"
            variant="text"
            size="small"
            aria-label="Close details"
            data-test="audit-drawer-close"
            @click="drawerOpen = false"
          />
        </div>
        <dl class="audit-fields">
          <template v-for="[name, value] in details" :key="name">
            <dt class="text-caption text-medium-emphasis">{{ name }}</dt>
            <dd class="text-body-2 mb-2" :data-test="`audit-field-${name}`">{{ value }}</dd>
          </template>
        </dl>
      </div>
    </v-navigation-drawer>
  </v-container>
</template>

<style scoped>
.audit-fields dd {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: inherit;
}
</style>
