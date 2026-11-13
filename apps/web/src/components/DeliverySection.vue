<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  type AcceptanceAnswer,
  answererText,
  canAnswer,
  canOffer,
  daysLateText,
  type DeliverableProgress,
  type DeliverableStatus,
  type Delivery,
  deliveryApi,
  isOpen,
  MAX_ACCEPTANCE_TEXT,
  MAX_MEANS,
  MAX_OFFERED_TO,
  MAX_REACHED_NOTE,
  MAX_SPEND_NOTE,
  type MilestoneProgress,
  roundText,
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
//
// Acceptance (item 7.6): Offer for acceptance on a deliverable ready for it (to whom, when, by what
// means; nothing is sent from here), Record the answer on an offer waiting for one, and each
// deliverable's history, every round. A simulated client's answers say "Simulated client". With no
// client, the sponsor records the answer. `changed` tells the page the acceptance gate may move.
const props = defineProps<{ projectId: number }>()
const emit = defineEmits<{ changed: [] }>()

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
const offering = ref<DeliverableProgress | null>(null)
const offeredTo = ref('')
const offeredOn = ref('')
const means = ref('')
const offerNote = ref('')
const answering = ref<DeliverableProgress | null>(null)
const answerValue = ref<AcceptanceAnswer>('accepted')
const answeredOn = ref('')
const missingText = ref('')
const answerNote = ref('')

const mayWrite = computed(
  () => !!delivery.value && isOpen(delivery.value) && auth.can('project:write'),
)
const offerReady = computed(
  () => !!offeredTo.value.trim() && !!offeredOn.value && !!means.value.trim(),
)
const answerReady = computed(
  () =>
    !!answeredOn.value && (answerValue.value === 'accepted' || !!missingText.value.trim()),
)
const answerChoices = [
  { title: 'Accepted', value: 'accepted' },
  { title: 'Not accepted', value: 'not_accepted' },
]

function mayOffer(deliverable: DeliverableProgress): boolean {
  return !!delivery.value && canOffer(delivery.value, deliverable, auth.can('project:write'))
}

function mayAnswer(deliverable: DeliverableProgress): boolean {
  return (
    !!delivery.value &&
    canAnswer(delivery.value, deliverable, auth.user, (action) => auth.can(action))
  )
}

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
    emit('changed')
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

function startOffer(deliverable: DeliverableProgress) {
  answering.value = null
  offering.value = deliverable
  offeredTo.value = ''
  offeredOn.value = delivery.value?.today ?? ''
  means.value = ''
  offerNote.value = ''
}

async function offer() {
  const deliverable = offering.value
  if (!deliverable || !offerReady.value) return
  const note = offerNote.value.trim()
  if (
    await act(
      () =>
        deliveryApi.offer(props.projectId, deliverable, {
          offered_to: offeredTo.value.trim(),
          offered_on: offeredOn.value,
          means: means.value.trim(),
          ...(note ? { note } : {}),
        }),
      `${deliverable.name} offered for acceptance. Nothing was sent from here.`,
    )
  ) {
    offering.value = null
  }
}

function startAnswer(deliverable: DeliverableProgress) {
  offering.value = null
  answering.value = deliverable
  answerValue.value = 'accepted'
  answeredOn.value = delivery.value?.today ?? ''
  missingText.value = ''
  answerNote.value = ''
}

