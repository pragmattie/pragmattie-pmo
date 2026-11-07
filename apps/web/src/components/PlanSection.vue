<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import {
  amountError,
  budgetFitText,
  canEditPlan,
  CATEGORIES,
  CONTINGENCY_MAX,
  type Deliverable,
  DESCRIPTION_MAX,
  DRAFTED_BY_AGENT,
  dueDayMax,
  type EstimateCategory,
  type EstimateLine,
  type ItemInput,
  type ItemKind,
  LONG_TEXT_MAX,
  type Milestone,
  NAME_MAX,
  ownerText,
  type Plan,
  planApi,
  REPORT_PERIODS,
  type ReportPeriod,
  reportingText,
  WEEKDAYS,
} from '../lib/plans'
import { budgetText, label } from '../lib/projects'
import { ApiError, type Person } from '../lib/risks'
import { useAuthStore } from '../stores/auth'

// The project's plan (item 5.1): what is missing first, then scope and deliverables, milestones in
// date order with who does what, the estimate and its total against the brief's budget range, and
// the reporting plan. The API computes the total and the missing list. Every page of each list is
// read.
//
// While the project is in planning, a role with project:write can change it here (item 6.6): the
// scope, contingency and reporting plan, and add, edit and delete milestones, deliverables and
// estimate lines, each through the plan routes with the version it read. An item the planner agent
// drafted (item 6.5) carries an "Agent draft" chip until a person edits or confirms it; confirming
// marks it a person's without changing it. A stale version (409) shows the API's message and
// reloads the plan. Everyone else, and everyone once the project leaves planning, reads only. The
// web only hides the controls; the API checks every write.
const props = defineProps<{ projectId: number }>()
const auth = useAuthStore()

const plan = ref<Plan | null>(null)
const milestones = ref<Milestone[]>([])
const deliverables = ref<Deliverable[]>([])
const lines = ref<EstimateLine[]>([])
const people = ref<Person[]>([])
const error = ref('')
// A refusal of a write: a stale version or a project that left planning. The plan is reloaded.
const conflict = ref('')
const formError = ref('')
const busy = ref(false)

const editable = computed(() => canEditPlan(plan.value, auth.can('project:write')))
const agentCount = computed(
  () =>
    [...milestones.value, ...deliverables.value, ...lines.value].filter((i) => i.source === 'agent')
      .length,
)

async function fetchAll(id: number) {
  const [found, milestoneItems, deliverableItems, lineItems] = await Promise.all([
    planApi.get(id),
    planApi.milestones(id),
    planApi.deliverables(id),
    planApi.estimateLines(id),
  ])
  plan.value = found
  milestones.value = milestoneItems
  deliverables.value = deliverableItems
  lines.value = lineItems
}

async function load(id: number) {
  error.value = ''
  conflict.value = ''
  plan.value = null
  closeForms()
  try {
    await fetchAll(id)
  } catch (failure) {
    error.value = (failure as Error).message
  }
}

// --- Writes

// Run one write, then reload the plan: every write moves its version on. A 409 (stale, or the
// project left planning) shows the API's message and reloads; anything else stays in the form.
async function write(action: () => Promise<unknown>) {
  busy.value = true
  formError.value = ''
  conflict.value = ''
  try {
    await action()
    closeForms()
    await fetchAll(props.projectId)
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 409) {
      closeForms()
      conflict.value = failure.message
      try {
        await fetchAll(props.projectId)
      } catch (reload) {
        error.value = (reload as Error).message
      }
    } else {
      formError.value = (failure as Error).message
    }
  } finally {
    busy.value = false
  }
}

const empty = (value: unknown) =>
  value === null || value === undefined || String(value).trim() === ''
const orNull = (value: string | null) => (empty(value) ? null : String(value).trim())
const required = (value: unknown) => (!empty(value) ? true : 'Required')
const maxLength = (max: number) => (value: string | null) =>
  (value ?? '').length <= max || `At most ${max} characters`

function closeForms() {
  editingPlan.value = false
  itemForm.kind = null
  pendingDelete.value = null
  formError.value = ''
}

