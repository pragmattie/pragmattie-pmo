<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { type Baseline, gateApi, type GateSubject } from '../lib/gates'
import { formFor, type InboxEntry, riskLink, subjectLink } from '../lib/inbox'
import { REQUEST_CONFLICT } from '../lib/intake'
import { projectApi } from '../lib/projects'
import {
  agentSuggestion,
  ApiError,
  canAcceptRisk,
  canEditRisk,
  CONFLICT_MESSAGE,
  type HeatCell,
  type Heatmap,
  type HistoryEntry,
  MIN_ACCEPT_REASON,
  mayClearEscalation,
  type Risk,
  riskApi,
} from '../lib/risks'
import { useAuthStore } from '../stores/auth'
import ChangesSection from './ChangesSection.vue'
import GateSection from './GateSection.vue'
import ProposalSection from './ProposalSection.vue'
import RiskDrawer from './RiskDrawer.vue'
import RiskScoreForm from './RiskScoreForm.vue'

// One inbox item's existing form, inside the inbox: the gate decision, the risk with Accept, Open
// and Reject, the proposal with approve, return and mark sent, or the change requests with their
// decision. Each calls the same route it calls elsewhere, and a refusal (403 or 409) shows the
// same way. Steps done on their own page (drafting a value case or a plan) link there.
const props = defineProps<{ entry: InboxEntry }>()
const emit = defineEmits<{ decided: [message: string] }>()

const auth = useAuthStore()
const router = useRouter()
const form = computed(() => formFor(props.entry))
const loadError = ref('')
// A gate refusal (403 or 409): its message and what is still unmet, worded as the request or
// project page words it.
const conflict = ref('')
const conflictItems = ref<string[]>([])

function gateConflict(text: string, items: string[] = []) {
  const stale = props.entry.subject_type === 'request' && text.includes('changed')
  conflict.value = stale ? REQUEST_CONFLICT : text
  conflictItems.value = items
}

// The proposal needs the project's client; the change requests need its latest baseline.
const clientName = ref<string | null>(null)
const baseline = ref<Baseline | null>(null)
const ready = ref(false)

// The risk form.
const risk = ref<Risk | null>(null)
const bands = ref<Heatmap['bands']>([])
const cells = ref<HeatCell[]>([])
const history = ref<HistoryEntry[]>([])
const historyTotal = ref(0)
const busy = ref(false)
const message = ref('')
const messageType = ref<'error' | 'warning' | 'success'>('success')
const acceptOpen = ref(false)
const acceptReason = ref('')
const reviewDate = ref('')
const rejectOpen = ref(false)
const rejectReason = ref('')

const today = () => {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}
const acceptReady = computed(
  () => acceptReason.value.trim().length >= MIN_ACCEPT_REASON && reviewDate.value >= today(),
)
const canAccept = computed(
  () =>
    !!risk.value &&
    canAcceptRisk(risk.value, auth.user?.role, auth.can('risk:accept'), bands.value),
)
// A `score_risk` item, including a risk that needs a person's judgement, is scored in place.
const scoring = computed(
  () =>
    props.entry.step === 'score_risk' &&
    auth.can('project:write') &&
    !!risk.value &&
    canEditRisk(risk.value),
)
const probability = ref<number | null>(null)
const impact = ref<number | null>(null)
const canClear = computed(
  () =>
    !!risk.value &&
    mayClearEscalation(risk.value, auth.user?.role, auth.can('risk:accept'), bands.value),
)

function failed(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.'
}

function show(text: string, type: 'error' | 'warning' | 'success') {
  message.value = text
  messageType.value = type
}

async function loadRisk() {
  const projectId = props.entry.project_id
  if (projectId === null) return
  const [found, heatmap, past] = await Promise.all([
    riskApi.get(projectId, props.entry.subject_id),
    riskApi.heatmap(projectId, null),
    riskApi.history(projectId, props.entry.subject_id),
  ])
  risk.value = found
  bands.value = heatmap.bands
  cells.value = heatmap.cells
  history.value = past.items
  historyTotal.value = past.total
}

async function decideRisk(request: () => Promise<Risk>, done: string) {
  busy.value = true
  try {
    risk.value = await request()
    show(done, 'success')
    emit('decided', done)
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      show(CONFLICT_MESSAGE, 'warning')
      await loadRisk().catch(() => undefined)
    } else {
      show(failed(error), 'error')
    }
  } finally {
    busy.value = false
  }
}

function acceptRisk() {
  const found = risk.value
  const projectId = props.entry.project_id
  if (!found || projectId === null || !acceptReady.value) return
  acceptOpen.value = false
  const body = { version: found.version, reason: acceptReason.value, review_date: reviewDate.value }
  return decideRisk(() => riskApi.accept(projectId, found.id, body), 'Risk accepted.')
}

function openRisk() {
  const found = risk.value
  const projectId = props.entry.project_id
  if (!found || projectId === null) return
  return decideRisk(() => riskApi.open(projectId, found.id, found.version), 'Risk opened.')
}

function rejectRisk() {
  const found = risk.value
  const projectId = props.entry.project_id
  if (!found || projectId === null || !rejectReason.value.trim()) return
  rejectOpen.value = false
  const body = { version: found.version, reason: rejectReason.value }
  return decideRisk(() => riskApi.reject(projectId, found.id, body), 'Risk rejected.')
}

