<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  canDecideChange,
  canWithdrawChange,
  type ChangeForm,
  type ChangeLog,
  type ChangeOutcome,
  type ChangeRequest,
  changeApi,
  changeSetFrom,
  deciderText,
  effectText,
  emptyChangeForm,
  isEmptyChangeSet,
  MAX_REASON,
  MAX_TITLE,
  MIN_DECISION_REASON,
  STATUS_TEXT,
} from '../lib/changes'
import type { Baseline } from '../lib/gates'
import { moneyText } from '../lib/intake'
import { ApiError } from '../lib/risks'
import { useAuthStore } from '../stores/auth'

// Changes (item 5.5): once a project is in delivery, raise a change request against the current
// baseline, see the effect and who decides it (both computed by the API), submit, withdraw or
// decide it, and read the change log. The buttons show only for whoever may use them; the API
// checks every rule again. An approved change makes the next baseline: `decided` asks the page
// to reload.
const props = defineProps<{ projectId: number; baseline: Baseline | null }>()
const emit = defineEmits<{ decided: [message: string] }>()

const auth = useAuthStore()
const log = ref<ChangeLog | null>(null)
const loadError = ref('')
const message = ref('')
const raising = ref(false)
const form = ref<ChangeForm>(emptyChangeForm())
const title = ref('')
const reason = ref('')
const formError = ref('')
const busy = ref(false)
const deciding = ref<{ change: ChangeRequest; outcome: ChangeOutcome } | null>(null)
const decisionReason = ref('')
const decisionError = ref('')

const pmUserId = computed(() => log.value?.pm_user_id ?? null)
const open = computed(
  () => log.value?.project_stage === 'delivery' && log.value.project_status !== 'cancelled',
)
const mayRaise = computed(() => open.value && auth.can('project:write') && !!props.baseline)
const changeSet = computed(() => changeSetFrom(form.value))
const raiseReady = computed(
  () => !!title.value.trim() && !!reason.value.trim() && !isEmptyChangeSet(changeSet.value),
)
const milestones = computed(() => props.baseline?.plan.milestones ?? [])
const deliverables = computed(() => props.baseline?.plan.deliverables ?? [])
const lines = computed(() =>
  (props.baseline?.plan.estimate_lines ?? []).map((l) => ({
    id: l.id,
    title: `${l.description} (${moneyText(l.amount)})`,
  })),
)

async function load() {
  loadError.value = ''
  try {
    log.value = await changeApi.list(props.projectId)
  } catch (error) {
    log.value = null
    loadError.value = (error as Error).message
  }
}

watch(() => [props.projectId, props.baseline?.number] as const, load, { immediate: true })

function startRaise() {
  form.value = emptyChangeForm()
  title.value = ''
  reason.value = ''
  formError.value = ''
  raising.value = true
}

async function act(work: () => Promise<unknown>, done: string) {
  busy.value = true
  message.value = ''
  try {
    await work()
    message.value = done
    await load()
  } catch (error) {
    message.value = (error as Error).message
    await load()
  } finally {
    busy.value = false
  }
}

async function raise() {
  if (!raiseReady.value) return
  busy.value = true
  formError.value = ''
  try {
    await changeApi.raise(props.projectId, {
      title: title.value.trim(),
      reason: reason.value.trim(),
      changes: changeSet.value,
    })
    raising.value = false
    message.value = 'Change request raised as a draft. Check its effect, then submit it.'
    await load()
  } catch (error) {
    formError.value = (error as Error).message
  } finally {
    busy.value = false
  }
}

function ask(change: ChangeRequest, outcome: ChangeOutcome) {
  deciding.value = { change, outcome }
  decisionReason.value = ''
  decisionError.value = ''
}

