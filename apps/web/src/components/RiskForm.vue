<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import {
  CATEGORIES,
  KINDS,
  label,
  OBJECTIVES,
  type Person,
  RESPONSES_BY_KIND,
  type Risk,
  type RiskInput,
  type RiskScales,
  scaleHint,
  scaleItems,
} from '../lib/risks'

// Adds a blank risk, or edits one. The API checks every rule again; these checks only save a
// round trip. Score and band are never entered: the API works them out from the risk policy.
const props = defineProps<{
  modelValue: boolean
  risk: Risk | null
  people: Person[]
  busy: boolean
  error: string
  // The policy's labelled scales; bare 1 to 5 until they load.
  scales?: RiskScales | null
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  save: [input: RiskInput]
}>()

const MAX_TITLE = 200
const MAX_TEXT = 4000

function blank(): RiskInput {
  return {
    kind: 'threat',
    title: '',
    cause: '',
    effect: '',
    category: 'project',
    objectives: [],
    probability: null,
    impact: null,
    response: 'reduce',
    response_plan: '',
    owner_user_id: null,
    needs_judgement: false,
    show_in_proposal: false,
  }
}

function fromRisk(risk: Risk): RiskInput {
  return {
    kind: risk.kind,
    title: risk.title,
    cause: risk.cause,
    effect: risk.effect,
    category: risk.category,
    objectives: [...risk.objectives],
    probability: risk.probability,
    impact: risk.impact,
    response: risk.response,
    response_plan: risk.response_plan,
    owner_user_id: risk.owner_user_id,
    needs_judgement: risk.needs_judgement,
    show_in_proposal: risk.show_in_proposal,
  }
}

const form = reactive<RiskInput>(blank())
const formRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)

watch(
  () => [props.modelValue, props.risk] as const,
  ([open]) => {
    if (open) Object.assign(form, props.risk ? fromRisk(props.risk) : blank())
  },
  { immediate: true },
)

const responses = computed(() => RESPONSES_BY_KIND[form.kind])
watch(
  () => form.kind,
  () => {
    if (!responses.value.includes(form.response)) form.response = responses.value[0]
  },
)

const probabilityItems = computed(() => scaleItems(props.scales?.probability))
const impactItems = computed(() => scaleItems(props.scales?.impact))
const option = (value: string) => ({ title: label(value), value })
const required = (value: unknown) => (value !== null && value !== '' ? true : 'Required')
const maxLength = (max: number) => (value: string) =>
  (value ?? '').length <= max || `At most ${max} characters`
// Only a risk that needs a person's judgement may be saved without a score.
const levelRule = (value: number | null) =>
  value !== null || form.needs_judgement || 'Required unless it needs a person’s judgement'
const pairRule = () =>
  (form.probability === null) === (form.impact === null) || 'Set probability and impact together'

async function submit() {
  const result = await formRef.value?.validate()
  if (result && !result.valid) return
  emit('save', { ...form, objectives: [...form.objectives] })
}
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="720"
    scrollable
    @update:model-value="emit('update:modelValue', $event)"
  >
    <v-card data-test="risk-form">
      <v-card-title>{{ risk ? 'Edit risk' : 'Add a risk' }}</v-card-title>
      <v-card-text>
        <v-alert v-if="error" type="error" variant="tonal" class="mb-4" data-test="form-error">
          {{ error }}
        </v-alert>
        <v-form ref="formRef" @submit.prevent="submit">
          <v-row density="compact">
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.kind"
                label="Kind"
                :items="KINDS.map(option)"
                data-test="field-kind"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.category"
                label="Category"
                :items="CATEGORIES.map(option)"
                data-test="field-category"
              />
            </v-col>
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
              <v-textarea
                v-model="form.cause"
                label="Cause"
                rows="2"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-cause"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-textarea
                v-model="form.effect"
                label="Effect"
                rows="2"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-effect"
              />
            </v-col>
            <v-col cols="12">
              <v-select
                v-model="form.objectives"
                label="Objectives affected"
                :items="OBJECTIVES.map(option)"
                multiple
                chips
                :rules="[(v: string[]) => v.length > 0 || 'Choose at least one']"
                data-test="field-objectives"
              />
            </v-col>
            <v-col cols="12">
              <v-switch
                v-model="form.needs_judgement"
                color="secondary"
                label="Needs a person's judgement (for example ethical or reputational)"
                hide-details
                data-test="field-needs-judgement"
              />
            </v-col>
            <v-col cols="12">
              <v-switch
                v-model="form.show_in_proposal"
                color="secondary"
                label="Show in the proposal (the client sees its title and response, never its score)"
                hide-details
                data-test="field-show-in-proposal"
              />
            </v-col>
            <v-col cols="6">
              <v-select
                v-model="form.probability"
                label="Probability (1 to 5)"
                :items="probabilityItems"
                :hint="scaleHint(scales?.probability, form.probability)"
                persistent-hint
                :clearable="form.needs_judgement"
                :rules="[levelRule, pairRule]"
                data-test="field-probability"
              />
            </v-col>
            <v-col cols="6">
              <v-select
                v-model="form.impact"
                label="Impact (1 to 5)"
                :items="impactItems"
                :hint="scaleHint(scales?.impact, form.impact)"
                persistent-hint
                :clearable="form.needs_judgement"
                :rules="[levelRule, pairRule]"
                data-test="field-impact"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.response"
                label="Response"
                :items="responses.map(option)"
                data-test="field-response"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.owner_user_id"
                label="Owner"
                :items="people"
                item-title="display_name"
                item-value="id"
                clearable
                data-test="field-owner"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.response_plan"
                label="Response plan"
                rows="2"
                auto-grow
                :rules="[required, maxLength(MAX_TEXT)]"
                data-test="field-response-plan"
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
