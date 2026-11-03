<script setup lang="ts">
import { computed } from 'vue'

import {
  AGENT_PROPOSAL,
  bandColor,
  canCloseRisk,
  changedFields,
  type HistoryEntry,
  isAgentProposal,
  label,
  type Risk,
  scoreText,
} from '../lib/risks'

// Every field of one risk and its history from the audit trail. The edit, close, open, reject and
// accept buttons are only rendered for a role that may use them; the API refuses everyone else
// anyway. `canAccept` is worked out by the page, which knows each band's owner.
const props = defineProps<{
  risk: Risk | null
  history: HistoryEntry[]
  historyTotal: number
  ownerName: (id: number | null) => string
  canWrite: boolean
  canAccept: boolean
  busy: boolean
  message: string
  messageType: 'error' | 'warning' | 'success'
}>()
const emit = defineEmits<{
  close: []
  edit: []
  closeRisk: []
  accept: []
  openProposed: []
  reject: []
  moreHistory: []
}>()

const proposed = computed(() => props.canWrite && props.risk?.status === 'proposed')

const fields = computed(() => {
  const risk = props.risk
  if (!risk) return []
  const level = (value: number | null) => (value === null ? '—' : String(value))
  return [
    ['Kind', label(risk.kind)],
    ['Category', label(risk.category)],
    ['Status', label(risk.status)],
    ['Cause', risk.cause],
    ['Effect', risk.effect],
    ['Objectives affected', risk.objectives.map(label).join(', ')],
    ['Probability', level(risk.probability)],
    ['Impact', level(risk.impact)],
    ['Score', scoreText(risk)],
    ['Band', risk.band ?? '—'],
    ['Response', label(risk.response)],
    ['Response plan', risk.response_plan],
    ['Owner', props.ownerName(risk.owner_user_id)],
    ['Escalated to', risk.escalated_to ? `${label(risk.escalated_to)} (${risk.escalated_band})` : '—'],
    ['Review date', risk.review_date ?? '—'],
    ['Needs a person’s judgement', risk.needs_judgement ? 'Yes' : 'No'],
    ['Source', label(risk.source)],
    ['Version', String(risk.version)],
    ['Added', new Date(risk.created_at).toLocaleString()],
    ['Last changed', new Date(risk.updated_at).toLocaleString()],
  ]
})

function describe(entry: HistoryEntry) {
  const changed = changedFields(entry).map(label)
  return changed.length ? `Changed: ${changed.join(', ')}` : ''
}
</script>

<template>
  <div v-if="risk" class="pa-4" data-test="risk-drawer">
    <div class="d-flex align-start mb-2">
      <h2 class="text-h6 flex-grow-1" data-test="drawer-title">{{ risk.title }}</h2>
      <v-btn
        icon="$close"
        variant="text"
        size="small"
        aria-label="Close details"
        data-test="drawer-close"
        @click="emit('close')"
      />
    </div>
    <v-chip
      v-if="risk.band"
      :color="bandColor(risk.band)"
      variant="flat"
      size="small"
      class="mb-3"
    >
      {{ risk.band }}
    </v-chip>
    <v-chip
      v-if="isAgentProposal(risk)"
      color="secondary"
      variant="tonal"
      size="small"
      class="mb-3 ml-1"
      data-test="drawer-agent-chip"
    >
      {{ AGENT_PROPOSAL }}
    </v-chip>
    <v-alert
      v-if="message"
      :type="messageType"
      variant="tonal"
      density="compact"
      class="mb-3"
      data-test="drawer-message"
    >
      {{ message }}
    </v-alert>
    <v-alert
      v-if="risk.escalated_to"
      type="info"
      variant="tonal"
      density="compact"
      class="mb-3"
      data-test="escalated"
    >
      Escalated to the {{ label(risk.escalated_to).toLowerCase() }} ({{ risk.escalated_band }}
      band).
    </v-alert>
    <div v-if="canWrite || canAccept" class="d-flex flex-wrap ga-2 mb-4">
      <v-btn
        v-if="canAccept"
        color="secondary"
        variant="flat"
        size="small"
        :loading="busy"
        data-test="accept-risk"
        @click="emit('accept')"
      >
        Accept
      </v-btn>
      <template v-if="proposed">
        <v-btn
          color="primary"
          variant="flat"
          size="small"
          :loading="busy"
          data-test="open-risk"
          @click="emit('openProposed')"
        >
          Open
        </v-btn>
        <v-btn
          variant="outlined"
          size="small"
          :loading="busy"
          data-test="reject-risk"
          @click="emit('reject')"
        >
          Reject
        </v-btn>
      </template>
      <v-btn
        v-if="canWrite"
        color="primary"
        variant="flat"
        size="small"
        data-test="edit-risk"
        @click="emit('edit')"
      >
        Edit
      </v-btn>
      <v-btn
        v-if="canWrite && canCloseRisk(risk)"
        variant="outlined"
        size="small"
        :loading="busy"
        data-test="close-risk"
        @click="emit('closeRisk')"
      >
        Close risk
      </v-btn>
    </div>
    <dl class="risk-fields">
      <template v-for="[name, value] in fields" :key="name">
        <dt class="text-caption text-medium-emphasis">{{ name }}</dt>
        <dd class="text-body-2 mb-2" :data-test="`field-${name}`">{{ value }}</dd>
      </template>
    </dl>
    <h3 class="text-subtitle-1 mt-4 mb-2">History</h3>
    <p v-if="!history.length" class="text-body-2">No recorded changes.</p>
    <v-list density="compact" data-test="history">
      <v-list-item v-for="entry in history" :key="entry.id" class="px-0">
        <v-list-item-title class="text-body-2">
          {{ label(entry.action) }} (version {{ entry.version }}) by
          {{ entry.actor_name ?? 'the system' }}
        </v-list-item-title>
        <v-list-item-subtitle>
          {{ new Date(entry.created_at).toLocaleString() }}
          <span v-if="describe(entry)">. {{ describe(entry) }}</span>
        </v-list-item-subtitle>
      </v-list-item>
    </v-list>
    <v-btn
      v-if="history.length < historyTotal"
      variant="text"
      size="small"
      data-test="more-history"
      @click="emit('moreHistory')"
    >
      Show older changes
    </v-btn>
  </div>
</template>

<style scoped>
.risk-fields dd {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