// --- Scope, contingency and the reporting plan

const editingPlan = ref(false)
const planForm = reactive({
  scope_in: '' as string | null,
  scope_out: '' as string | null,
  contingency_pct: '0' as string,
  report_period: null as ReportPeriod | null,
  report_due_day: null as number | null,
})
const planFormRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)

function startEditingPlan() {
  if (!plan.value) return
  closeForms()
  Object.assign(planForm, {
    scope_in: plan.value.scope_in ?? '',
    scope_out: plan.value.scope_out ?? '',
    contingency_pct: String(plan.value.contingency_pct),
    report_period: plan.value.report_period,
    report_due_day: plan.value.report_due_day,
  })
  editingPlan.value = true
}

const contingencyRule = (value: string) =>
  (/^\d+$/.test(String(value).trim()) && Number(value) <= CONTINGENCY_MAX) ||
  `A whole number from 0 to ${CONTINGENCY_MAX}`
const dueDays = computed(() =>
  planForm.report_period
    ? Array.from({ length: dueDayMax(planForm.report_period) }, (_, i) => i + 1)
    : [],
)
const dueDayRule = (value: number | null) => {
  if (!planForm.report_period) return true
  if (value === null || value === undefined) return 'Pick the day the report is due'
  return value <= dueDayMax(planForm.report_period) || 'This day does not fit the period'
}

function periodChanged(period: ReportPeriod | null) {
  planForm.report_period = period
  if (!period || (planForm.report_due_day ?? 0) > dueDayMax(period)) planForm.report_due_day = null
}

function dueDayText(day: number): string {
  if (planForm.report_period === 'monthly') return String(day)
  return WEEKDAYS[day - 1] ?? String(day)
}

async function savePlan() {
  const result = await planFormRef.value?.validate()
  if ((result && !result.valid) || !plan.value) return
  const current = plan.value
  const body = {
    scope_in: orNull(planForm.scope_in),
    scope_out: orNull(planForm.scope_out),
    contingency_pct: Number(String(planForm.contingency_pct).trim()),
    report_period: planForm.report_period,
    report_due_day: planForm.report_period ? planForm.report_due_day : null,
  }
  await write(() => planApi.update(props.projectId, current.version, body))
}

// --- Milestones, deliverables and estimate lines

type AnyItem = Milestone | Deliverable | EstimateLine

const KIND_NAMES: Record<ItemKind, string> = {
  milestone: 'milestone',
  deliverable: 'deliverable',
  estimate_line: 'estimate line',
}

const itemForm = reactive({
  kind: null as ItemKind | null,
  item: null as AnyItem | null,
  name: '',
  target_date: '',
  owner_user_id: null as number | null,
  description: '' as string | null,
  contributors: '' as string | null,
  milestone_id: null as number | null,
  category: 'people' as EstimateCategory,
  amount: '',
})
const itemFormRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)
const pendingDelete = ref<{ kind: ItemKind; item: AnyItem; name: string } | null>(null)

const ownerChoices = computed(() => [
  { title: 'No owner', value: null },
  ...people.value.map((p) => ({ title: p.display_name, value: p.id })),
])
const milestoneChoices = computed(() => [
  { title: 'No milestone', value: null },
  ...milestones.value.map((m) => ({ title: m.name, value: m.id })),
])
const categoryChoices = CATEGORIES.map((c) => ({ title: label(c), value: c }))
const periodChoices = [
  { title: 'Not set', value: null },
  ...REPORT_PERIODS.map((p) => ({ title: label(p), value: p })),
]
const amountRule = (value: string) => amountError(value) ?? true

