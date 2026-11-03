<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import RequestForm from '../components/RequestForm.vue'
import {
  type Client,
  clientText,
  intakeApi,
  NO_QUEUE_FILTERS,
  type ProjectRequest,
  type QueueFilters,
  queueQuery,
  REQUEST_STATUSES,
  type RequestInput,
  type Sort,
  SORTS,
} from '../lib/intake'
import { label } from '../lib/risks'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

// Only a role with project:write gets "New request"; the API checks too.
const canWrite = computed(() => auth.can('project:write'))

const clients = ref<Client[]>([])
const filters = reactive<QueueFilters>({ ...NO_QUEUE_FILTERS })
const sort = ref<Sort>('submitted_at')
const page = ref(1)
const perPage = ref(25)
const requests = ref<ProjectRequest[]>([])
const total = ref(0)
const loading = ref(false)
const pageError = ref('')

const formOpen = ref(false)
const formError = ref('')
const busy = ref(false)

const headers = [
  { title: 'Title', key: 'title', sortable: false },
  { title: 'Client', key: 'client_name', sortable: false },
  { title: 'Status', key: 'status', sortable: false },
  { title: 'AI', key: 'uses_ai', sortable: false },
  { title: 'Wanted by', key: 'wanted_by', sortable: false },
  { title: 'Submitted', key: 'submitted_at', sortable: false },
]

const statusItems = REQUEST_STATUSES.map((value) => ({ title: label(value), value }))
const aiItems = [
  { title: 'Uses AI', value: true },
  { title: 'No AI', value: false },
]

async function load() {
  loading.value = true
  pageError.value = ''
  try {
    const result = await intakeApi.list(queueQuery(filters, sort.value, page.value, perPage.value))
    requests.value = result.items
    total.value = result.total
  } catch (error) {
    pageError.value = (error as Error).message
  } finally {
    loading.value = false
  }
}

watch([filters, sort, perPage], () => {
  if (page.value !== 1) page.value = 1
  else load()
})
watch(page, load)

onMounted(async () => {
  try {
    clients.value = (await intakeApi.clients()).items
  } catch {
    // The queue still works; the client filter is just empty.
  }
  await load()
})

function openRequest(request: ProjectRequest) {
  router.push({ name: 'request', params: { id: request.id } })
}

async function create(input: RequestInput) {
  busy.value = true
  formError.value = ''
  try {
    const created = await intakeApi.create(input)
    formOpen.value = false
    openRequest(created)
  } catch (error) {
    formError.value = (error as Error).message
  } finally {
    busy.value = false
  }
}

function newRequest() {
  formError.value = ''
  formOpen.value = true
}

const day = (value: string | null) => (value ? new Date(value).toLocaleDateString() : '—')
</script>

<template>
  <v-container class="py-8">
    <div class="d-flex flex-wrap align-center ga-2 mb-4">
      <h1 class="text-h4 text-primary" data-test="page-title">Intake</h1>
      <v-spacer />
      <v-btn v-if="canWrite" color="primary" data-test="new-request" @click="newRequest">
        New request
      </v-btn>
    </div>
    <v-alert v-if="pageError" type="error" variant="tonal" class="mb-4" data-test="page-error">
      {{ pageError }}
    </v-alert>

    <v-row density="compact" data-test="filters">
      <v-col cols="6" sm="3">
        <v-select
          v-model="filters.status"
          label="Status"
          :items="statusItems"
          clearable
          density="compact"
          data-test="filter-status"
        />
      </v-col>
      <v-col cols="6" sm="3">
        <v-select
          v-model="filters.client_id"
          label="Client"
          :items="clients"
          item-title="name"
          item-value="id"
          clearable
          density="compact"
          data-test="filter-client"
        />
      </v-col>
      <v-col cols="6" sm="3">
        <v-select
          v-model="filters.uses_ai"
          label="AI"
          :items="aiItems"
          clearable
          density="compact"
          data-test="filter-ai"
        />
      </v-col>
      <v-col cols="6" sm="3">
        <v-select
          v-model="sort"
          label="Sort"
          :items="SORTS"
          density="compact"
          data-test="sort"
        />
      </v-col>
    </v-row>

    <v-data-table-server
      v-model:page="page"
      v-model:items-per-page="perPage"
      :headers="headers"
      :items="requests"
      :items-length="total"
      :loading="loading"
      :items-per-page-options="[10, 25, 50, 100]"
      item-value="id"
      mobile-breakpoint="sm"
      hover
      no-data-text="No requests match."
      data-test="request-table"
      @click:row="(_event: Event, row: { item: ProjectRequest }) => openRequest(row.item)"
    >
      <template #[`item.title`]="{ item }">
        <button
          type="button"
          class="request-title text-left"
          :data-test="`request-row-${item.id}`"
          @click.stop="openRequest(item)"
        >
          {{ item.title }}
        </button>
      </template>
      <template #[`item.client_name`]="{ item }">{{ clientText(item) }}</template>
      <template #[`item.status`]="{ item }">
        <span :data-test="`status-${item.id}`">{{ label(item.status) }}</span>
      </template>
      <template #[`item.uses_ai`]="{ item }">
        <v-chip
          v-if="item.uses_ai"
          color="secondary"
          variant="tonal"
          size="small"
          :data-test="`ai-${item.id}`"
        >
          AI
        </v-chip>
        <span v-else>—</span>
      </template>
      <template #[`item.wanted_by`]="{ item }">{{ item.wanted_by ?? '—' }}</template>
      <template #[`item.submitted_at`]="{ item }">{{ day(item.submitted_at) }}</template>
    </v-data-table-server>

    <RequestForm
      v-model="formOpen"
      :request="null"
      :clients="clients"
      :busy="busy"
      :error="formError"
      @save="create"
    />
  </v-container>
</template>

<style scoped>
.request-title {
  color: inherit;
  font-weight: 500;
}
</style>
