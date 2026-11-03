<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import GateSection from '../components/GateSection.vue'
import ReadinessSection from '../components/ReadinessSection.vue'
import RequestForm from '../components/RequestForm.vue'
import ValueCaseSection from '../components/ValueCaseSection.vue'
import {
  budgetText,
  canDraftRequest,
  canEditRequest,
  canSubmitRequest,
  canWithdrawRequest,
  type Client,
  clientText,
  draftText,
  intakeApi,
  type ProjectRequest,
  REQUEST_CONFLICT,
  type RequestInput,
} from '../lib/intake'
import { ApiError, changedFields, type HistoryEntry, label } from '../lib/risks'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()

const requestId = computed(() => Number(route.params.id))
const request = ref<ProjectRequest | null>(null)
const history = ref<HistoryEntry[]>([])
const historyTotal = ref(0)
const clients = ref<Client[]>([])
const message = ref('')
const messageType = ref<'error' | 'warning' | 'success'>('success')
// What a request still needs before it can be submitted, when the API lists it.
const missing = ref<string[]>([])
const busy = ref(false)

const formOpen = ref(false)
const formError = ref('')
const confirmWithdraw = ref(false)

// Edit and Submit only for a draft, Withdraw for a draft or submitted request, and only for a
// role with project:write. The API checks both again.
const canWrite = computed(() => auth.can('project:write'))
const showEdit = computed(() => !!request.value && canEditRequest(request.value, canWrite.value))
const showSubmit = computed(
  () => !!request.value && canSubmitRequest(request.value, canWrite.value),
)
// "Draft with the agent": a draft request and project:write. The API checks both, and AGENT_MODE.
const showDraft = computed(() => !!request.value && canDraftRequest(request.value, canWrite.value))
const drafting = ref(false)
const showWithdraw = computed(
  () => !!request.value && canWithdrawRequest(request.value, canWrite.value),
)

const fields = computed<[string, string][]>(() => {
  const r = request.value
  if (!r) return []
  return [
    ['Client', clientText(r)],
    ['Status', label(r.status)],
    ['What is asked for', r.asked_for],
    ['The problem it solves', r.problem],
    ['The outcome wanted', r.outcome],
    ['Budget available', budgetText(r)],
    ['Wanted by', r.wanted_by ?? 'Not given'],
    ['Uses AI', r.uses_ai ? 'Yes' : 'No'],
    ['Risks already seen', r.known_risks ?? 'None given'],
    ['Requested by', r.requested_by_name ?? `User ${r.requested_by}`],
    ['Submitted', r.submitted_at ? new Date(r.submitted_at).toLocaleString() : 'Not yet'],
  ]
})

function show(text: string, type: 'error' | 'warning' | 'success', items: string[] = []) {
  message.value = text
  messageType.value = type
  missing.value = items
}

async function loadHistory(offset = 0) {
  const result = await intakeApi.history(requestId.value, offset)
  history.value = offset ? [...history.value, ...result.items] : result.items
  historyTotal.value = result.total
}

async function load() {
  try {
    request.value = await intakeApi.get(requestId.value)
    await loadHistory()
  } catch (error) {
    show((error as Error).message, 'error')
  }
}

onMounted(async () => {
  await load()
  if (canWrite.value) {
    try {
      clients.value = (await intakeApi.clients()).items
    } catch {
      // Editing still works without the client list; only the client can't be changed.
    }
  }
})
watch(requestId, load)

// A 409 means someone else changed it, or it is no longer a draft: show the latest version.
async function act(work: () => Promise<ProjectRequest>, done: string) {
  busy.value = true
  try {
    request.value = await work()
    await loadHistory()
    show(done, 'success')

  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await load()
      show(
        error.message.includes('changed') ? REQUEST_CONFLICT : error.message,
        'warning',
        error.missing,
      )
    } else {
      show((error as Error).message, 'error')
    }
  } finally {
    busy.value = false
  }
}

async function save(input: RequestInput) {
  const current = request.value
  if (!current) return
  formError.value = ''
  busy.value = true
  try {
    request.value = await intakeApi.update(current.id, { ...input, version: current.version })
    formOpen.value = false
    await loadHistory()
    show('Request saved.', 'success')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      // Stale or no longer a draft: close the form and show the latest version.
      formOpen.value = false
      await load()
      show(error.message.includes('changed') ? REQUEST_CONFLICT : error.message, 'warning')
    } else {
      formError.value = (error as Error).message
    }
  } finally {
    busy.value = false
  }
}

function submit() {
  const current = request.value
  if (current) act(() => intakeApi.submit(current.id, current.version), 'Request submitted.')
}

// The agent's sections are saved with a new request version, so reloading the request reloads
// them too. A 409 is AGENT_MODE=off or a request that is no longer a draft.
async function draftWithAgent() {
  const current = request.value
  if (!current) return
  drafting.value = true
  try {
    const result = await intakeApi.draft(current.id)
    await load()
    show(draftText(result), 'success')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await load()
      show(error.message, 'warning')
    } else {
      show((error as Error).message, 'error')
    }
  } finally {
    drafting.value = false
  }
}

