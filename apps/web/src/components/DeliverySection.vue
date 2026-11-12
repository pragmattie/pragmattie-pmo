<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  daysLateText,
  type DeliverableProgress,
  type DeliverableStatus,
  type Delivery,
  deliveryApi,
  isOpen,
  MAX_REACHED_NOTE,
  MAX_SPEND_NOTE,
  type MilestoneProgress,
  SETTABLE_STATUSES,
  STATUS_TEXT,
} from '../lib/delivery'
import { moneyText } from '../lib/intake'
import { useAuthStore } from '../stores/auth'

// Delivery (item 7.2): for a project in delivery, each milestone with its baseline target, reached
// date and days late (working days, computed by the API); each deliverable with its status; and
// spend to date with its history and the baseline total beside it. A project manager marks
// milestones reached, moves deliverables short of accepted and adds spend entries; a correction is
// a new entry naming the newest one. The buttons show only for whoever may use them; the API
// checks every rule again. Nothing here changes the plan: that is a change request.
const props = defineProps<{ projectId: number }>()

const auth = useAuthStore()
const delivery = ref<Delivery | null>(null)
const loadError = ref('')
const message = ref('')
const busy = ref(false)
const reaching = ref<MilestoneProgress | null>(null)
const reachedOn = ref('')
const reachedNote = ref('')
const spending = ref<'new' | 'correct' | null>(null)
const amount = ref('')
const asOf = ref('')
const spendNote = ref('')

const mayWrite = computed(
  () => !!delivery.value && isOpen(delivery.value) && auth.can('project:write'),
)
const spendReady = computed(
  () => /^\d+(\.\d{1,2})?$/.test(amount.value.trim()) && !!asOf.value && !!spendNote.value.trim(),
)

async function load() {
  loadError.value = ''
  try {
    delivery.value = await deliveryApi.get(props.projectId)
  } catch (error) {
    delivery.value = null
    loadError.value = (error as Error).message
  }
}

watch(() => props.projectId, load, { immediate: true })

async function act(work: () => Promise<Delivery>, done: string) {
  busy.value = true
  message.value = ''
  try {
    delivery.value = await work()
    message.value = done
    return true
  } catch (error) {
    message.value = (error as Error).message
    await load()
    return false
  } finally {
    busy.value = false
  }
}

function startReach(milestone: MilestoneProgress) {
  reaching.value = milestone
  reachedOn.value = delivery.value?.today ?? ''
  reachedNote.value = ''
}

async function reach() {
  const milestone = reaching.value
  if (!milestone || !reachedOn.value) return
  const note = reachedNote.value.trim() || null
  if (
    await act(
      () => deliveryApi.reach(props.projectId, milestone, reachedOn.value, note),
      `${milestone.name} marked reached.`,
    )
  ) {
    reaching.value = null
  }
}

function clearReached(milestone: MilestoneProgress) {
  act(
    () => deliveryApi.reach(props.projectId, milestone, null, null),
    `${milestone.name} is no longer marked reached.`,
  )
}

function setStatus(deliverable: DeliverableProgress, status: DeliverableStatus) {
  act(
    () => deliveryApi.setStatus(props.projectId, deliverable, status),
    `${deliverable.name}: ${STATUS_TEXT[status]}.`,
  )
}

function startSpend(kind: 'new' | 'correct') {
  spending.value = kind
  const current = delivery.value?.current_spend
  amount.value = kind === 'correct' && current ? current.amount : ''
  asOf.value = kind === 'correct' && current ? current.as_of : (delivery.value?.today ?? '')
  spendNote.value = ''
}

async function spend() {
  if (!spendReady.value || !spending.value) return
  const corrects = spending.value === 'correct' ? delivery.value?.current_spend?.id : undefined
  if (
    await act(
      () =>
        deliveryApi.spend(props.projectId, {
          amount: amount.value.trim(),
          as_of: asOf.value,
          note: spendNote.value.trim(),
          ...(corrects ? { supersedes_id: corrects } : {}),
        }),
      corrects ? 'Correction recorded as a new entry.' : 'Spend to date recorded.',
    )
  ) {
    spending.value = null
  }
}
</script>