function startItem(kind: ItemKind, item: AnyItem | null = null) {
  closeForms()
  const m = item as Milestone | null
  const d = item as Deliverable | null
  const l = item as EstimateLine | null
  Object.assign(itemForm, {
    kind,
    item,
    name: kind === 'milestone' ? (m?.name ?? '') : kind === 'deliverable' ? (d?.name ?? '') : '',
    target_date: m?.target_date ?? '',
    owner_user_id: kind === 'milestone' ? (m?.owner_user_id ?? null) : null,
    description: item?.description ?? '',
    contributors: kind === 'milestone' ? (m?.contributors ?? '') : '',
    milestone_id: kind === 'deliverable' ? (d?.milestone_id ?? null) : null,
    category: kind === 'estimate_line' ? (l?.category ?? 'people') : 'people',
    amount: kind === 'estimate_line' ? (l?.amount ?? '') : '',
  })
}

function itemBody(kind: ItemKind): ItemInput {
  if (kind === 'milestone') {
    return {
      name: itemForm.name.trim(),
      target_date: itemForm.target_date,
      owner_user_id: itemForm.owner_user_id,
      description: orNull(itemForm.description),
      contributors: orNull(itemForm.contributors),
    }
  }
  if (kind === 'deliverable') {
    return {
      name: itemForm.name.trim(),
      description: orNull(itemForm.description),
      milestone_id: itemForm.milestone_id,
    }
  }
  return {
    description: String(itemForm.description ?? '').trim(),
    category: itemForm.category,
    amount: itemForm.amount.trim(),
  }
}

async function saveItem() {
  const result = await itemFormRef.value?.validate()
  const kind = itemForm.kind
  if ((result && !result.valid) || !kind || !plan.value) return
  const body = itemBody(kind)
  // The browser checks the amount; the API checks it again.
  if (kind === 'estimate_line' && amountError(itemForm.amount)) return
  const item = itemForm.item
  const planVersion = plan.value.version
  await write(() =>
    item
      ? planApi.updateItem(props.projectId, kind, item.id, item.version, body)
      : planApi.addItem(props.projectId, kind, planVersion, body),
  )
}

function itemName(item: AnyItem): string {
  return 'name' in item ? item.name : item.description
}

function askDelete(kind: ItemKind, item: AnyItem) {
  closeForms()
  pendingDelete.value = { kind, item, name: itemName(item) }
}

async function deleteItem() {
  const target = pendingDelete.value
  if (!target) return
  await write(() =>
    planApi.deleteItem(props.projectId, target.kind, target.item.id, target.item.version),
  )
}

async function confirmItem(kind: ItemKind, item: AnyItem) {
  await write(() => planApi.confirm(props.projectId, item.version, { kind, id: item.id }))
}

async function confirmAll() {
  if (!plan.value) return
  const version = plan.value.version
  await write(() => planApi.confirm(props.projectId, version))
}

// Registered last: loading closes the forms, so they must exist first.
watch(() => props.projectId, load, { immediate: true })

// Milestone owners are picked from the active users; only loaded for someone who can edit.
watch(editable, async (allowed) => {
  if (!allowed) {
    closeForms()
    return
  }
  if (people.value.length) return
  try {
    people.value = await planApi.people()
  } catch {
    people.value = []
  }
})
</script>

