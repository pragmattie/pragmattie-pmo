<script setup lang="ts">
import { reactive, ref, watch } from 'vue'

import {
  type Client,
  MAX_TEXT,
  MAX_TITLE,
  type ProjectRequest,
  type RequestInput,
} from '../lib/intake'

// Adds a blank project request, or edits a draft. The API checks every rule again; these checks
// only save a round trip. Who asked for it is the signed-in person, set by the API.
const props = defineProps<{
  modelValue: boolean
  request: ProjectRequest | null
  clients: Client[]
  busy: boolean
  error: string
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  save: [input: RequestInput]
}>()

function blank(): RequestInput {
  return {
    title: '',
    client_id: null,
    asked_for: '',
    problem: '',
    outcome: '',
    budget_low: null,
    budget_high: null,
    wanted_by: null,
    uses_ai: false,
    known_risks: null,
  }
}

function fromRequest(request: ProjectRequest): RequestInput {
  return {
    title: request.title,
    client_id: request.client_id,
    asked_for: request.asked_for,
    problem: request.problem,
    outcome: request.outcome,
    budget_low: request.budget_low,
    budget_high: request.budget_high,
    wanted_by: request.wanted_by,
    uses_ai: request.uses_ai,
    known_risks: request.known_risks,
  }
}

const form = reactive<RequestInput>(blank())
const formRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)

watch(
  () => [props.modelValue, props.request] as const,
  ([open]) => {
    if (open) Object.assign(form, props.request ? fromRequest(props.request) : blank())
  },
  { immediate: true },
)

const MONEY = /^\d{1,10}(\.\d{1,2})?$/
const empty = (value: unknown) => value === null || value === undefined || String(value).trim() === ''
const required = (value: unknown) => (!empty(value) ? true : 'Required')
const maxLength = (max: number) => (value: string | null) =>
  (value ?? '').length <= max || `At most ${max} characters`
const money = (value: string | null) =>
  empty(value) || MONEY.test(String(value).trim()) || 'An amount such as 25000 or 25000.50'
const budgetOrder = () => {
  if (empty(form.budget_low) || empty(form.budget_high)) return true
  return Number(form.budget_low) <= Number(form.budget_high) || 'Low must not be above high'
}

// Empty optional fields go to the API as null.
function cleaned(): RequestInput {
  const orNull = (value: string | null) => (empty(value) ? null : String(value).trim())
  return {
    ...form,
    budget_low: orNull(form.budget_low),
    budget_high: orNull(form.budget_high),
    wanted_by: orNull(form.wanted_by),
    known_risks: orNull(form.known_risks),
  }
}

async function submit() {
  const result = await formRef.value?.validate()
  if (result && !result.valid) return
  emit('save', cleaned())
}
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="720"
    scrollable
    @update:model-value="emit('update:modelValue', $event)"
  >
    <v-card data-test="request-form">
      <v-card-title>{{ request ? 'Edit request' : 'New request' }}</v-card-title>
      <v-card-text>
        <v-alert v-if="error" type="error" variant="tonal" class="mb-4" data-test="form-error">
          {{ error }}
        </v-alert>
        <v-form ref="formRef" @submit.prevent="submit">
          <v-row density="compact">
            <v-col cols="12">
              <v-text-field
                v-model="form.title"
                label="Title"
                :counter="MAX_TITLE"
                :rules="[required, maxLength(MAX_TITLE)]"
                data-test="field-title"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.client_id"
                label="Client (leave empty for internal work)"
                :items="clients"
                item-title="name"
                item-value="id"
                clearable
                data-test="field-client"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.wanted_by"
                label="Wanted by"
                type="date"
                clearable
                data-test="field-wanted-by"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.asked_for"
                label="What is asked for"
                rows="2"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-asked-for"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.problem"
                label="The problem it solves"
                rows="2"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-problem"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.outcome"
                label="The outcome wanted"
                rows="2"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-outcome"
              />
            </v-col>
            <v-col cols="6">
              <v-text-field
                v-model="form.budget_low"
                label="Budget, low (USD)"
                inputmode="decimal"
                :rules="[money, budgetOrder]"
                data-test="field-budget-low"
              />
            </v-col>
            <v-col cols="6">
              <v-text-field
                v-model="form.budget_high"
                label="Budget, high (USD)"
                inputmode="decimal"
                :rules="[money, budgetOrder]"
                data-test="field-budget-high"
              />
            </v-col>
            <v-col cols="12">
              <v-switch
                v-model="form.uses_ai"
                color="secondary"
                label="The work would build or rely on AI"
                hide-details
                data-test="field-uses-ai"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.known_risks"
                label="Risks you already see (optional)"
                rows="2"
                auto-grow
                :rules="[maxLength(MAX_TEXT)]"
                data-test="field-known-risks"
              />
            </v-col>
          </v-row>
        </v-form>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" data-test="form-cancel" @click="emit('update:modelValue', false)">
          Cancel
        </v-btn>
        <v-btn color="primary" variant="flat" :loading="busy" data-test="form-save" @click="submit">
          Save
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
