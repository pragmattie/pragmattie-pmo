<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { label } from '../lib/projects'
import { approvedCopy, proposalApi, type ProposalVersion, TEXT_SECTIONS } from '../lib/proposals'

// A print view of the approved version of a project's proposal (item 5.2): the frozen copy the
// sponsor approved, as the client sees it. Printing is done by the browser; nothing is sent.
const route = useRoute()
const projectId = computed(() => Number(route.params.id))

const copy = ref<ProposalVersion | null>(null)
const loaded = ref(false)
const error = ref('')

async function load(id: number) {
  loaded.value = false
  error.value = ''
  copy.value = null
  try {
    copy.value = approvedCopy(await proposalApi.versions(id))
  } catch (failure) {
    error.value = (failure as Error).message
  }
  loaded.value = true
}

watch(projectId, load, { immediate: true })

function print() {
  window.print()
}
</script>

<template>
  <v-container class="py-8 print-view" data-test="proposal-print">
    <div class="d-flex align-center ga-2 mb-4 no-print">
      <v-btn variant="text" :to="{ name: 'project', params: { id: projectId } }">
        Back to the project
      </v-btn>
      <v-spacer />
      <v-btn v-if="copy" color="primary" data-test="proposal-print-button" @click="print">
        Print
      </v-btn>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" data-test="load-error">{{ error }}</v-alert>
    <p v-else-if="loaded && !copy" class="text-body-1" data-test="proposal-print-none">
      No version of this proposal has been approved yet.
    </p>
    <article v-else-if="copy">
      <h1 class="text-h4 text-primary mb-1" data-test="proposal-print-title">
        Proposal: {{ copy.content.project_name }}
      </h1>
      <p class="text-body-2 mb-4">
        For {{ copy.content.client_name }}. Version {{ copy.number }}.
      </p>
      <template v-for="section in TEXT_SECTIONS" :key="section.key">
        <template v-if="copy.content[section.key]">
          <h2 class="text-subtitle-1 mt-4">{{ section.title }}</h2>
          <p class="pre">{{ copy.content[section.key] }}</p>
        </template>
      </template>
      <h2 class="text-subtitle-1 mt-4">Deliverables</h2>
      <ul class="pl-4">
        <li v-for="item in copy.content.deliverables" :key="item.name">
          {{ item.name }}<template v-if="item.description">: {{ item.description }}</template>
        </li>
      </ul>
      <h2 class="text-subtitle-1 mt-4">Milestones and dates</h2>
      <ul class="pl-4">
        <li v-for="item in copy.content.milestones" :key="`${item.name}-${item.target_date}`">
          {{ item.target_date }}: {{ item.name }}
        </li>
      </ul>
      <h2 class="text-subtitle-1 mt-4">Price</h2>
      <p class="font-weight-bold" data-test="proposal-print-price">{{ copy.content.price }}</p>
      <template v-if="copy.content.risks.length">
        <h2 class="text-subtitle-1 mt-4">Risks you should know about</h2>
        <ul class="pl-4">
          <li v-for="risk in copy.content.risks" :key="risk.risk_id" data-test="proposal-print-risk">
            {{ risk.title }}: {{ label(risk.response) }}. {{ risk.response_plan }}
          </li>
        </ul>
      </template>
    </article>
  </v-container>
</template>

<style scoped>
.pre {
  white-space: pre-line;
}
@media print {
  .no-print {
    display: none;
  }
}
</style>
