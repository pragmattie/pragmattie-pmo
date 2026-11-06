<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  type AnyGate,
  canDecideGate,
  type GateOutcome,
  type GateSubject,
  gateApi,
  gateVersion,
  MAX_GATE_NOTE,
  MAX_GATE_REASON,
  MIN_GATE_REASON,
  outcomeChoices,
  outcomeText,
  type PersonTick,
  readyToPass,
} from '../lib/gates'
import { ApiError, type Person } from '../lib/risks'
import { useAuthStore } from '../stores/auth'

// A stage gate: the approval gate on a request, or the go-ahead gate on a project. Each criterion
// with a tick or a cross, the person criteria as tick boxes with notes, and Pass, Return and
// Decline for a role the gate lists (never on the person's own request or project). Each decision
// asks for a reason. The ticks stay in the form until a decision is sent, and the API records them
// with it. The API checks every rule again. `version` reloads the gate when its subject changes.
const props = defineProps<{ subject: GateSubject; subjectId: number; version?: number }>()
const emit = defineEmits<{
  decided: [message: string]
  conflict: [message: string, items: string[]]
  loaded: [gate: AnyGate]
}>()

const auth = useAuthStore()
const gate = ref<AnyGate | null>(null)
const loadError = ref('')
const ticks = ref<PersonTick[]>([])
const outcome = ref<GateOutcome | null>(null)
const reason = ref('')
const busy = ref(false)
const formError = ref('')
// Passing a request creates the project, so the sponsor names its project manager.
const projectManagers = ref<Person[]>([])
const projectManagerId = ref<number | null>(null)

const mayDecide = computed(
  () => !!gate.value && canDecideGate(gate.value, auth.user, auth.can('gate:decide')),
)
const passReady = computed(() => !!gate.value && readyToPass(gate.value, ticks.value))
const reasonValid = computed(() => reason.value.trim().length >= MIN_GATE_REASON)
const namesManager = computed(() => props.subject === 'request')
const sendReady = computed(
  () =>
    reasonValid.value &&
    (outcome.value !== 'pass' || !namesManager.value || projectManagerId.value !== null),
)
const decision = computed(() => gate.value?.latest_decision ?? null)
const choices = computed(() => outcomeChoices(props.subject))
const chosen = computed(() => choices.value.find((o) => o.value === outcome.value) ?? null)
const fallbackName = computed(() =>
  props.subject === 'project' ? 'Go-ahead gate' : 'Approval gate',
)

// Keep the ticks and notes typed so far when the gate reloads; add any new person criterion.
function syncTicks(loaded: AnyGate) {
  ticks.value = loaded.criteria
    .filter((c) => c.kind === 'person')
    .map((c) => ticks.value.find((t) => t.id === c.id) ?? { id: c.id, met: false, note: '' })
}

async function load() {
  loadError.value = ''
  try {
    gate.value = await gateApi.get(props.subject, props.subjectId)
    syncTicks(gate.value)
    emit('loaded', gate.value)
  } catch (error) {
    gate.value = null
    loadError.value = (error as Error).message
  }
}

watch(
  () => [props.subject, props.subjectId, props.version] as const,
  () => {
    load()
  },
  { immediate: true },
)

function tick(id: string): PersonTick {
  return ticks.value.find((t) => t.id === id) ?? { id, met: false, note: '' }
}

async function ask(value: GateOutcome) {
  outcome.value = value
  reason.value = ''
  formError.value = ''
  projectManagerId.value = null
  if (value !== 'pass' || !namesManager.value) return
  try {
    projectManagers.value = (await gateApi.projectManagers()).items
  } catch (error) {
    formError.value = (error as Error).message
  }
}