async function decide() {
  const current = deciding.value
  if (!current || decisionReason.value.trim().length < MIN_DECISION_REASON) return
  busy.value = true
  decisionError.value = ''
  try {
    const decided = await changeApi.decide(
      props.projectId,
      current.change,
      current.outcome,
      decisionReason.value.trim(),
    )
    deciding.value = null
    if (decided.status === 'approved') {
      emit('decided', `Change approved: the plan is now baseline ${decided.baseline_number}.`)
    } else {
      message.value = 'Change rejected; nothing in the plan changed.'
    }
    await load()
  } catch (error) {
    if (error instanceof ApiError && (error.status === 403 || error.status === 409)) {
      deciding.value = null
      message.value = error.message
      await load()
    } else {
      decisionError.value = (error as Error).message
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="mb-6" data-test="changes">
    <h2 class="text-subtitle-1 mb-2">Changes</h2>
    <v-alert v-if="loadError" type="info" variant="tonal" class="mb-2" data-test="changes-error">
      {{ loadError }}
    </v-alert>
    <template v-if="log">
      <p class="text-caption text-medium-emphasis mb-2">
        In delivery, scope, dates and cost change only through a change request. Each is measured
        against the current baseline<span v-if="log.current_baseline_number">
          ({{ log.current_baseline_number }})</span
        >, and the changes policy says who decides it.
      </p>
      <v-alert
        v-if="log.proposal_out_of_date"
        type="warning"
        variant="tonal"
        class="mb-2"
        data-test="proposal-out-of-date"
      >
        The proposal no longer matches the plan (baseline {{ log.current_baseline_number }}). Refresh
        it from the plan and have it approved again.
      </v-alert>
      <v-alert v-if="message" type="info" variant="tonal" class="mb-2" closable data-test="changes-message">
        {{ message }}
      </v-alert>

      <v-btn
        v-if="mayRaise && !raising"
        color="primary"
        variant="outlined"
        class="mb-2"
        data-test="change-raise"
        @click="startRaise"
      >
        Raise a change request
      </v-btn>

      <v-card v-if="raising" variant="outlined" class="mb-4 pa-4" data-test="change-form">
        <v-alert v-if="formError" type="error" variant="tonal" class="mb-2" data-test="change-form-error">
          {{ formError }}
        </v-alert>
        <v-text-field v-model="title" label="Title" :maxlength="MAX_TITLE" data-test="change-title" />
        <v-textarea v-model="reason" label="Why" rows="2" auto-grow :maxlength="MAX_REASON" data-test="change-reason" />
        <p class="text-caption text-medium-emphasis mb-2">Leave anything that doesn't change empty.</p>
        <v-row dense>
          <v-col cols="12" md="6">
            <v-text-field v-model="form.addLineDescription" label="Add an estimate line" data-test="change-add-line" />
          </v-col>
          <v-col cols="6" md="3">
            <v-select
              v-model="form.addLineCategory"
              :items="['people', 'software', 'services', 'other']"
              label="Category"
            />
          </v-col>
          <v-col cols="6" md="3">
            <v-text-field v-model="form.addLineAmount" label="Amount" data-test="change-add-amount" />
          </v-col>
          <v-col cols="12" md="6">
            <v-select
              v-model="form.changeLine"
              :items="lines"
              item-value="id"
              label="Change a line's amount"
              clearable
            />
          </v-col>
          <v-col cols="12" md="6">
            <v-text-field v-model="form.changeLineAmount" label="New amount" />
          </v-col>
          <v-col cols="12" md="6">
            <v-select v-model="form.removeLines" :items="lines" item-value="id" label="Remove lines" multiple />
          </v-col>
          <v-col cols="12" md="6">
            <v-text-field v-model="form.contingency" label="Contingency %" type="number" />
          </v-col>
          <v-col cols="12" md="6">
            <v-select
              v-model="form.moveMilestone"
              :items="milestones"
              item-title="name"
              item-value="id"
              label="Move a milestone"
              clearable
            />
          </v-col>
          <v-col cols="12" md="6">
            <v-text-field v-model="form.moveTo" label="New date" type="date" />
          </v-col>
          <v-col cols="12" md="6">
            <v-text-field v-model="form.addDeliverable" label="Add a deliverable" />
          </v-col>
          <v-col cols="12" md="6">
            <v-select
              v-model="form.addDeliverableMilestone"
              :items="milestones"
              item-title="name"
              item-value="id"
              label="Its milestone"
              clearable
            />
          </v-col>
          <v-col cols="12">
            <v-select
              v-model="form.removeDeliverables"
              :items="deliverables"
              item-title="name"
              item-value="id"
              label="Remove deliverables"
              multiple
            />
          </v-col>
          <v-col cols="12" md="6">
            <v-textarea v-model="form.scopeIn" label="New scope in" rows="2" auto-grow />
          </v-col>
          <v-col cols="12" md="6">
            <v-textarea v-model="form.scopeOut" label="New scope out" rows="2" auto-grow />
          </v-col>
        </v-row>
        <div class="d-flex ga-2">
          <v-spacer />
          <v-btn variant="text" @click="raising = false">Cancel</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :disabled="!raiseReady"
            :loading="busy"
            data-test="change-save"
            @click="raise"
          >
            Raise as draft
          </v-btn>
        </div>
      </v-card>

      <p v-if="!log.items.length" class="text-body-2 text-medium-emphasis" data-test="changes-empty">
        No change requests yet.
      </p>
      <v-list v-else density="compact" class="py-0" data-test="change-log">
        <v-list-item
          v-for="change in log.items"
          :key="change.id"
          class="px-0 mb-2"
          :data-test="`change-${change.id}`"
        >
          <div class="d-flex align-center ga-2">
            <strong class="text-body-2">{{ change.title }}</strong>
            <v-chip size="x-small" data-test="change-status">{{ STATUS_TEXT[change.status] }}</v-chip>
          </div>
          <div class="text-body-2" data-test="change-effect">
            {{ effectText(change.effect) }}. Against baseline {{ change.measured_baseline_number }}:
            {{ moneyText(change.effect.baseline_total) }} to {{ moneyText(change.effect.new_total) }}.
          </div>
          <div class="text-caption" data-test="change-decider">Decided by {{ deciderText(change) }}</div>
          <div class="text-caption text-medium-emphasis">
            Raised by {{ change.raised_by_name ?? `User ${change.raised_by}` }}: {{ change.reason }}
          </div>
          <div v-if="change.decided_by" class="text-caption" data-test="change-decision">
            {{ STATUS_TEXT[change.status] }} by
            {{ change.decided_by_name ?? `User ${change.decided_by}` }}<span v-if="change.decided_at">
              on {{ new Date(change.decided_at).toLocaleDateString() }}</span
            >: {{ change.decision_reason }}
            <span v-if="change.baseline_number" data-test="change-baseline">
              Made baseline {{ change.baseline_number }}.
            </span>
          </div>
          <div class="d-flex flex-wrap ga-2 mt-1">
            <v-btn
              v-if="open && change.status === 'draft' && auth.can('project:write')"
              size="small"
              color="primary"
              variant="flat"
              :loading="busy"
              data-test="change-submit"
              @click="act(() => changeApi.submit(projectId, change), 'Change request submitted.')"
            >
              Submit
            </v-btn>
            <v-btn
              v-if="
                open &&
                change.status === 'submitted' &&
                auth.can('project:write') &&
                change.measured_baseline_number !== log.current_baseline_number
              "
              size="small"
              variant="outlined"
              :loading="busy"
              data-test="change-resubmit"
              @click="act(() => changeApi.submit(projectId, change), 'Measured again against the current baseline.')"
            >
              Resubmit
            </v-btn>
            <template v-if="open && canDecideChange(change, auth.user, pmUserId)">
              <v-btn size="small" color="success" variant="flat" data-test="change-approve" @click="ask(change, 'approve')">
                Approve
              </v-btn>
              <v-btn size="small" color="error" variant="outlined" data-test="change-reject" @click="ask(change, 'reject')">
                Reject
              </v-btn>
            </template>
            <v-btn
              v-if="open && canWithdrawChange(change, auth.user, pmUserId)"
              size="small"
              variant="text"
              :loading="busy"
              data-test="change-withdraw"
              @click="act(() => changeApi.withdraw(projectId, change), 'Change request withdrawn.')"
            >
              Withdraw
            </v-btn>
          </div>
        </v-list-item>
      </v-list>
    </template>

    <v-dialog :model-value="deciding !== null" max-width="520" @update:model-value="deciding = null">
      <v-card v-if="deciding" data-test="change-dialog">
        <v-card-title>{{ deciding.outcome === 'approve' ? 'Approve' : 'Reject' }} this change?</v-card-title>
        <v-card-text>
          <v-alert v-if="decisionError" type="error" variant="tonal" class="mb-2">{{ decisionError }}</v-alert>
          <p class="text-body-2 mb-2">
            {{
              deciding.outcome === 'approve'
                ? 'The change is applied to the plan, which becomes the next baseline.'
                : 'Nothing in the plan changes.'
            }}
          </p>
          <v-textarea
            v-model="decisionReason"
            label="Reason"
            rows="3"
            auto-grow
            :maxlength="MAX_REASON"
            :hint="`At least ${MIN_DECISION_REASON} characters`"
            persistent-hint
            data-test="change-decision-reason"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="deciding = null">Cancel</v-btn>
          <v-btn
            :color="deciding.outcome === 'approve' ? 'success' : 'error'"
            variant="flat"
            :disabled="decisionReason.trim().length < MIN_DECISION_REASON"
            :loading="busy"
            data-test="change-confirm"
            @click="decide"
          >
            {{ deciding.outcome === 'approve' ? 'Approve' : 'Reject' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </section>
</template>