// "Use this score": fills the scoring form with the agent's suggestion; nothing is saved yet.
function useSuggestion() {
  const suggestion = risk.value ? agentSuggestion(risk.value) : null
  if (!suggestion) return
  probability.value = suggestion.probability
  impact.value = suggestion.impact
}

// Saves the score through the existing risk update route, with the risk's version. A version
// conflict shows the register's conflict message and the latest risk.
function saveScore() {
  const found = risk.value
  const projectId = props.entry.project_id
  if (!found || projectId === null || probability.value === null || impact.value === null) return
  const body = { version: found.version, probability: probability.value, impact: impact.value }
  return decideRisk(() => riskApi.update(projectId, found.id, body), 'Risk scored.')
}

// Editing or closing a risk is done in the risk register, at that risk with its drawer open.
function toRegister() {
  router.push(riskLink(props.entry))
}

onMounted(async () => {
  const projectId = props.entry.project_id
  try {
    if (form.value === 'risk') {
      await loadRisk()
      probability.value = risk.value?.probability ?? null
      impact.value = risk.value?.impact ?? null
    }
    if (form.value === 'proposal' && projectId !== null) {
      clientName.value = (await projectApi.get(projectId)).client_name
    }
    if (form.value === 'changes' && projectId !== null) {
      const gate = await gateApi.get('project', projectId)
      baseline.value = 'baseline' in gate ? gate.baseline : null
    }
  } catch (error) {
    loadError.value = failed(error)
  }
  ready.value = true
})
</script>

<template>
  <div data-test="inbox-decision">
    <v-alert v-if="loadError" type="error" variant="tonal" density="compact" class="mb-3">
      {{ loadError }}
    </v-alert>

    <v-alert
      v-if="conflict"
      type="warning"
      variant="tonal"
      density="compact"
      class="mb-3"
      data-test="inbox-conflict"
    >
      {{ conflict }}
      <ul v-if="conflictItems.length" class="ml-4">
        <li v-for="item in conflictItems" :key="item">{{ item }}</li>
      </ul>
    </v-alert>

    <GateSection
      v-if="form === 'gate'"
      :subject="entry.subject_type as GateSubject"
      :subject-id="entry.subject_id"
      @decided="emit('decided', $event)"
      @conflict="gateConflict"
    />

    <template v-else-if="form === 'risk' && risk">
      <RiskScoreForm
        v-if="scoring"
        v-model:probability="probability"
        v-model:impact="impact"
        :cells="cells"
        :busy="busy"
        @save="saveScore"
      />
      <RiskDrawer
        :risk="risk"
        :history="history"
        :history-total="historyTotal"
        :owner-name="(id) => (id === null ? 'No owner' : `Person ${id}`)"
        :can-write="auth.can('project:write')"
        :can-accept="canAccept"
        :can-clear-escalation="canClear"
        :busy="busy"
        :message="message"
        :message-type="messageType"
        @close="() => undefined"
        @edit="toRegister"
        @close-risk="toRegister"
        @accept="acceptOpen = true"
        @open-proposed="openRisk"
        @reject="rejectOpen = true"
        @use-suggestion="useSuggestion"
        @more-history="() => undefined"
      />
      <v-dialog v-model="acceptOpen" max-width="480">
        <v-card data-test="inbox-accept-dialog">
          <v-card-title>Accept this risk?</v-card-title>
          <v-card-text>
            <v-textarea
              v-model="acceptReason"
              label="Why accept it"
              :hint="`At least ${MIN_ACCEPT_REASON} characters`"
              persistent-hint
              rows="3"
              counter
              maxlength="4000"
            />
            <v-text-field
              v-model="reviewDate"
              label="Review date"
              type="date"
              :min="today()"
              class="mt-2"
            />
          </v-card-text>
          <v-card-actions>
            <v-spacer />
            <v-btn variant="text" @click="acceptOpen = false">Cancel</v-btn>
            <v-btn color="secondary" variant="flat" :disabled="!acceptReady" @click="acceptRisk">
              Accept risk
            </v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>
      <v-dialog v-model="rejectOpen" max-width="480">
        <v-card data-test="inbox-reject-dialog">
          <v-card-title>Reject this proposed risk?</v-card-title>
          <v-card-text>
            <v-textarea v-model="rejectReason" label="Why reject it" rows="3" maxlength="4000" />
          </v-card-text>
          <v-card-actions>
            <v-spacer />
            <v-btn variant="text" @click="rejectOpen = false">Cancel</v-btn>
            <v-btn color="primary" variant="flat" :disabled="!rejectReason.trim()" @click="rejectRisk">
              Reject risk
            </v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>
    </template>

    <ProposalSection
      v-else-if="form === 'proposal' && ready && entry.project_id !== null"
      :project-id="entry.project_id"
      :client-name="clientName"
      @decided="emit('decided', $event)"
    />

    <ChangesSection
      v-else-if="form === 'changes' && ready && entry.project_id !== null"
      :project-id="entry.project_id"
      :baseline="baseline"
      @decided="emit('decided', $event)"
    />

    <v-btn
      v-else-if="form === 'link' && subjectLink(entry)"
      :to="subjectLink(entry)!"
      variant="outlined"
      color="primary"
      data-test="inbox-open-subject"
    >
      Open the {{ entry.subject_type === 'request' ? 'request' : 'project' }}
    </v-btn>
  </div>
</template>
