<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import InboxDecision from '../components/InboxDecision.vue'
import {
  decidesAnything,
  dueText,
  EMPTY,
  type InboxEntry,
  inboxApi,
  MIN_HOLD_REASON,
  mayHold,
  NO_DECISIONS,
  parseUtc,
  roleText,
  stepTitle,
  waitsOnText,
} from '../lib/inbox'
import { useAuthStore } from '../stores/auth'
import { useInboxStore } from '../stores/inbox'

// "Needs you": every decision waiting on the signed-in person, oldest first. Each shows why it is
// here, when it arrived, the facts and the recommendation, and opens its existing form in place.
// A window action shows when it happens and, for its role only, a Hold button.
const auth = useAuthStore()
const counter = useInboxStore()
const items = ref<InboxEntry[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const message = ref('')
const open = ref<string[]>([])

const decides = computed(() => decidesAnything(auth.can))

const holding = ref<InboxEntry | null>(null)
const holdReason = ref('')
const holdError = ref('')
const holdBusy = ref(false)
const holdOpen = computed({
  get: () => holding.value !== null,
  set: (value: boolean) => {
    if (!value) holding.value = null
  },
})

const key = (entry: InboxEntry) => `${entry.kind}-${entry.id}`

function failed(err: unknown): string {
  return err instanceof Error ? err.message : 'Something went wrong. Please try again.'
}

async function load(more = false) {
  loading.value = true
  error.value = ''
  try {
    const page = await inboxApi.list(more ? items.value.length : 0)
    items.value = more ? [...items.value, ...page.items] : page.items
    total.value = page.total
  } catch (err) {
    error.value = failed(err)
  } finally {
    loading.value = false
  }
}

async function decided(text: string) {
  message.value = text
  await Promise.all([load(), counter.refresh()])
}

function startHold(entry: InboxEntry) {
  holding.value = entry
  holdReason.value = ''
  holdError.value = ''
}

async function hold() {
  const entry = holding.value
  if (!entry || entry.action_id === null) return
  holdBusy.value = true
  holdError.value = ''
  try {
    await inboxApi.hold(entry.action_id, holdReason.value.trim())
    holding.value = null
    await decided('Held: the step is now waiting on you.')
  } catch (err) {
    holdError.value = failed(err)
  } finally {
    holdBusy.value = false
  }
}

onMounted(() => {
  if (decides.value) load()
})
</script>

<template>
  <v-container class="py-8">
    <div class="d-flex align-center mb-4">
      <h1 class="text-h4 text-primary">Needs you</h1>
      <v-spacer />
      <v-btn v-if="decides" variant="text" :loading="loading" data-test="inbox-refresh" @click="load()">
        Refresh
      </v-btn>
    </div>

    <v-alert v-if="!decides" type="info" variant="tonal" data-test="inbox-no-decisions">
      {{ NO_DECISIONS }}
    </v-alert>

    <template v-else>
      <v-alert v-if="error" type="error" variant="tonal" class="mb-4" data-test="inbox-error">
        {{ error }}
      </v-alert>
      <v-alert
        v-if="message"
        type="success"
        variant="tonal"
        closable
        class="mb-4"
        data-test="inbox-message"
        @click:close="message = ''"
      >
        {{ message }}
      </v-alert>
      <p v-if="!loading && !error && !items.length" class="text-body-1" data-test="inbox-empty">
        {{ EMPTY }}
      </p>

      <v-expansion-panels v-model="open" multiple>
        <v-expansion-panel
          v-for="entry in items"
          :key="key(entry)"
          :value="key(entry)"
          :data-test="`inbox-item-${entry.kind}-${entry.id}`"
        >
          <v-expansion-panel-title>
            <div>
              <div class="text-subtitle-1" data-test="inbox-title">
                {{ stepTitle(entry.step) }}: {{ entry.subject_title }}
              </div>
              <div class="text-caption text-medium-emphasis">
                <span v-if="entry.project_name">{{ entry.project_name }} · </span>
                waiting on {{ waitsOnText(entry) }} · arrived
                {{ parseUtc(entry.created_at).toLocaleString() }}
              </div>
            </div>
          </v-expansion-panel-title>
          <v-expansion-panel-text>
            <p class="text-body-2 mb-1" data-test="inbox-reason">Why: {{ entry.reason }}</p>
            <p v-if="entry.handoff" class="text-body-2 mb-1" data-test="inbox-handoff">
              Sent to a person: {{ entry.handoff }}
            </p>
            <div v-if="entry.kind === 'window'" class="d-flex align-center ga-2 my-2">
              <v-chip color="accent" variant="flat" data-test="inbox-due">{{ dueText(entry) }}</v-chip>
              <v-btn
                v-if="mayHold(entry, auth.user?.role)"
                color="primary"
                variant="outlined"
                size="small"
                data-test="inbox-hold"
                @click="startHold(entry)"
              >
                Hold
              </v-btn>
              <span v-else class="text-caption" data-test="inbox-hold-who">
                Only the {{ roleText(entry.who) }} may hold it.
              </span>
            </div>

            <v-list density="compact" class="my-2" data-test="inbox-facts">
              <v-list-item v-for="fact in entry.facts" :key="fact.key" class="px-0">
                <span
                  v-if="fact.met !== null"
                  :class="fact.met ? 'text-success' : 'text-error'"
                  class="mr-1"
                  :aria-label="fact.met ? 'met' : 'not met'"
                  >{{ fact.met ? '✓' : '✗' }}</span
                >
                <strong>{{ fact.label }}:</strong> {{ fact.value }}
              </v-list-item>
            </v-list>
            <v-alert
              type="info"
              variant="tonal"
              density="compact"
              class="mb-4"
              data-test="inbox-recommendation"
            >
              Recommendation (from the facts above): {{ entry.recommendation }}
            </v-alert>

            <InboxDecision
              v-if="open.includes(key(entry)) && entry.kind === 'item'"
              :entry="entry"
              @decided="decided"
            />
          </v-expansion-panel-text>
        </v-expansion-panel>
      </v-expansion-panels>

      <v-btn
        v-if="items.length < total"
        variant="text"
        class="mt-4"
        :loading="loading"
        data-test="inbox-more"
        @click="load(true)"
      >
        Show more
      </v-btn>
    </template>

    <v-dialog v-model="holdOpen" max-width="480">
      <v-card data-test="inbox-hold-dialog">
        <v-card-title>Hold this step?</v-card-title>
        <v-card-text>
          <p class="text-body-2 mb-3">
            The agent won't do it; it comes to your inbox instead. Your name and reason are
            recorded.
          </p>
          <v-textarea
            v-model="holdReason"
            label="Why hold it"
            :hint="`At least ${MIN_HOLD_REASON} characters`"
            persistent-hint
            rows="3"
            maxlength="4000"
            data-test="inbox-hold-reason"
          />
          <v-alert v-if="holdError" type="error" variant="tonal" class="mt-3" data-test="inbox-hold-error">
            {{ holdError }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="holding = null">Cancel</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :disabled="holdReason.trim().length < MIN_HOLD_REASON"
            :loading="holdBusy"
            data-test="inbox-hold-yes"
            @click="hold"
          >
            Hold
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-container>
</template>