<template>
  <section class="mb-6" data-test="plan">
    <div class="d-flex align-center ga-2 mb-2">
      <h2 class="text-subtitle-1">Plan</h2>
      <v-spacer />
      <v-btn
        v-if="editable && agentCount"
        size="small"
        variant="outlined"
        :disabled="busy"
        data-test="plan-confirm-all"
        @click="confirmAll"
      >
        Confirm all
      </v-btn>
    </div>
    <v-alert v-if="error" type="info" variant="tonal" data-test="no-plan">{{ error }}</v-alert>

    <template v-else-if="plan">
      <v-alert
        v-if="conflict"
        type="warning"
        variant="tonal"
        class="mb-4"
        closable
        data-test="plan-conflict"
        @click:close="conflict = ''"
      >
        {{ conflict }}
      </v-alert>
      <v-alert
        v-if="plan.missing.length"
        type="warning"
        variant="tonal"
        class="mb-4"
        data-test="plan-missing"
      >
        <p class="mb-1">This plan is missing:</p>
        <ul class="pl-4">
          <li v-for="gap in plan.missing" :key="gap" data-test="plan-gap">{{ gap }}</li>
        </ul>
      </v-alert>
      <p v-else class="text-body-2 mb-4" data-test="plan-complete">Nothing is missing from this plan.</p>
      <p v-if="!plan.editable" class="text-body-2 text-medium-emphasis mb-4" data-test="plan-baselined">
        The project has left planning, so this plan changes only through change requests.
      </p>

      <v-alert
        v-if="pendingDelete"
        type="warning"
        variant="outlined"
        class="mb-4"
        data-test="plan-delete-confirm"
      >
        Delete the {{ KIND_NAMES[pendingDelete.kind] }} "{{ pendingDelete.name }}"?
        <template #append>
          <v-btn size="small" color="error" :loading="busy" data-test="plan-delete-yes" @click="deleteItem">Delete</v-btn>
          <v-btn size="small" variant="text" @click="pendingDelete = null">Cancel</v-btn>
        </template>
      </v-alert>
      <v-alert v-if="formError" type="error" variant="tonal" class="mb-4" data-test="plan-form-error">
        {{ formError }}
      </v-alert>

      <v-card v-if="editingPlan" variant="outlined" class="pa-4 mb-4" data-test="plan-form">
        <v-form ref="planFormRef" @submit.prevent="savePlan">
          <v-textarea
            v-model="planForm.scope_in"
            label="Scope in"
            rows="2"
            auto-grow
            :rules="[maxLength(LONG_TEXT_MAX)]"
            data-test="plan-field-scope-in"
          />
          <v-textarea
            v-model="planForm.scope_out"
            label="Scope out"
            rows="2"
            auto-grow
            :rules="[maxLength(LONG_TEXT_MAX)]"
            data-test="plan-field-scope-out"
          />
          <v-text-field
            v-model="planForm.contingency_pct"
            label="Contingency %"
            :rules="[contingencyRule]"
            data-test="plan-field-contingency"
          />
          <v-row>
            <v-col cols="12" md="6">
              <v-select
                :model-value="planForm.report_period"
                :items="periodChoices"
                label="Report period"
                data-test="plan-field-period"
                @update:model-value="periodChanged"
              />
            </v-col>
            <v-col cols="12" md="6">
              <v-select
                v-model="planForm.report_due_day"
                :items="dueDays"
                :item-title="dueDayText"
                :disabled="!planForm.report_period"
                :rules="[dueDayRule]"
                label="Due day"
                data-test="plan-field-due-day"
              />
            </v-col>
          </v-row>
          <div class="d-flex ga-2">
            <v-btn type="submit" color="primary" :loading="busy" data-test="plan-save">Save</v-btn>
            <v-btn variant="text" @click="closeForms">Cancel</v-btn>
          </div>
        </v-form>
      </v-card>

      <v-card v-if="itemForm.kind" variant="outlined" class="pa-4 mb-4" data-test="plan-item-form">
        <h3 class="text-body-1 font-weight-medium mb-2">
          {{ itemForm.item ? 'Edit' : 'Add' }} {{ KIND_NAMES[itemForm.kind] }}
        </h3>
        <v-form ref="itemFormRef" @submit.prevent="saveItem">
          <v-text-field
            v-if="itemForm.kind !== 'estimate_line'"
            v-model="itemForm.name"
            label="Name"
            :rules="[required, maxLength(NAME_MAX)]"
            data-test="plan-field-name"
          />
          <v-text-field
            v-if="itemForm.kind === 'milestone'"
            v-model="itemForm.target_date"
            label="Target date"
            type="date"
            :rules="[required]"
            data-test="plan-field-target-date"
          />
          <v-select
            v-if="itemForm.kind === 'milestone'"
            v-model="itemForm.owner_user_id"
            :items="ownerChoices"
            label="Owner"
            data-test="plan-field-owner"
          />
          <v-text-field
            v-model="itemForm.description"
            label="Description"
            :rules="
              itemForm.kind === 'estimate_line'
                ? [required, maxLength(DESCRIPTION_MAX)]
                : [maxLength(DESCRIPTION_MAX)]
            "
            data-test="plan-field-description"
          />
          <v-text-field
            v-if="itemForm.kind === 'milestone'"
            v-model="itemForm.contributors"
            label="Contributors"
            :rules="[maxLength(DESCRIPTION_MAX)]"
            data-test="plan-field-contributors"
          />
          <v-select
            v-if="itemForm.kind === 'deliverable'"
            v-model="itemForm.milestone_id"
            :items="milestoneChoices"
            label="Delivered by"
            data-test="plan-field-milestone"
          />
          <template v-if="itemForm.kind === 'estimate_line'">
            <v-select
              v-model="itemForm.category"
              :items="categoryChoices"
              label="Category"
              data-test="plan-field-category"
            />
            <v-text-field
              v-model="itemForm.amount"
              label="Amount"
              :rules="[amountRule]"
              data-test="plan-field-amount"
            />
          </template>
          <div class="d-flex ga-2">
            <v-btn type="submit" color="primary" :loading="busy" data-test="plan-item-save">Save</v-btn>
            <v-btn variant="text" @click="closeForms">Cancel</v-btn>
          </div>
        </v-form>
      </v-card>

      <div class="d-flex align-center mb-1">
        <h3 class="text-body-1 font-weight-medium">Scope</h3>
        <v-spacer />
        <v-btn
          v-if="editable && !editingPlan"
          size="small"
          variant="text"
          data-test="plan-edit"
          @click="startEditingPlan"
        >
          Edit scope, contingency and reporting
        </v-btn>
      </div>
      <v-table density="compact" class="mb-4">
        <tbody>
          <tr>
            <th scope="row">Scope in</th>
            <td class="pre" data-test="plan-scope-in">{{ plan.scope_in ?? 'Not set' }}</td>
          </tr>
          <tr>
            <th scope="row">Scope out</th>
            <td class="pre">{{ plan.scope_out ?? 'Not set' }}</td>
          </tr>
        </tbody>
      </v-table>

      <div class="d-flex align-center mb-1">
        <h3 class="text-body-1 font-weight-medium">Deliverables</h3>
        <v-spacer />
        <v-btn v-if="editable" size="small" variant="text" data-test="plan-add-deliverable" @click="startItem('deliverable')">
          Add deliverable
        </v-btn>
      </div>
      <p v-if="!deliverables.length" class="text-body-2 mb-4">No deliverables yet.</p>
      <v-table v-else density="compact" class="mb-4">
        <thead>
          <tr>
            <th>Deliverable</th>
            <th>Description</th>
            <th>Delivered by</th>
            <th v-if="editable"><span class="d-sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in deliverables" :key="item.id" data-test="plan-deliverable">
            <td>
              {{ item.name }}
              <v-chip v-if="item.source === 'agent'" size="x-small" color="secondary" variant="outlined" class="ml-1" data-test="plan-agent-drafted">{{ DRAFTED_BY_AGENT }}</v-chip>
            </td>
            <td>{{ item.description ?? '' }}</td>
            <td>{{ item.milestone_name ?? 'No milestone' }}</td>
            <td v-if="editable" class="text-right text-no-wrap">
              <v-btn v-if="item.source === 'agent'" size="x-small" variant="text" :disabled="busy" data-test="plan-item-confirm" @click="confirmItem('deliverable', item)">Confirm</v-btn>
              <v-btn size="x-small" variant="text" data-test="plan-item-edit" @click="startItem('deliverable', item)">Edit</v-btn>
              <v-btn size="x-small" variant="text" color="error" data-test="plan-item-delete" @click="askDelete('deliverable', item)">Delete</v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>

      <div class="d-flex align-center mb-1">
        <h3 class="text-body-1 font-weight-medium">Milestones and who does what</h3>
        <v-spacer />
        <v-btn v-if="editable" size="small" variant="text" data-test="plan-add-milestone" @click="startItem('milestone')">
          Add milestone
        </v-btn>
      </div>
      <p v-if="!milestones.length" class="text-body-2 mb-4">No milestones yet.</p>
      <v-table v-else density="compact" class="mb-4">
        <thead>
          <tr>
            <th>Target date</th>
            <th>Milestone</th>
            <th>Owner</th>
            <th>Contributors</th>
            <th v-if="editable"><span class="d-sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in milestones" :key="item.id" data-test="plan-milestone">
            <td>{{ item.target_date }}</td>
            <td>
              {{ item.name }}
              <v-chip v-if="item.source === 'agent'" size="x-small" color="secondary" variant="outlined" class="ml-1" data-test="plan-agent-drafted">{{ DRAFTED_BY_AGENT }}</v-chip>
              <div v-if="item.description" class="text-caption">{{ item.description }}</div>
            </td>
            <td>{{ ownerText(item) }}</td>
            <td>{{ item.contributors ?? '' }}</td>
            <td v-if="editable" class="text-right text-no-wrap">
              <v-btn v-if="item.source === 'agent'" size="x-small" variant="text" :disabled="busy" data-test="plan-item-confirm" @click="confirmItem('milestone', item)">Confirm</v-btn>
              <v-btn size="x-small" variant="text" data-test="plan-item-edit" @click="startItem('milestone', item)">Edit</v-btn>
              <v-btn size="x-small" variant="text" color="error" data-test="plan-item-delete" @click="askDelete('milestone', item)">Delete</v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>

      <div class="d-flex align-center mb-1">
        <h3 class="text-body-1 font-weight-medium">Estimate</h3>
        <v-spacer />
        <v-btn v-if="editable" size="small" variant="text" data-test="plan-add-line" @click="startItem('estimate_line')">
          Add estimate line
        </v-btn>
      </div>
      <v-table density="compact" class="mb-4">
        <tbody>
          <tr v-for="item in lines" :key="item.id" data-test="plan-line">
            <td>
              {{ item.description }}
              <v-chip v-if="item.source === 'agent'" size="x-small" color="secondary" variant="outlined" class="ml-1" data-test="plan-agent-drafted">{{ DRAFTED_BY_AGENT }}</v-chip>
            </td>
            <td>{{ label(item.category) }}</td>
            <td class="text-right">{{ item.amount }}</td>
            <td v-if="editable" class="text-right text-no-wrap">
              <v-btn v-if="item.source === 'agent'" size="x-small" variant="text" :disabled="busy" data-test="plan-item-confirm" @click="confirmItem('estimate_line', item)">Confirm</v-btn>
              <v-btn size="x-small" variant="text" data-test="plan-item-edit" @click="startItem('estimate_line', item)">Edit</v-btn>
              <v-btn size="x-small" variant="text" color="error" data-test="plan-item-delete" @click="askDelete('estimate_line', item)">Delete</v-btn>
            </td>
          </tr>
          <tr>
            <th scope="row" colspan="2">Lines</th>
            <td class="text-right" data-test="plan-lines-total">{{ plan.lines_total }}</td>
          </tr>
          <tr>
            <th scope="row" colspan="2">Contingency ({{ plan.contingency_pct }}%)</th>
            <td class="text-right">{{ plan.contingency_amount }}</td>
          </tr>
          <tr>
            <th scope="row" colspan="2">Total</th>
            <td class="text-right font-weight-bold" data-test="plan-total">{{ plan.total }}</td>
          </tr>
        </tbody>
      </v-table>
      <p class="text-body-2 mb-4" data-test="plan-budget-fit">
        {{ budgetFitText(plan.budget_fit) }}<template v-if="plan.budget_fit !== 'no_range'">
          ({{ budgetText(plan.budget_low, plan.budget_high) }})</template>.
      </p>

      <h3 class="text-body-1 font-weight-medium mb-1">Reporting plan</h3>
      <p class="text-body-2" data-test="plan-reporting">
        {{ reportingText(plan.report_period, plan.report_due_day) }}.
        Who hears what is on the
        <a href="#stakeholders" data-test="plan-stakeholders-link">stakeholder list</a>.
      </p>
    </template>
  </section>
</template>

<style scoped>
.pre {
  white-space: pre-line;
}
th[scope='row'] {
  width: 12rem;
  vertical-align: top;
}
</style>
