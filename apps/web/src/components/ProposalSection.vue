<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import { label } from '../lib/projects'
import {
  approvalText,
  approvedCopy,
  buttonsFor,
  draftText,
  eventText,
  SIMULATED_CLIENT,
  submitterText,
  flagsFor,
  type Proposal,
  proposalApi,
  type ProposalVersion,
  ruleText,
  STATUS_TEXT,
  TEXT_SECTIONS,
  type TextSection,
} from '../lib/proposals'
import { ApiError } from '../lib/risks'
import { useAuthStore } from '../stores/auth'

// The client proposal (item 5.2): the text sections with the checker's flags beside the text they
// concern, what the API copied from the plan (price, milestones, deliverables, risks shown), the
// status with the buttons the signed-in role may use, and the version history. Marking it sent
// records what a person did outside the product: nothing is sent from here. A project with no
// client has no proposal. "Draft with agent" asks the proposal agent (item 5.3) for the text; each
// section it wrote shows "Agent draft, not reviewed" until a person edits or confirms it. Each
// decision (submit, approve, return, mark sent, the client's answer, withdraw) emits `decided`, so
// a page listing it (the inbox) refreshes at once.
const props = defineProps<{ projectId: number; clientName: string | null }>()
const emit = defineEmits<{ decided: [message: string] }>()

const auth = useAuthStore()
const proposal = ref<Proposal | null>(null)
const versions = ref<ProposalVersion[]>([])
const missing = ref(false)
const error = ref('')
const actionError = ref('')
const actionDetails = ref<string[]>([])
const busy = ref(false)
const agentNote = ref('')
// Which form is open: editing the text, a decision, marking sent, the answer or withdrawing.
const open = ref<'edit' | 'approve' | 'return' | 'sent' | 'answer' | 'withdraw' | null>(null)
const draft = reactive<Record<TextSection, string>>({
  summary: '',
  scope_in: '',
  scope_out: '',
  assumptions: '',
  terms: '',
})
const reason = ref('')
const sent = reactive({ to: '', on: '', means: '' })
const answer = reactive({ value: 'accepted' as 'accepted' | 'declined', on: '', note: '' })

const buttons = computed(() =>
  proposal.value ? buttonsFor(proposal.value, auth.can, auth.user?.id ?? null) : null,
)
const approved = computed(() => approvedCopy(versions.value))

async function load(id: number) {
  error.value = ''
  missing.value = false
  proposal.value = null
  versions.value = []
  if (props.clientName === null) return
  try {
    proposal.value = await proposalApi.get(id)
    versions.value = await proposalApi.versions(id)
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 404) missing.value = true
    else error.value = (failure as Error).message
  }
}

watch(() => [props.projectId, props.clientName] as const, ([id]) => load(id), { immediate: true })

async function act(step: () => Promise<Proposal>, done?: string) {
  busy.value = true
  actionError.value = ''
  actionDetails.value = []
  agentNote.value = ''
  try {
    proposal.value = await step()
    versions.value = await proposalApi.versions(props.projectId)
    missing.value = false
    open.value = null
    reason.value = ''
    if (done) emit('decided', done)
  } catch (failure) {
    actionError.value = (failure as Error).message
    if (failure instanceof ApiError) actionDetails.value = failure.missing
  } finally {
    busy.value = false
  }
}

function startEdit() {
  if (!proposal.value) return
  for (const { key } of TEXT_SECTIONS) draft[key] = proposal.value[key] ?? ''
  open.value = 'edit'
}

function saveEdit() {
  const current = proposal.value
  if (!current) return
  const changes: Partial<Record<TextSection, string | null>> = {}
  for (const { key } of TEXT_SECTIONS) {
    const value = draft[key].trim() || null
    if (value !== current[key]) changes[key] = value
  }
  act(() => proposalApi.update(props.projectId, current.version, changes))
}

const today = () => new Date().toISOString().slice(0, 10)

function openForm(form: 'approve' | 'return' | 'sent' | 'answer' | 'withdraw') {
  reason.value = ''
  sent.to = ''
  sent.on = today()
  sent.means = ''
  answer.value = 'accepted'
  answer.on = today()
  answer.note = ''
  open.value = form
}

const id = () => props.projectId
const version = () => proposal.value?.version ?? 0
const create = () => act(() => proposalApi.create(id()))
const refresh = () => act(() => proposalApi.refresh(id(), version()))
const submit = () => act(() => proposalApi.submit(id(), version()), 'Proposal submitted.')
const decide = (kind: 'approve' | 'return') =>
  act(
    () =>
      kind === 'approve'
        ? proposalApi.approve(id(), version(), reason.value)
        : proposalApi.return(id(), version(), reason.value),
    kind === 'approve' ? 'Proposal approved.' : 'Proposal returned to draft.',
  )
