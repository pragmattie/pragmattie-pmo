<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'

import {
  type Board,
  type BoardFilters,
  boardApi,
  cardLink,
  HIGHLIGHT_MS,
  healthColor,
  healthText,
  moneyText,
  movedKeys,
  NOTHING_WAITING,
  POLL_MS,
  risksText,
  states,
  WAITING_CHOICES,
} from '../lib/board'
import { bandColor } from '../lib/risks'

// The portfolio board: every request and project as a card in a column by stage, with what each
// is waiting on and whether an agent or a person has it. It is read-only: a card opens the
// request's or project's page, where a person acts. It refreshes every 15 seconds and briefly
// highlights a card that changed column or waiting state.
const board = ref<Board | null>(null)
const error = ref('')
const loading = ref(false)
const highlighted = ref<Set<string>>(new Set())
const filters = ref<BoardFilters>({
  client_id: null,
  project_manager: null,
  waiting: null,
  show_stopped: false,
})

let timer: ReturnType<typeof setInterval> | null = null
let last = new Map<string, string>()
// Each load takes the next number; a response for an older one is dropped, so a poll that was
// already on its way can't put back the board for a filter that has since changed.
let latest = 0

function failed(err: unknown): string {
  return err instanceof Error ? err.message : 'Something went wrong. Please try again.'
}

async function load() {
  const asked = ++latest
  loading.value = true
  try {
    const found = await boardApi.get(filters.value)
    if (asked !== latest) return
    const moved = movedKeys(last, found)
    last = states(found)
    board.value = found
    error.value = ''
    if (moved.length) {
      highlighted.value = new Set([...highlighted.value, ...moved])
      setTimeout(() => {
        const left = new Set(highlighted.value)
        for (const key of moved) left.delete(key)
        highlighted.value = left
      }, HIGHLIGHT_MS)
    }
  } catch (err) {
    if (asked === latest) error.value = failed(err)
  } finally {
    if (asked === latest) loading.value = false
  }
}

// A new filter is a new board: nothing on it has "moved".
watch(
  filters,
  () => {
    last = new Map()
    load()
  },
  { deep: true },
)

onMounted(() => {
  load()
  timer = setInterval(load, POLL_MS)
})

onUnmounted(() => {
  if (timer !== null) clearInterval(timer)
})
</script>