async function withdraw() {
  const current = request.value
  confirmWithdraw.value = false
  if (current) await act(() => intakeApi.withdraw(current.id, current.version), 'Request withdrawn.')
}

// The value case was saved: the request has a new version, so reload it and its history.
async function valueCaseSaved() {
  await load()
  show('Value case saved.', 'success')
}

async function readinessSaved() {
  await load()
  show('Readiness check saved.', 'success')
}

async function sectionConflict(text: string, items: string[] = []) {
  await load()
  show(text.includes('changed') ? REQUEST_CONFLICT : text, 'warning', items)
}

// A gate decision changed the request's status and version: reload it and its history.
async function gateDecided(text: string) {
  await load()
  show(text, 'success')
}

function describe(entry: HistoryEntry): string {
  const changed = changedFields(entry)
  return changed.length ? `Changed: ${changed.map(label).join(', ')}` : ''
}
</script>

<template>
  <v-container class="py-8">
    <v-btn variant="text" :to="{ name: 'intake' }" class="mb-2 px-0" data-test="back">
      Back to the queue
    </v-btn>
    <v-alert
      v-if="message"
      :type="messageType"
      variant="tonal"
      class="mb-4"
      closable
      data-test="message"
      @click:close="message = ''"
    >
      {{ message }}
      <ul v-if="missing.length" class="mt-2 pl-4" data-test="missing">
        <li v-for="item in missing" :key="item">{{ item }}</li>
      </ul>
    </v-alert>

    <template v-if="request">
      <div class="d-flex flex-wrap align-center ga-2 mb-4">
        <h1 class="text-h4 text-primary" data-test="page-title">{{ request.title }}</h1>
        <v-spacer />
        <v-btn
          v-if="request.project_id"
          variant="text"
          color="primary"
          :to="{ name: 'project', params: { id: request.project_id } }"
          data-test="project-link"
        >
          Open its project
        </v-btn>
        <v-btn
          v-if="showDraft"
          variant="outlined"
          color="secondary"
          :loading="drafting"
          :disabled="busy"
          data-test="draft-agent"
          @click="draftWithAgent"
        >
          Draft with the agent
        </v-btn>
        <v-btn v-if="showEdit" variant="outlined" data-test="edit" @click="formOpen = true">
          Edit
        </v-btn>
        <v-btn v-if="showSubmit" color="primary" :loading="busy" data-test="submit" @click="submit">
          Submit
        </v-btn>
        <v-btn
          v-if="showWithdraw"
          color="error"
          variant="outlined"
          :loading="busy"
          data-test="withdraw"
          @click="confirmWithdraw = true"
        >
          Withdraw
        </v-btn>
      </div>

      <dl class="request-fields">
        <template v-for="[name, value] in fields" :key="name">
          <dt class="text-caption text-medium-emphasis">{{ name }}</dt>
          <dd class="text-body-2 mb-2" :data-test="`field-${name}`">{{ value }}</dd>
        </template>
      </dl>

      <ValueCaseSection
        :request="request"
        :can-write="canWrite"
        @saved="valueCaseSaved"
        @conflict="sectionConflict"
      />

      <ReadinessSection
        v-if="request.uses_ai"
        :request="request"
        :can-write="canWrite"
        @saved="readinessSaved"
        @conflict="sectionConflict"
      />

      <GateSection :request="request" @decided="gateDecided" @conflict="sectionConflict" />

      <h2 class="text-subtitle-1 mt-4 mb-2">History</h2>
      <p v-if="!history.length" class="text-body-2">No recorded changes.</p>
      <v-list density="compact" data-test="history">
        <v-list-item v-for="entry in history" :key="entry.id" class="px-0">
          <v-list-item-title class="text-body-2">
            {{ label(entry.action) }} (version {{ entry.version }}) by
            {{ entry.actor_name ?? 'the system' }}
          </v-list-item-title>
          <v-list-item-subtitle>
            {{ new Date(entry.created_at).toLocaleString() }}
            <span v-if="describe(entry)">. {{ describe(entry) }}</span>
          </v-list-item-subtitle>
        </v-list-item>
      </v-list>
      <v-btn
        v-if="history.length < historyTotal"
        variant="text"
        size="small"
        data-test="more-history"
        @click="loadHistory(history.length)"
      >
        Show older changes
      </v-btn>
    </template>

    <RequestForm
      v-model="formOpen"
      :request="request"
      :clients="clients"
      :busy="busy"
      :error="formError"
      @save="save"
    />

    <v-dialog v-model="confirmWithdraw" max-width="480">
      <v-card data-test="confirm-withdraw">
        <v-card-title>Withdraw this request?</v-card-title>
        <v-card-text>A withdrawn request is final: it can't be changed or submitted again.</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="confirmWithdraw = false">Cancel</v-btn>
          <v-btn color="error" variant="flat" data-test="confirm-withdraw-yes" @click="withdraw">
            Withdraw
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-container>
</template>

<style scoped>
.request-fields dd {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
