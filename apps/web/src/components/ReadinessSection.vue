<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import {
  canEditReadiness,
  confirmationText,
  dueText,
  intakeApi,
  isAgentDraft,
  isOverdue,
  MAX_ACTION,
  MAX_ACTIONS,
  MAX_TEXT,
  ownerText,
  type ProjectRequest,
  RATINGS,
  ratingColor,
  ratingText,
  type Readiness,
  READINESS_AREAS,
  type ReadinessActionInput,
  type ReadinessAreaName,
  type ReadinessInput,
} from '../lib/intake'
import { ApiError, type Person, riskApi } from '../lib/risks'

// The readiness check of a request that uses AI: problem, approach and data, each rated green,
// yellow or red with a note and actions. The page shows this section only when the request uses
// AI. The edit form shows only to roles with project:write while the request is a draft; the API
// checks both again. Saving it as a person confirms it, even an agent's draft left unchanged.
const props = defineProps<{
  request: ProjectRequest
  canWrite: boolean
}>()
const emit = defineEmits<{
  saved: []
  conflict: [message: string]
}>()

const readiness = ref<Readiness | null>(null)
const loaded = ref(false)
const loadError = ref('')
const editing = ref(false)
const busy = ref(false)
const formError = ref('')
const people = ref<Person[]>([])

const editable = computed(() => canEditReadiness(props.request, props.canWrite))

async function load() {
  loadError.value = ''
  try {
    readiness.value = await intakeApi.readiness(props.request.id)
  } catch (error) {
    loadError.value = (error as Error).message
  }
  loaded.value = true
}

watch(
  () => [props.request.id, props.request.version] as const,
  () => {
    load()
  },
  { immediate: true },
)
watch(editable, (allowed) => {
  if (!allowed) editing.value = false
})

function blankAction(): ReadinessActionInput {
  return { description: '', owner_id: null, due_date: '' }
}

function blank(): ReadinessInput {
  return {
    problem: { rating: null, note: '', actions: [] },
    approach: { rating: null, note: '', actions: [] },
    data: { rating: null, note: '', actions: [] },
  }
}

function fromReadiness(stored: Readiness): ReadinessInput {
  const area = (key: ReadinessAreaName) => ({
    rating: stored[key].rating,
    note: stored[key].note ?? '',
    actions: stored[key].actions.map(({ description, owner_id, due_date }) => ({
      description,
      owner_id,
      // An agent's draft has no date yet: the person adds one before saving.
      due_date: due_date ?? '',
    })),
  })
  return { problem: area('problem'), approach: area('approach'), data: area('data') }
}

const form = reactive<ReadinessInput>(blank())
const formRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)

async function startEditing() {
  Object.assign(form, readiness.value ? fromReadiness(readiness.value) : blank())
  formError.value = ''
  editing.value = true
  try {
    people.value = (await riskApi.people()).items
  } catch (error) {
    formError.value = (error as Error).message
  }
}

function addAction(area: ReadinessAreaName) {
  if (form[area].actions.length < MAX_ACTIONS) form[area].actions.push(blankAction())
}

function removeAction(area: ReadinessAreaName, index: number) {
  form[area].actions.splice(index, 1)
}

const empty = (value: unknown) => value === null || value === undefined || String(value).trim() === ''
const required = (value: unknown) => (!empty(value) ? true : 'Required')
const maxLength = (max: number) => (value: string | null) =>
  (value ?? '').length <= max || `At most ${max} characters`

// A yellow area needs at least one action; the API refuses it too.
const needsAction = (area: ReadinessAreaName) =>
  form[area].rating === 'yellow' && form[area].actions.length === 0
const formReady = computed(() => READINESS_AREAS.every(({ key }) => !needsAction(key)))

function cleaned(): ReadinessInput {
  const area = (key: ReadinessAreaName) => ({
    rating: form[key].rating,
    note: form[key].note.trim(),
    actions: form[key].actions.map((a) => ({
      description: a.description.trim(),
      owner_id: a.owner_id,
      due_date: a.due_date,
    })),
  })
  return { problem: area('problem'), approach: area('approach'), data: area('data') }
}