<template>
  <v-container fluid class="py-6 px-4">
    <div class="d-flex align-center mb-4">
      <h1 class="text-h4 text-primary">Board</h1>
      <v-spacer />
      <v-btn variant="text" :loading="loading" data-test="board-refresh" @click="load()">
        Refresh
      </v-btn>
    </div>

    <v-alert v-if="error" type="error" variant="tonal" class="mb-4" data-test="board-error">
      {{ error }}
    </v-alert>

    <template v-if="board">
      <v-sheet class="summary d-flex flex-wrap ga-4 pa-3 mb-4" rounded border data-test="board-summary">
        <div v-for="column in board.columns" :key="column.id" class="stat">
          <div class="text-caption text-medium-emphasis">{{ column.title }}</div>
          <div class="text-h6" :data-test="`summary-count-${column.id}`">
            {{ board.summary.counts[column.id] ?? 0 }}
          </div>
        </div>
        <v-divider vertical />
        <div class="stat">
          <div class="text-caption text-medium-emphasis">Plan value in planning</div>
          <div class="text-h6" data-test="summary-planning-value">
            {{ board.summary.planning_value }}
          </div>
        </div>
        <div class="stat">
          <div class="text-caption text-medium-emphasis">Plan value in delivery</div>
          <div class="text-h6" data-test="summary-delivery-value">
            {{ board.summary.delivery_value }}
          </div>
        </div>
        <div class="stat">
          <div class="text-caption text-medium-emphasis">Open Medium and High risks</div>
          <div class="text-h6" data-test="summary-risks">
            {{ board.summary.open_medium_high_risks }}
          </div>
        </div>
        <div class="stat">
          <div class="text-caption text-medium-emphasis">Decisions waiting on people</div>
          <div class="text-h6" data-test="summary-decisions">
            {{ board.summary.decisions_waiting }}
          </div>
        </div>
      </v-sheet>

      <div class="d-flex flex-wrap ga-3 mb-4 align-center">
        <v-select
          v-model="filters.client_id"
          :items="[{ title: 'Every client', value: null }, ...board.clients.map((c) => ({ title: c.name, value: c.id }))]"
          label="Client"
          density="compact"
          hide-details
          class="filter"
          data-test="filter-client"
        />
        <v-select
          v-model="filters.project_manager"
          :items="[{ title: 'Every project manager', value: null }, ...board.project_managers.map((p) => ({ title: p, value: p }))]"
          label="Project manager"
          density="compact"
          hide-details
          class="filter"
          data-test="filter-pm"
        />
        <v-select
          v-model="filters.waiting"
          :items="WAITING_CHOICES"
          label="Waiting on"
          density="compact"
          hide-details
          class="filter"
          data-test="filter-waiting"
        />
        <v-switch
          v-model="filters.show_stopped"
          label="Show declined, withdrawn and cancelled"
          density="compact"
          hide-details
          color="primary"
          data-test="filter-stopped"
        />
      </div>

      <div class="columns" data-test="board-columns">
        <section
          v-for="column in board.columns"
          :key="column.id"
          class="column"
          :data-test="`column-${column.id}`"
        >
          <h2 class="text-subtitle-1 font-weight-bold mb-2">
            {{ column.title }}
            <span class="text-medium-emphasis">({{ column.cards.length }})</span>
          </h2>
          <v-card
            v-for="card in column.cards"
            :key="card.key"
            :to="cardLink(card)"
            class="board-card mb-3"
            :class="[
              card.held_by ? `held-${card.held_by}` : 'held-none',
              { moved: highlighted.has(card.key), stopped: card.stopped },
            ]"
            variant="outlined"
            :data-test="`card-${card.key}`"
            :data-held="card.held_by ?? 'none'"
          >
            <v-card-item>
              <v-card-title class="text-body-1 font-weight-bold text-wrap" data-test="card-name">
                {{ card.name }}
              </v-card-title>
              <v-card-subtitle data-test="card-client">
                {{ card.client_name ?? 'Internal' }}
                <span v-if="card.kind === 'request'"> · request</span>
              </v-card-subtitle>
            </v-card-item>
            <v-card-text class="pt-0">
              <div class="text-body-2" data-test="card-pm">
                Project manager: {{ card.project_manager ?? 'not named yet' }}
              </div>
              <div class="d-flex flex-wrap ga-1 my-2">
                <v-chip
                  v-if="card.health"
                  size="small"
                  :color="healthColor(card.health)"
                  variant="flat"
                  data-test="card-health"
                >
                  {{ healthText(card.health) }}
                </v-chip>
                <v-chip v-if="card.proposal_status" size="small" data-test="card-proposal">
                  Proposal: {{ card.proposal_status }}
                </v-chip>
                <v-chip v-if="card.stopped" size="small" color="grey" data-test="card-stopped">
                  Stopped
                </v-chip>
                <v-chip
                  v-if="card.simulated"
                  size="small"
                  color="secondary"
                  variant="outlined"
                  data-test="card-simulated"
                >
                  Simulated
                </v-chip>
              </div>
              <div class="text-body-2 d-flex align-center ga-1" data-test="card-risks">
                <v-chip
                  v-if="card.top_risk_band"
                  size="x-small"
                  :color="bandColor(card.top_risk_band)"
                  variant="flat"
                >
                  {{ card.top_risk_band }}
                </v-chip>
                {{ risksText(card) }}
              </div>
              <div class="text-body-2" data-test="card-money">{{ moneyText(card) }}</div>
              <div class="waiting mt-2" data-test="card-waiting">
                <div v-if="!card.waiting.length" class="text-medium-emphasis">
                  {{ NOTHING_WAITING }}
                </div>
                <div
                  v-for="w in card.waiting"
                  :key="`${w.step}-${w.subject_type}-${w.subject_id}`"
                  class="text-body-2"
                  :class="w.holder === 'person' ? 'font-weight-bold' : 'font-italic'"
                  :data-test="`waiting-${w.holder}`"
                >
                  <span class="holder">{{ w.holder === 'person' ? 'Person' : 'Agent' }}:</span>
                  {{ w.text }}
                  <span v-if="w.simulated" class="text-secondary"> (simulated)</span>
                </div>
              </div>
            </v-card-text>
          </v-card>
        </section>
      </div>
    </template>
  </v-container>
</template>

<style scoped>
.columns {
  display: grid;
  grid-template-columns: repeat(7, minmax(230px, 1fr));
  gap: 12px;
  overflow-x: auto;
  padding-bottom: 8px;
}
.column {
  min-width: 0;
}
.stat {
  min-width: 110px;
}
.filter {
  min-width: 200px;
  max-width: 260px;
}
.board-card {
  border-left-width: 6px;
  transition: box-shadow 0.3s, background-color 0.3s;
}
/* An agent has it: a dashed teal edge. A person has it: a solid gold edge. */
.held-agent {
  border-left-style: dashed;
  border-left-color: rgb(var(--v-theme-secondary));
}
.held-person {
  border-left-style: solid;
  border-left-color: rgb(var(--v-theme-accent));
}
.moved {
  background-color: rgba(232, 179, 90, 0.25);
  box-shadow: 0 0 0 3px rgb(var(--v-theme-accent));
}
.stopped {
  opacity: 0.6;
}
.holder {
  text-transform: uppercase;
  font-size: 0.7rem;
  letter-spacing: 0.05em;
}
@media (min-width: 2000px) {
  .board-card {
    font-size: 1.1rem;
  }
}
</style>