async function answer() {
  const deliverable = answering.value
  if (!deliverable || !answerReady.value) return
  const note = answerNote.value.trim()
  const accepted = answerValue.value === 'accepted'
  if (
    await act(
      () =>
        deliveryApi.answer(props.projectId, deliverable, {
          answer: answerValue.value,
          answered_on: answeredOn.value,
          ...(accepted ? {} : { missing: missingText.value.trim() }),
          ...(note ? { note } : {}),
        }),
      accepted
        ? `${deliverable.name} accepted.`
        : `${deliverable.name} not accepted: it is back in progress.`,
    )
  ) {
    answering.value = null
  }
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
      <p class="text-caption text-medium-emphasis mb-1" data-test="acceptance-answerer">
        {{ answererText(delivery) }} A deliverable is accepted only through a recorded answer.
      </p>
      <p v-if="!delivery.deliverables.length" class="text-body-2">No deliverables.</p>
      <v-table v-else density="compact" class="mb-4" data-test="delivery-deliverables">
        <thead>
          <tr>
            <th>Deliverable</th>
            <th>Status</th>
            <th>Acceptance</th>
            <th><span class="d-sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="deliverable in delivery.deliverables" :key="deliverable.id" data-test="delivery-deliverable">
            <td>{{ deliverable.name }}</td>
            <td data-test="deliverable-status">{{ STATUS_TEXT[deliverable.status] }}</td>
            <td data-test="deliverable-acceptance">
              <span v-if="!deliverable.acceptance.length" class="text-caption">Not offered yet</span>
              <ul v-else class="pl-4 text-caption" data-test="acceptance-history">
                <li
                  v-for="round in deliverable.acceptance"
                  :key="round.id"
                  data-test="acceptance-round"
                >
                  {{ roundText(round) }}
                  <v-chip
                    v-if="round.source === 'simulated'"
                    size="x-small"
                    class="ml-1"
                    data-test="acceptance-simulated"
                  >
                    Simulated
                  </v-chip>
                  <span v-if="round.note"> ({{ round.note }})</span>
                </li>
              </ul>
              <span
                v-if="deliverable.awaiting_answer"
                class="text-caption"
                data-test="acceptance-waiting"
              >
                Waiting for the answer.
              </span>
            </td>
            <td>
              <v-btn
                v-if="mayOffer(deliverable)"
                size="small"
                variant="tonal"
                :disabled="busy"
                data-test="deliverable-offer"
                @click="startOffer(deliverable)"
              >
                Offer for acceptance
              </v-btn>
              <v-btn
                v-if="mayAnswer(deliverable)"
                size="small"
                variant="tonal"
                :disabled="busy"
                data-test="deliverable-answer"
                @click="startAnswer(deliverable)"
              >
                Record the answer
              </v-btn>
              <template v-if="mayWrite && deliverable.status !== 'accepted'">
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
      <v-card v-if="offering" variant="outlined" class="mb-4 pa-4" data-test="offer-form">
        <p class="text-body-2 mb-2">
          Offer {{ offering.name }} for acceptance. Record how it was offered: nothing is sent from
          here.
        </p>
        <v-text-field
          v-model="offeredTo"
          label="Offered to"
          :maxlength="MAX_OFFERED_TO"
          data-test="offer-to"
        />
        <v-text-field
          v-model="offeredOn"
          label="Offered on"
          type="date"
          :max="delivery.today"
          data-test="offer-on"
        />
        <v-text-field
          v-model="means"
          label="By (for example: email, meeting)"
          :maxlength="MAX_MEANS"
          data-test="offer-means"
        />
        <v-textarea
          v-model="offerNote"
          label="Note (optional)"
          rows="2"
          auto-grow
          :maxlength="MAX_ACCEPTANCE_TEXT"
          data-test="offer-note"
        />
        <v-btn color="primary" :disabled="busy || !offerReady" data-test="offer-save" @click="offer">
          Offer
        </v-btn>
        <v-btn variant="text" :disabled="busy" @click="offering = null">Cancel</v-btn>
      </v-card>
      <v-card v-if="answering" variant="outlined" class="mb-4 pa-4" data-test="answer-form">
        <p class="text-body-2 mb-2">Record the answer for {{ answering.name }}</p>
        <v-radio-group v-model="answerValue" inline data-test="answer-choice">
          <v-radio
            v-for="choice in answerChoices"
            :key="choice.value"
            :label="choice.title"
            :value="choice.value"
            :data-test="`answer-${choice.value}`"
          />
        </v-radio-group>
        <v-text-field
          v-model="answeredOn"
          label="Answer came on"
          type="date"
          :max="delivery.today"
          data-test="answer-on"
        />
        <v-textarea
          v-if="answerValue === 'not_accepted'"
          v-model="missingText"
          label="What is missing"
          rows="2"
          auto-grow
          :maxlength="MAX_ACCEPTANCE_TEXT"
          data-test="answer-missing"
        />
        <v-textarea
          v-model="answerNote"
          label="Note (optional)"
          rows="2"
          auto-grow
          :maxlength="MAX_ACCEPTANCE_TEXT"
          data-test="answer-note"
        />
        <v-btn color="primary" :disabled="busy || !answerReady" data-test="answer-save" @click="answer">
          Save the answer
        </v-btn>
        <v-btn variant="text" :disabled="busy" @click="answering = null">Cancel</v-btn>
      </v-card>

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