const markSent = () =>
  act(
    () => proposalApi.sent(id(), version(), sent.to, sent.on, sent.means),
    'Proposal recorded as sent.',
  )
const recordAnswer = () =>
  act(
    () => proposalApi.answer(id(), version(), answer.value, answer.on, answer.note.trim() || null),
    "The client's answer is recorded.",
  )
const confirmSection = (section: TextSection) =>
  act(() => proposalApi.confirm(id(), version(), section))

async function draftWithAgent() {
  busy.value = true
  actionError.value = ''
  actionDetails.value = []
  agentNote.value = ''
  try {
    const result = await proposalApi.draft(id())
    agentNote.value = draftText(result)
    proposal.value = await proposalApi.get(id())
  } catch (failure) {
    actionError.value = (failure as Error).message
  } finally {
    busy.value = false
  }
}
const withdraw = () =>
  act(() => proposalApi.withdraw(id(), version(), reason.value), 'Proposal withdrawn.')
</script>

<template>
  <section class="mb-6" data-test="proposal">
    <h2 class="text-subtitle-1 mb-2">Proposal</h2>
    <p v-if="clientName === null" class="text-body-2" data-test="proposal-no-client">
      This project has no client, so it has no proposal.
    </p>
    <v-alert v-else-if="error" type="error" variant="tonal" data-test="proposal-error">
      {{ error }}
    </v-alert>
    <template v-else-if="missing">
      <p class="text-body-2 mb-2" data-test="proposal-none">
        No proposal yet. Creating one copies the price, milestones, deliverables and the risks
        ticked to show from the plan.
      </p>
      <v-btn
        v-if="auth.can('project:write')"
        color="primary"
        :loading="busy"
        data-test="proposal-create"
        @click="create"
      >
        Create the proposal
      </v-btn>
    </template>

    <template v-else-if="proposal && buttons">
      <div class="d-flex flex-wrap align-center ga-2 mb-2">
        <v-chip color="primary" variant="tonal" data-test="proposal-status">
          {{ STATUS_TEXT[proposal.status] }}
        </v-chip>
        <v-chip
          v-if="proposal.client_simulated"
          color="secondary"
          variant="outlined"
          data-test="proposal-simulated"
        >
          {{ SIMULATED_CLIENT }}
        </v-chip>
        <span v-if="proposal.latest_version" class="text-body-2" data-test="proposal-latest">
          Latest submitted version: {{ proposal.latest_version }}
        </span>
        <v-spacer />
        <v-btn
          v-if="approved"
          variant="text"
          :to="{ name: 'proposal-print', params: { id: projectId } }"
          data-test="proposal-print-link"
        >
          Print the approved version
        </v-btn>
      </div>
      <p v-if="approvalText(proposal)" class="text-body-2 mb-2" data-test="proposal-approval">
        {{ approvalText(proposal) }}
      </p>
      <v-alert
        v-if="proposal.plan_changed"
        type="info"
        variant="tonal"
        class="mb-2"
        data-test="proposal-plan-changed"
      >
        The plan has changed since this proposal was last refreshed from it.
      </v-alert>
      <v-alert
        v-if="proposal.flags.length"
        type="warning"
        variant="tonal"
        class="mb-4"
        data-test="proposal-flags"
      >
        The checker has {{ proposal.flags.length }} open
        {{ proposal.flags.length === 1 ? 'flag' : 'flags' }}; the proposal can't be submitted
        or marked sent until each is fixed. Each one is shown beside the section it concerns.
      </v-alert>
      <p v-else class="text-body-2 mb-4" data-test="proposal-clean">The checker has no flags.</p>

      <v-alert v-if="actionError" type="error" variant="tonal" class="mb-4" data-test="proposal-action-error">
        {{ actionError }}
        <ul v-if="actionDetails.length" class="pl-4">
          <li v-for="item in actionDetails" :key="item">{{ item }}</li>
        </ul>
      </v-alert>

      <v-alert v-if="agentNote" type="info" variant="tonal" class="mb-4" data-test="proposal-agent-note">
        {{ agentNote }}
      </v-alert>

      <div class="d-flex flex-wrap ga-2 mb-4" data-test="proposal-buttons">
        <v-btn v-if="buttons.edit" variant="outlined" data-test="proposal-edit" @click="startEdit">
          Edit the text
        </v-btn>
        <v-btn
          v-if="buttons.agent"
          variant="outlined"
          :loading="busy"
          data-test="proposal-agent-draft"
          @click="draftWithAgent"
        >
          Draft with agent
        </v-btn>
        <v-btn
          v-if="buttons.edit"
          variant="outlined"
          :loading="busy"
          data-test="proposal-refresh"
          @click="refresh"
        >
          Refresh from the plan
        </v-btn>
        <v-btn
          v-if="buttons.submit"
          color="primary"
          :loading="busy"
          data-test="proposal-submit"
          @click="submit"
        >
          Submit to the sponsor
        </v-btn>
        <v-btn v-if="buttons.decide" color="primary" data-test="proposal-approve" @click="openForm('approve')">
          Approve
        </v-btn>
        <v-btn v-if="buttons.decide" variant="outlined" data-test="proposal-return" @click="openForm('return')">
          Return
        </v-btn>
        <v-btn v-if="buttons.sent" color="primary" data-test="proposal-sent" @click="openForm('sent')">
          Mark sent
        </v-btn>
        <v-btn v-if="buttons.answer" color="primary" data-test="proposal-answer" @click="openForm('answer')">
          Record the client's answer
        </v-btn>
        <v-btn v-if="buttons.withdraw" variant="text" data-test="proposal-withdraw" @click="openForm('withdraw')">
          Withdraw
        </v-btn>
      </div>
      <p v-if="buttons.edit && proposal.status !== 'draft'" class="text-caption mb-4">
        Any edit sends the proposal back to draft; it then needs submitting and approving again.
      </p>

      <v-card v-if="open === 'edit'" variant="outlined" class="pa-4 mb-4" data-test="proposal-edit-form">
        <p class="text-caption mb-2" data-test="proposal-date-forms">
          The checker reads dates written as 2027-01-15, 15 January 2027 or January 15, 2027.
          Other forms, such as 15/01/2027, are not read, so write milestone dates in one of these.
        </p>
        <v-textarea
          v-for="section in TEXT_SECTIONS"
          :key="section.key"
          v-model="draft[section.key]"
          :label="section.title"
          rows="2"
          auto-grow
          counter="2000"
          :data-test="`proposal-field-${section.key}`"
        />
        <v-btn color="primary" :loading="busy" data-test="proposal-save" @click="saveEdit">Save</v-btn>
        <v-btn variant="text" @click="open = null">Cancel</v-btn>
      </v-card>

      <v-card
        v-if="open === 'approve' || open === 'return' || open === 'withdraw'"
        variant="outlined"
        class="pa-4 mb-4"
        data-test="proposal-reason-form"
      >
        <v-textarea
          v-model="reason"
          label="Reason (at least 20 characters)"
          rows="2"
          auto-grow
          data-test="proposal-reason"
        />
        <v-btn
          color="primary"
          :loading="busy"
          :disabled="reason.trim().length < 20"
          data-test="proposal-confirm"
          @click="open === 'withdraw' ? withdraw() : decide(open as 'approve' | 'return')"
        >
          {{ open === 'approve' ? 'Approve' : open === 'return' ? 'Return to draft' : 'Withdraw' }}
        </v-btn>
        <v-btn variant="text" @click="open = null">Cancel</v-btn>
      </v-card>

      <v-card v-if="open === 'sent'" variant="outlined" class="pa-4 mb-4" data-test="proposal-sent-form">
        <p class="text-body-2 mb-2">
          Record how a person sent the approved version. Nothing is sent from here.
        </p>
        <v-text-field v-model="sent.to" label="Sent to" data-test="proposal-sent-to" />
        <v-text-field v-model="sent.on" label="Sent on" type="date" data-test="proposal-sent-on" />
        <v-text-field v-model="sent.means" label="By what means" data-test="proposal-sent-means" />
        <v-btn
          color="primary"
          :loading="busy"
          :disabled="!sent.to.trim() || !sent.on || !sent.means.trim()"
          data-test="proposal-confirm"
          @click="markSent"
        >
          Record it as sent
        </v-btn>
        <v-btn variant="text" @click="open = null">Cancel</v-btn>
      </v-card>

      <v-card v-if="open === 'answer'" variant="outlined" class="pa-4 mb-4" data-test="proposal-answer-form">
        <v-radio-group v-model="answer.value" inline>
          <v-radio label="Accepted" value="accepted" />
          <v-radio label="Declined" value="declined" />
        </v-radio-group>
        <v-text-field v-model="answer.on" label="Answered on" type="date" />
        <v-textarea v-model="answer.note" label="Note" rows="2" auto-grow />
        <v-btn color="primary" :loading="busy" :disabled="!answer.on" data-test="proposal-confirm" @click="recordAnswer">
          Record the answer
        </v-btn>
        <v-btn variant="text" @click="open = null">Cancel</v-btn>
      </v-card>

      <v-table density="compact" class="mb-4">
        <tbody>
          <tr v-for="section in TEXT_SECTIONS" :key="section.key" :data-test="`proposal-section-${section.key}`">
            <th scope="row">{{ section.title }}</th>
            <td class="pre">
              <div v-if="proposal.sections?.[section.key]?.unreviewed" class="d-flex align-center ga-2 mb-1">
                <v-chip size="small" color="warning" variant="tonal" data-test="proposal-agent-chip">
                  Agent draft, not reviewed
                </v-chip>
                <v-btn
                  v-if="buttons.agent"
                  size="small"
                  variant="text"
                  :loading="busy"
                  :data-test="`proposal-confirm-${section.key}`"
                  @click="confirmSection(section.key)"
                >
                  Confirm
                </v-btn>
              </div>
              {{ proposal[section.key] ?? 'Not written yet' }}
              <div
                v-for="flag in flagsFor(proposal.flags, section.key)"
                :key="`${flag.rule}-${flag.text}`"
                class="text-warning text-caption"
                data-test="proposal-flag"
              >
                {{ ruleText(flag.rule) }}: “{{ flag.text }}”
              </div>
            </td>
          </tr>
          <tr data-test="proposal-section-price">
            <th scope="row">Price</th>
            <td>
              <span class="font-weight-bold" data-test="proposal-price">{{ proposal.price }}</span>
              <ul class="pl-4">
                <li v-for="line in proposal.estimate_lines" :key="line.description">
                  {{ line.description }} ({{ label(line.category) }}): {{ line.amount }}
                </li>
              </ul>
              <div
                v-for="flag in flagsFor(proposal.flags, 'price')"
                :key="flag.text"
                class="text-warning text-caption"
                data-test="proposal-flag"
              >
                {{ ruleText(flag.rule) }}: {{ flag.text }}
              </div>
            </td>
          </tr>
          <tr data-test="proposal-section-milestones">
            <th scope="row">Milestones and dates</th>
            <td>
              <ul class="pl-4">
                <li v-for="item in proposal.milestones" :key="`${item.name}-${item.target_date}`">
                  {{ item.target_date }}: {{ item.name }}
                </li>
              </ul>
              <div
                v-for="flag in flagsFor(proposal.flags, 'milestones')"
                :key="`${flag.rule}-${flag.text}`"
                class="text-warning text-caption"
                data-test="proposal-flag"
              >
                {{ ruleText(flag.rule) }}: {{ flag.text }}
              </div>
            </td>
          </tr>
          <tr data-test="proposal-section-deliverables">
            <th scope="row">Deliverables</th>
            <td>
              <ul class="pl-4">
                <li v-for="item in proposal.deliverables" :key="item.name">
                  {{ item.name }}<template v-if="item.milestone"> (by {{ item.milestone }})</template>
                </li>
              </ul>
              <div
                v-for="flag in flagsFor(proposal.flags, 'deliverables')"
                :key="`${flag.rule}-${flag.text}`"
                class="text-warning text-caption"
                data-test="proposal-flag"
              >
                {{ ruleText(flag.rule) }}: {{ flag.text }}
              </div>
            </td>
          </tr>
          <tr data-test="proposal-section-risks">
            <th scope="row">Risks the client should know</th>
            <td>
              <p v-if="!proposal.risks.length" class="text-body-2">
                None. Tick “Show in the proposal” on a risk in the risk register to show it here.
              </p>
              <ul v-else class="pl-4">
                <li v-for="risk in proposal.risks" :key="risk.risk_id" data-test="proposal-risk">
                  {{ risk.title }}: {{ label(risk.response) }}. {{ risk.response_plan }}
                </li>
              </ul>
              <div
                v-for="flag in flagsFor(proposal.flags, 'risks')"
                :key="`${flag.rule}-${flag.text}`"
                class="text-warning text-caption"
                data-test="proposal-flag"
              >
                {{ ruleText(flag.rule) }}: {{ flag.text }}
              </div>
            </td>
          </tr>
        </tbody>
      </v-table>

      <h3 class="text-body-1 font-weight-medium mb-1">Version history</h3>
      <p v-if="!versions.length" class="text-body-2" data-test="proposal-no-versions">
        Not submitted yet.
      </p>
      <v-list v-else density="compact" data-test="proposal-versions">
        <v-list-item v-for="item in versions" :key="item.id" data-test="proposal-version">
          <v-list-item-title>
            Version {{ item.number }}, submitted by
            {{ submitterText(item) }} on
            {{ item.submitted_at.slice(0, 10) }} (price {{ item.content.price }})
          </v-list-item-title>
          <ul class="pl-4 text-body-2">
            <li v-for="event in item.events" :key="event.id" data-test="proposal-event">
              {{ eventText(event) }}
            </li>
          </ul>
        </v-list-item>
      </v-list>
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