<template>
  <section id="delivery" class="mb-6" data-test="delivery">
    <h2 class="text-subtitle-1 mb-2">Delivery</h2>
    <v-alert v-if="loadError" type="error" variant="tonal" data-test="delivery-error">
      {{ loadError }}
    </v-alert>
    <template v-if="delivery">
      <v-alert v-if="message" type="info" variant="tonal" class="mb-2" data-test="delivery-message">
        {{ message }}
      </v-alert>
      <p class="text-caption text-medium-emphasis mb-2">
        Days late are working days against the target in baseline
        {{ delivery.baseline_number ?? 'none' }}. Dates, scope and cost change only by a change
        request.
      </p>

      <h3 class="text-subtitle-2 mb-1">Milestones</h3>
      <p v-if="!delivery.milestones.length" class="text-body-2">No milestones.</p>
      <v-table v-else density="compact" class="mb-4" data-test="delivery-milestones">
        <thead>
          <tr>
            <th>Milestone</th>
            <th>Baseline target</th>
            <th>Reached</th>
            <th>Days late</th>
            <th v-if="mayWrite"><span class="d-sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="milestone in delivery.milestones" :key="milestone.id" data-test="delivery-milestone">
            <td>
              {{ milestone.name }}
              <div
                v-if="milestone.target_date && milestone.plan_date !== milestone.target_date"
                class="text-caption"
              >
                Plan date now {{ milestone.plan_date }}
              </div>
            </td>
            <td data-test="milestone-target">{{ milestone.target_date ?? 'No baseline target' }}</td>
            <td data-test="milestone-reached">
              {{ milestone.reached_on ?? 'Not yet' }}
              <div v-if="milestone.reached_note" class="text-caption">{{ milestone.reached_note }}</div>
            </td>
            <td data-test="milestone-days-late">{{ daysLateText(milestone) }}</td>
            <td v-if="mayWrite">
              <v-btn
                v-if="!milestone.reached_on"
                size="small"
                variant="text"
                :disabled="busy"
                data-test="milestone-reach"
                @click="startReach(milestone)"
              >
                Mark reached
              </v-btn>
              <v-btn
                v-else
                size="small"
                variant="text"
                :disabled="busy"
                data-test="milestone-clear"
                @click="clearReached(milestone)"
              >
                Clear
              </v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>
      <v-card v-if="reaching" variant="outlined" class="mb-4 pa-4" data-test="reach-form">
        <p class="text-body-2 mb-2">Mark {{ reaching.name }} reached</p>
        <v-text-field
          v-model="reachedOn"
          label="Reached on"
          type="date"
          :max="delivery.today"
          data-test="reach-date"
        />
        <v-textarea
          v-model="reachedNote"
          label="Note (optional)"
          rows="2"
          auto-grow
          :maxlength="MAX_REACHED_NOTE"
          data-test="reach-note"
        />
        <v-btn color="primary" :disabled="busy || !reachedOn" data-test="reach-save" @click="reach">
          Save
        </v-btn>
        <v-btn variant="text" :disabled="busy" @click="reaching = null">Cancel</v-btn>
      </v-card>

      <h3 class="text-subtitle-2 mb-1">Deliverables</h3>
      <p v-if="!delivery.deliverables.length" class="text-body-2">No deliverables.</p>
      <v-table v-else density="compact" class="mb-4" data-test="delivery-deliverables">
        <thead>
          <tr>
            <th>Deliverable</th>
            <th>Status</th>
            <th v-if="mayWrite"><span class="d-sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="deliverable in delivery.deliverables" :key="deliverable.id" data-test="delivery-deliverable">
            <td>{{ deliverable.name }}</td>
            <td data-test="deliverable-status">{{ STATUS_TEXT[deliverable.status] }}</td>
            <td v-if="mayWrite">
              <template v-if="deliverable.status !== 'accepted'">
                <v-btn
                  v-for="status in SETTABLE_STATUSES.filter((s) => s !== deliverable.status)"
                  :key="status"
                  size="small"
                  variant="text"
                  :disabled="busy"
                  :data-test="`deliverable-set-${status}`"
                  @click="setStatus(deliverable, status)"
                >
                  {{ STATUS_TEXT[status] }}
                </v-btn>
              </template>
            </td>
          </tr>
        </tbody>
      </v-table>

      <h3 class="text-subtitle-2 mb-1">Spend to date</h3>
      <p class="text-body-2" data-test="spend-current">
        <template v-if="delivery.current_spend">
          {{ moneyText(delivery.current_spend.amount) }} as of {{ delivery.current_spend.as_of }}
        </template>
        <template v-else>None recorded yet</template>
        <span data-test="spend-baseline">
          against a baseline total of
          {{ delivery.baseline_total ? moneyText(delivery.baseline_total) : 'none' }}
        </span>
      </p>
      <div v-if="mayWrite && !spending" class="mb-2">
        <v-btn size="small" variant="tonal" :disabled="busy" data-test="spend-add" @click="startSpend('new')">
          Record spend to date
        </v-btn>
        <v-btn
          v-if="delivery.current_spend"
          size="small"
          variant="text"
          :disabled="busy"
          data-test="spend-correct"
          @click="startSpend('correct')"
        >
          Correct the newest entry
        </v-btn>
      </div>
      <v-card v-if="spending" variant="outlined" class="mb-4 pa-4" data-test="spend-form">
        <p v-if="spending === 'correct'" class="text-body-2 mb-2">
          A correction is a new entry; the one it corrects stays in the history.
        </p>
        <v-text-field v-model="amount" label="Amount" data-test="spend-amount" />
        <v-text-field v-model="asOf" label="True as of" type="date" data-test="spend-as-of" />
        <v-text-field
          v-model="spendNote"
          label="Source (for example: finance report, October)"
          :maxlength="MAX_SPEND_NOTE"
          data-test="spend-note"
        />
        <v-btn color="primary" :disabled="busy || !spendReady" data-test="spend-save" @click="spend">
          Save
        </v-btn>
        <v-btn variant="text" :disabled="busy" @click="spending = null">Cancel</v-btn>
      </v-card>
      <v-table v-if="delivery.spend.items.length" density="compact" data-test="spend-history">
        <thead>
          <tr>
            <th>Amount</th>
            <th>As of</th>
            <th>Source</th>
            <th>Recorded by</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in delivery.spend.items" :key="entry.id" data-test="spend-entry">
            <td>{{ moneyText(entry.amount) }}</td>
            <td>{{ entry.as_of }}</td>
            <td>
              {{ entry.note }}
              <span v-if="entry.supersedes_id" class="text-caption" data-test="spend-corrects">
                (corrects entry {{ entry.supersedes_id }})
              </span>
            </td>
            <td>{{ entry.created_by_name ?? 'Unknown' }}</td>
          </tr>
        </tbody>
      </v-table>
    </template>
  </section>
</template>
