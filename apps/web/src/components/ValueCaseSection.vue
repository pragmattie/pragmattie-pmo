<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import {
  type Benefit,
  canEditValueCase,
  confirmationText,
  costText,
  intakeApi,
  MAX_BENEFITS,
  MAX_MEASURE,
  MAX_TARGET,
  MAX_TEXT,
  type ProjectRequest,
  type ValueCase,
  type ValueCaseInput,
} from '../lib/intake'
import { ApiError } from '../lib/risks'

// The request's value case: why it is worth doing, how we will know, and what it will cost. The
// edit form shows only to roles with project:write and only while the request is a draft; the API
// checks both again. Saving it as a person confirms it, even an agent's draft left unchanged.
const props = defineProps<{
  request: ProjectRequest
  canWrite: boolean
}>()
const emit = defineEmits<{
  // Saved: the request's version moved on, so the page reloads the request and its history.
  saved: []
  // Refused with a 409 (stale, or no longer a draft): the page reloads and shows the message.
  conflict: [message: string]
}>()

const valueCase = ref<ValueCase | null>(null)
const loaded = ref(false)
const loadError = ref('')
const editing = ref(false)
const busy = ref(false)
const formError = ref('')

const editable = computed(() => canEditValueCase(props.request, props.canWrite))

async function load() {
  loadError.value = ''
  try {
    valueCase.value = await intakeApi.valueCase(props.request.id)
  } catch (error) {
    loadError.value = (error as Error).message
  }
  loaded.value = true
}

// Reload when the request changes: another page, or a new version after a save or a refusal.
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

function blankBenefit(): Benefit {
  return { description: '', measure: null, current_value: null, target: null, target_date: null }
}

function blank(): ValueCaseInput {
  return {
    benefits: [blankBenefit()],
    cost_low: null,
    cost_high: null,
    cost_method: null,
    strategic_fit: '',
    cost_of_not_doing: null,
  }
}

function fromValueCase(stored: ValueCase): ValueCaseInput {
  return {
    benefits: stored.benefits.map((b) => ({ ...b })),
    cost_low: stored.cost_low,
    cost_high: stored.cost_high,
    cost_method: stored.cost_method,
    strategic_fit: stored.strategic_fit,
    cost_of_not_doing: stored.cost_of_not_doing,
  }
}

const form = reactive<ValueCaseInput>(blank())
const formRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)

function startEditing() {
  Object.assign(form, valueCase.value ? fromValueCase(valueCase.value) : blank())
  formError.value = ''
  editing.value = true
}

function addBenefit() {
  if (form.benefits.length < MAX_BENEFITS) form.benefits.push(blankBenefit())
}

function removeBenefit(index: number) {
  if (form.benefits.length > 1) form.benefits.splice(index, 1)
}

const MONEY = /^\d{1,10}(\.\d{1,2})?$/
const empty = (value: unknown) => value === null || value === undefined || String(value).trim() === ''
const required = (value: unknown) => (!empty(value) ? true : 'Required')
const maxLength = (max: number) => (value: string | null) =>
  (value ?? '').length <= max || `At most ${max} characters`
const money = (value: string | null) =>
  empty(value) || MONEY.test(String(value).trim()) || 'An amount such as 25000 or 25000.50'
const costOrder = () => {
  if (empty(form.cost_low) || empty(form.cost_high)) return true
  return Number(form.cost_low) <= Number(form.cost_high) || 'Low must not be above high'
}

// Empty optional fields go to the API as null; text is trimmed.
function cleaned(): ValueCaseInput {
  const orNull = (value: string | null) => (empty(value) ? null : String(value).trim())
  return {
    benefits: form.benefits.map((b) => ({
      description: b.description.trim(),
      measure: orNull(b.measure),
      current_value: orNull(b.current_value),
      target: orNull(b.target),
      target_date: orNull(b.target_date),
    })),
    cost_low: orNull(form.cost_low),
    cost_high: orNull(form.cost_high),
    cost_method: orNull(form.cost_method),
    strategic_fit: form.strategic_fit.trim(),
    cost_of_not_doing: orNull(form.cost_of_not_doing),
  }
}

