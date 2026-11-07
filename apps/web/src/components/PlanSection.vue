<script setup lang="ts">
import { ref, watch } from 'vue'

import {
  budgetFitText,
  type Deliverable,
  DRAFTED_BY_AGENT,
  type EstimateLine,
  type Milestone,
  ownerText,
  type Plan,
  planApi,
  reportingText,
} from '../lib/plans'
import { budgetText, label } from '../lib/projects'

// The project's plan (item 5.1), read only: what is missing first, then scope and deliverables,
// milestones in date order with who does what, the estimate and its total against the brief's
// budget range, and the reporting plan. The API computes the total and the missing list. Every
// page of each list is read. An item the planner agent drafted (item 6.5) is marked as such.
const props = defineProps<{ projectId: number }>()

const plan = ref<Plan | null>(null)
const milestones = ref<Milestone[]>([])
const deliverables = ref<Deliverable[]>([])
const lines = ref<EstimateLine[]>([])
const error = ref('')

async function load(id: number) {
  error.value = ''
  plan.value = null
  try {
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
  } catch (failure) {
    error.value = (failure as Error).message
  }
}

watch(() => props.projectId, load, { immediate: true })
</script>

<template>
  <section class="mb-6" data-test="plan">
    <h2 class="text-subtitle-1 mb-2">Plan</h2>
    <v-alert v-if="error" type="info" variant="tonal" data-test="no-plan">{{ error }}</v-alert>

    <template v-else-if="plan">
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

      <h3 class="text-body-1 font-weight-medium mb-1">Scope</h3>
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

      <h3 class="text-body-1 font-weight-medium mb-1">Deliverables</h3>
      <p v-if="!deliverables.length" class="text-body-2 mb-4">No deliverables yet.</p>
      <v-table v-else density="compact" class="mb-4">
        <thead>
          <tr>
            <th>Deliverable</th>
            <th>Description</th>
            <th>Delivered by</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in deliverables" :key="item.id" data-test="plan-deliverable">
            <td>
              {{ item.name }}
              <div v-if="item.source === 'agent'" class="text-caption text-medium-emphasis" data-test="plan-agent-drafted">{{ DRAFTED_BY_AGENT }}</div>
            </td>
            <td>{{ item.description ?? '' }}</td>
            <td>{{ item.milestone_name ?? 'No milestone' }}</td>
          </tr>
        </tbody>
      </v-table>

      <h3 class="text-body-1 font-weight-medium mb-1">Milestones and who does what</h3>
      <p v-if="!milestones.length" class="text-body-2 mb-4">No milestones yet.</p>
      <v-table v-else density="compact" class="mb-4">
        <thead>
          <tr>
            <th>Target date</th>
            <th>Milestone</th>
            <th>Owner</th>
            <th>Contributors</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in milestones" :key="item.id" data-test="plan-milestone">
            <td>{{ item.target_date }}</td>
            <td>
              {{ item.name }}
              <div v-if="item.description" class="text-caption">{{ item.description }}</div>
              <div v-if="item.source === 'agent'" class="text-caption text-medium-emphasis" data-test="plan-agent-drafted">{{ DRAFTED_BY_AGENT }}</div>
            </td>
            <td>{{ ownerText(item) }}</td>
            <td>{{ item.contributors ?? '' }}</td>
          </tr>
        </tbody>
      </v-table>

      <h3 class="text-body-1 font-weight-medium mb-1">Estimate</h3>
      <v-table density="compact" class="mb-4">
        <tbody>
          <tr v-for="item in lines" :key="item.id" data-test="plan-line">
            <td>
              {{ item.description }}
              <div v-if="item.source === 'agent'" class="text-caption text-medium-emphasis" data-test="plan-agent-drafted">{{ DRAFTED_BY_AGENT }}</div>
            </td>
            <td>{{ label(item.category) }}</td>
            <td class="text-right">{{ item.amount }}</td>
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