async function save() {
  const result = await formRef.value?.validate()
  if ((result && !result.valid) || !formReady.value) return
  formError.value = ''
  busy.value = true
  try {
    const body = { ...cleaned(), version: props.request.version }
    readiness.value = await intakeApi.saveReadiness(props.request.id, body)
    editing.value = false
    emit('saved')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      editing.value = false
      emit('conflict', error.message)
    } else {
      formError.value = (error as Error).message
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="mt-6" data-test="readiness">
    <div class="d-flex align-center ga-2 mb-2">
      <h2 class="text-subtitle-1">Readiness check</h2>
      <v-chip
        v-if="readiness && !editing"
        :color="ratingColor(readiness.overall)"
        size="small"
        variant="flat"
        data-test="readiness-overall"
      >
        Overall: {{ ratingText(readiness.overall) }}
      </v-chip>
      <v-chip
        v-if="readiness && isAgentDraft(readiness)"
        color="secondary"
        size="small"
        variant="outlined"
        data-test="readiness-agent-draft"
      >
        Agent draft, not confirmed
      </v-chip>
      <v-spacer />
      <v-btn
        v-if="editable && !editing && loaded"
        variant="outlined"
        size="small"
        data-test="readiness-edit"
        @click="startEditing"
      >
        {{ readiness ? 'Edit readiness check' : 'Add readiness check' }}
      </v-btn>
    </div>

    <v-alert v-if="loadError" type="error" variant="tonal" class="mb-2" data-test="readiness-error">
      {{ loadError }}
    </v-alert>

    <template v-if="!editing">
      <p v-if="loaded && !readiness && !loadError" class="text-body-2" data-test="readiness-none">
        No readiness check yet. A request that uses AI can't be submitted without one.
      </p>
      <div v-else-if="readiness" data-test="readiness-view">
        <div
          v-for="area in READINESS_AREAS"
          :key="area.key"
          class="mb-3"
          :data-test="`readiness-${area.key}`"
        >
          <div class="d-flex align-center ga-2">
            <span class="text-body-2 font-weight-medium">{{ area.title }}</span>
            <v-chip
              :color="ratingColor(readiness[area.key].rating)"
              size="small"
              variant="flat"
              data-test="rating-chip"
            >
              {{ ratingText(readiness[area.key].rating) }}
            </v-chip>
          </div>
          <div class="text-caption text-medium-emphasis">{{ area.question }}</div>
          <p class="text-body-2 note" data-test="rating-note">
            {{ readiness[area.key].note ?? 'No note yet' }}
          </p>
          <ul v-if="readiness[area.key].actions.length" class="pl-4">
            <li
              v-for="(action, index) in readiness[area.key].actions"
              :key="index"
              class="text-body-2"
              :data-test="`action-${area.key}-${index}`"
            >
              {{ action.description }}
              <span class="text-caption text-medium-emphasis">
                ({{ ownerText(action) }}, {{ dueText(action) }})
              </span>
              <v-chip
                v-if="isOverdue(action)"
                color="error"
                size="x-small"
                variant="outlined"
                class="ml-1"
                data-test="overdue"
              >
                Overdue
              </v-chip>
            </li>
          </ul>
        </div>
        <p class="text-body-2" data-test="readiness-confirmed">
          {{ confirmationText(readiness) }}
        </p>
      </div>
    </template>

    <v-card v-else variant="outlined" data-test="readiness-form">
      <v-card-text>
        <v-alert v-if="formError" type="error" variant="tonal" class="mb-4" data-test="readiness-form-error">
          {{ formError }}
        </v-alert>
        <p v-if="readiness?.source === 'agent' && readiness.confirmed_by === null" class="text-body-2 mb-4">
          An agent drafted this readiness check. Saving it, changed or not, records you as the person
          who confirmed it.
        </p>
        <v-form ref="formRef" @submit.prevent="save">
          <div
            v-for="area in READINESS_AREAS"
            :key="area.key"
            class="mb-4"
            :data-test="`readiness-form-${area.key}`"
          >
            <h3 class="text-subtitle-2">{{ area.title }}</h3>
            <p class="text-caption text-medium-emphasis mb-2">{{ area.question }}</p>
            <v-row density="compact">
              <v-col cols="12" sm="4">
                <v-select
                  v-model="form[area.key].rating"
                  :items="RATINGS.map((r) => ({ title: ratingText(r), value: r }))"
                  label="Rating"
                  :rules="[required]"
                  data-test="field-rating"
                />
              </v-col>
              <v-col cols="12" sm="8">
                <v-textarea
                  v-model="form[area.key].note"
                  label="Note"
                  rows="1"
                  auto-grow
                  :rules="[required, maxLength(MAX_TEXT)]"
                  data-test="field-note"
                />
              </v-col>
            </v-row>
            <v-row
              v-for="(action, index) in form[area.key].actions"
              :key="index"
              density="compact"
              :data-test="`action-form-${area.key}-${index}`"
            >
              <v-col cols="12" sm="5">
                <v-text-field
                  v-model="action.description"
                  :label="`Action ${index + 1}`"
                  :rules="[required, maxLength(MAX_ACTION)]"
                  data-test="field-action-description"
                />
              </v-col>
              <v-col cols="6" sm="3">
                <v-select
                  v-model="action.owner_id"
                  :items="people"
                  item-title="display_name"
                  item-value="id"
                  label="Owner"
                  :rules="[required]"
                  data-test="field-action-owner"
                />
              </v-col>
              <v-col cols="6" sm="3">
                <v-text-field
                  v-model="action.due_date"
                  label="Due"
                  type="date"
                  :rules="[required]"
                  data-test="field-action-due"
                />
              </v-col>
              <v-col cols="12" sm="1" class="d-flex align-center">
                <v-btn
                  variant="text"
                  size="small"
                  :data-test="`remove-action-${area.key}-${index}`"
                  @click="removeAction(area.key, index)"
                >
                  Remove
                </v-btn>
              </v-col>
            </v-row>
            <p v-if="needsAction(area.key)" class="text-caption text-error" data-test="needs-action">
              A yellow rating needs at least one action with an owner and a due date.
            </p>
            <v-btn
              v-if="form[area.key].actions.length < MAX_ACTIONS"
              variant="text"
              size="small"
              :data-test="`add-action-${area.key}`"
              @click="addAction(area.key)"
            >
              Add an action
            </v-btn>
          </div>
        </v-form>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" data-test="readiness-cancel" @click="editing = false">Cancel</v-btn>
        <v-btn color="primary" variant="flat" :loading="busy" data-test="readiness-save" @click="save">
          Save readiness check
        </v-btn>
      </v-card-actions>
    </v-card>
  </section>
</template>

<style scoped>
.note {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