async function save() {
  const result = await formRef.value?.validate()
  if (result && !result.valid) return
  formError.value = ''
  busy.value = true
  try {
    const body = { ...cleaned(), version: props.request.version }
    valueCase.value = await intakeApi.saveValueCase(props.request.id, body)
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

function benefitText(benefit: Benefit): string {
  const parts = [
    benefit.measure ? `Measured by: ${benefit.measure}` : 'No measure yet',
    benefit.current_value ? `now ${benefit.current_value}` : '',
    benefit.target ? `target ${benefit.target}` : 'no target yet',
    benefit.target_date ? `by ${benefit.target_date}` : '',
  ]
  return parts.filter(Boolean).join(', ')
}
</script>

<template>
  <section class="mt-6" data-test="value-case">
    <div class="d-flex align-center ga-2 mb-2">
      <h2 class="text-subtitle-1">Value case</h2>
      <v-spacer />
      <v-btn
        v-if="editable && !editing && loaded"
        variant="outlined"
        size="small"
        data-test="value-case-edit"
        @click="startEditing"
      >
        {{ valueCase ? 'Edit value case' : 'Add value case' }}
      </v-btn>
    </div>

    <v-alert v-if="loadError" type="error" variant="tonal" class="mb-2" data-test="value-case-error">
      {{ loadError }}
    </v-alert>

    <template v-if="!editing">
      <p v-if="loaded && !valueCase && !loadError" class="text-body-2" data-test="value-case-none">
        No value case yet. A request can't be submitted without one.
      </p>
      <dl v-else-if="valueCase" class="value-case-fields" data-test="value-case-view">
        <dt class="text-caption text-medium-emphasis">Benefits</dt>
        <dd class="mb-2">
          <ol class="pl-4">
            <li
              v-for="(benefit, index) in valueCase.benefits"
              :key="index"
              class="text-body-2"
              :data-test="`benefit-${index}`"
            >
              {{ benefit.description }}
              <div class="text-caption text-medium-emphasis">{{ benefitText(benefit) }}</div>
            </li>
          </ol>
        </dd>
        <dt class="text-caption text-medium-emphasis">Estimated cost</dt>
        <dd class="text-body-2 mb-2" data-test="value-case-cost">{{ costText(valueCase) }}</dd>
        <dt class="text-caption text-medium-emphasis">How the cost was estimated</dt>
        <dd class="text-body-2 mb-2" data-test="value-case-method">
          {{ valueCase.cost_method ?? 'Not given' }}
        </dd>
        <dt class="text-caption text-medium-emphasis">Strategic fit</dt>
        <dd class="text-body-2 mb-2" data-test="value-case-fit">{{ valueCase.strategic_fit }}</dd>
        <dt class="text-caption text-medium-emphasis">Cost of not doing it</dt>
        <dd class="text-body-2 mb-2" data-test="value-case-not-doing">
          {{ valueCase.cost_of_not_doing ?? 'Not given' }}
        </dd>
        <dt class="text-caption text-medium-emphasis">Confirmed</dt>
        <dd class="text-body-2 mb-2" data-test="value-case-confirmed">
          {{ confirmationText(valueCase) }}
        </dd>
      </dl>
    </template>

    <v-card v-else variant="outlined" data-test="value-case-form">
      <v-card-text>
        <v-alert v-if="formError" type="error" variant="tonal" class="mb-4" data-test="value-case-form-error">
          {{ formError }}
        </v-alert>
        <p v-if="valueCase?.source === 'agent' && valueCase.confirmed_by === null" class="text-body-2 mb-4">
          An agent drafted this value case. Saving it, changed or not, records you as the person who
          confirmed it.
        </p>
        <v-form ref="formRef" @submit.prevent="save">
          <h3 class="text-subtitle-2 mb-2">Benefits</h3>
          <v-row
            v-for="(benefit, index) in form.benefits"
            :key="index"
            density="compact"
            class="mb-2"
            :data-test="`benefit-form-${index}`"
          >
            <v-col cols="12">
              <v-textarea
                v-model="benefit.description"
                :label="`Benefit ${index + 1}`"
                rows="1"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-benefit-description"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="benefit.measure"
                label="How it is measured"
                :rules="[maxLength(MAX_MEASURE)]"
                data-test="field-benefit-measure"
              />
            </v-col>
            <v-col cols="6" sm="3">
              <v-text-field
                v-model="benefit.current_value"
                label="Current value"
                :rules="[maxLength(MAX_TARGET)]"
                data-test="field-benefit-current"
              />
            </v-col>
            <v-col cols="6" sm="3">
              <v-text-field
                v-model="benefit.target"
                label="Target"
                :rules="[maxLength(MAX_TARGET)]"
                data-test="field-benefit-target"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="benefit.target_date"
                label="Target met by"
                type="date"
                clearable
                data-test="field-benefit-target-date"
              />
            </v-col>
            <v-col cols="12" sm="6" class="d-flex align-center">
              <v-btn
                v-if="form.benefits.length > 1"
                variant="text"
                size="small"
                :data-test="`remove-benefit-${index}`"
                @click="removeBenefit(index)"
              >
                Remove this benefit
              </v-btn>
            </v-col>
          </v-row>
          <v-btn
            v-if="form.benefits.length < MAX_BENEFITS"
            variant="text"
            size="small"
            class="mb-4"
            data-test="add-benefit"
            @click="addBenefit"
          >
            Add a benefit
          </v-btn>

          <v-row density="compact">
            <v-col cols="6">
              <v-text-field
                v-model="form.cost_low"
                label="Estimated cost, low (USD)"
                inputmode="decimal"
                :rules="[money, costOrder]"
                data-test="field-cost-low"
              />
            </v-col>
            <v-col cols="6">
              <v-text-field
                v-model="form.cost_high"
                label="Estimated cost, high (USD)"
                inputmode="decimal"
                :rules="[money, costOrder]"
                data-test="field-cost-high"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.cost_method"
                label="How the cost was estimated"
                rows="1"
                auto-grow
                :rules="[maxLength(MAX_TEXT)]"
                data-test="field-cost-method"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.strategic_fit"
                label="Strategic fit: the client or practice goal it serves"
                rows="2"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-strategic-fit"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.cost_of_not_doing"
                label="Cost of not doing it (optional)"
                rows="2"
                auto-grow
                :rules="[maxLength(MAX_TEXT)]"
                data-test="field-cost-of-not-doing"
              />
            </v-col>
          </v-row>
        </v-form>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" data-test="value-case-cancel" @click="editing = false">Cancel</v-btn>
        <v-btn color="primary" variant="flat" :loading="busy" data-test="value-case-save" @click="save">
          Save value case
        </v-btn>
      </v-card-actions>
    </v-card>
  </section>
</template>

<style scoped>
.value-case-fields dd {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