async function send() {
  const current = gate.value
  const choice = chosen.value
  if (!current || !choice || !sendReady.value) return
  busy.value = true
  formError.value = ''
  try {
    const criteria = ticks.value.map((t) => ({
      id: t.id,
      met: t.met,
      note: t.note?.trim() ? t.note.trim() : null,
    }))
    gate.value = await gateApi.decide(props.subject, props.subjectId, {
      version: gateVersion(current),
      outcome: choice.value,
      reason: reason.value.trim(),
      criteria,
      ...(choice.value === 'pass' && namesManager.value && projectManagerId.value !== null
        ? { project_manager_id: projectManagerId.value }
        : {}),
    })
    outcome.value = null
    ticks.value = []
    syncTicks(gate.value)
    emit('loaded', gate.value)
    emit('decided', choice.done)
  } catch (error) {
    if (error instanceof ApiError && (error.status === 409 || error.status === 403)) {
      outcome.value = null
      emit('conflict', error.message, error.missing)
    } else {
      formError.value = (error as Error).message
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="mt-6" data-test="gate">
    <h2 class="text-subtitle-1 mb-2">{{ gate?.gate.name ?? fallbackName }}</h2>
    <v-alert v-if="loadError" type="info" variant="tonal" class="mb-2" data-test="gate-error">
      {{ loadError }}
    </v-alert>
    <template v-if="gate">
      <p class="text-caption text-medium-emphasis mb-2" data-test="gate-deciders">
        From {{ gate.gate.from_stage }} to {{ gate.gate.to_stage }}. Decided by:
        {{ gate.gate.deciders.join(', ') }}.
      </p>
      <v-list density="compact" class="py-0" data-test="gate-criteria">
        <v-list-item
          v-for="criterion in gate.criteria"
          :key="criterion.id"
          class="px-0"
          :data-test="`criterion-${criterion.id}`"
        >
          <template v-if="criterion.kind === 'check'">
            <div class="d-flex align-center ga-2">
              <v-icon
                :icon="criterion.met ? '$success' : '$error'"
                :color="criterion.met ? 'success' : 'error'"
                size="small"
              />
              <span class="text-body-2">{{ criterion.text }}</span>
              <span class="sr-only" data-test="criterion-result">
                {{ criterion.met ? 'Met' : 'Not met' }}
              </span>
            </div>
            <div v-if="criterion.detail" class="text-caption text-medium-emphasis ml-7">
              {{ criterion.detail }}
            </div>
          </template>
          <template v-else-if="mayDecide">
            <v-checkbox
              v-model="tick(criterion.id).met"
              :label="criterion.text"
              density="compact"
              hide-details
              data-test="person-tick"
            />
            <v-text-field
              v-model="tick(criterion.id).note"
              label="Note"
              density="compact"
              class="ml-8"
              :counter="MAX_GATE_NOTE"
              :maxlength="MAX_GATE_NOTE"
              :rules="[(v: string) => !tick(criterion.id).met || !!v?.trim() || 'A tick needs a note']"
              data-test="person-note"
            />
          </template>
          <div v-else class="d-flex align-center ga-2">
            <v-icon icon="$checkboxOff" size="small" />
            <span class="text-body-2">{{ criterion.text }}</span>
            <span class="text-caption text-medium-emphasis">(the person deciding ticks this)</span>
          </div>
        </v-list-item>
      </v-list>

      <p v-if="decision" class="text-body-2 mt-2" data-test="gate-decision">
        {{ outcomeText(decision.outcome, subject) }} by
        {{ decision.decided_by_name ?? `User ${decision.decided_by}` }} on
        {{ new Date(decision.created_at).toLocaleString() }}: {{ decision.reason }}
      </p>

      <div v-if="mayDecide" class="d-flex flex-wrap align-center ga-2 mt-2" data-test="gate-actions">
        <v-btn
          v-for="choice in choices"
          :key="choice.value"
          :color="choice.color"
          :variant="choice.value === 'pass' ? 'flat' : 'outlined'"
          :disabled="choice.value === 'pass' && !passReady"
          :data-test="`gate-${choice.value}`"
          @click="ask(choice.value)"
        >
          {{ choice.title }}
        </v-btn>
        <span v-if="!passReady" class="text-caption text-medium-emphasis" data-test="pass-hint">
          Pass needs every criterion met, and each person criterion ticked with a note.
        </span>
      </div>
    </template>

    <v-dialog :model-value="outcome !== null" max-width="520" @update:model-value="outcome = null">
      <v-card v-if="chosen" data-test="gate-dialog">
        <v-card-title>{{ chosen.title }} this {{ subject }}?</v-card-title>
        <v-card-text>
          <v-alert v-if="formError" type="error" variant="tonal" class="mb-2" data-test="gate-form-error">
            {{ formError }}
          </v-alert>
          <p class="text-body-2 mb-2" data-test="gate-explain">{{ chosen.explain }}</p>
          <template v-if="outcome === 'pass' && namesManager">
            <v-select
              v-model="projectManagerId"
              :items="projectManagers"
              item-title="display_name"
              item-value="id"
              label="Project manager"
              class="mb-2"
              data-test="gate-project-manager"
            />
          </template>
          <v-textarea
            v-model="reason"
            label="Reason"
            rows="3"
            auto-grow
            :counter="MAX_GATE_REASON"
            :maxlength="MAX_GATE_REASON"
            :hint="`At least ${MIN_GATE_REASON} characters`"
            persistent-hint
            data-test="gate-reason"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="outcome = null">Cancel</v-btn>
          <v-btn
            :color="chosen.color"
            variant="flat"
            :disabled="!sendReady"
            :loading="busy"
            data-test="gate-confirm"
            @click="send"
          >
            {{ chosen.title }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </section>
</template>

<style scoped>
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
</style>
