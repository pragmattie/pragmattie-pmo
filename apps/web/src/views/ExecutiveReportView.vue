<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  type ExecutiveReport,
  milestonesNotShown,
  milestoneText,
  money,
  RATED,
  RATING_COLOR,
  RATING_TEXT,
  statusReportApi,
} from '../lib/statusReports'

// The executive view (item 7.5): a published report on one page, for a sponsor to read in a
// minute: the overall and four ratings, the summary, the decisions needed, the three milestones
// that matter most (late first, then due next), spend against the baseline and the open High
// risks. Everything comes from the figures the report froze when it was published; nothing is
// read from live records. It prints on one page (the print stylesheet below), and the detailed
// report is one click away. Opening it tells the API, which keeps only the project sponsor's
// first opening.
const route = useRoute()
const projectId = computed(() => Number(route.params.id))
const reportId = computed(() => Number(route.params.reportId))

const view = ref<ExecutiveReport | null>(null)
const error = ref('')

async function load() {
  error.value = ''
  view.value = null
  try {
    view.value = await statusReportApi.executive(projectId.value, reportId.value)
  } catch (failure) {
    error.value = (failure as Error).message
    return
  }
  // A read receipt is best effort: the page shows whether or not it was recorded.
  statusReportApi.opened(projectId.value, reportId.value).catch(() => undefined)
}

watch([projectId, reportId], load, { immediate: true })

const notShown = computed(() => (view.value ? milestonesNotShown(view.value) : 0))
const detailed = computed(() => ({
  name: 'status-reports',
  params: { id: String(projectId.value) },
  query: { report: String(reportId.value) },
}))

function print() {
  window.print()
}
</script>

<template>
  <v-container class="py-6 executive-page print-one-page" data-test="executive">
    <div class="d-flex flex-wrap align-center ga-2 mb-4 no-print">
      <v-btn variant="text" :to="detailed" data-test="executive-detailed">Detailed report</v-btn>
      <v-spacer />
      <v-btn v-if="view" color="primary" data-test="executive-print" @click="print">Print</v-btn>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" data-test="load-error">{{ error }}</v-alert>
    <article v-else-if="view">
      <header class="mb-3">
        <h1 class="text-h5 text-primary" data-test="executive-title">
          {{ view.project_name }}: status report
        </h1>
        <p class="text-body-2 text-medium-emphasis" data-test="executive-meta">
          {{ view.period_start }} to {{ view.period_end }}. Published
          {{ new Date(view.published_at).toLocaleDateString() }}<span v-if="view.published_by_name">
            by {{ view.published_by_name }}</span
          >. Figures as read {{ new Date(view.figures_at).toLocaleDateString() }}.
        </p>
      </header>

      <section class="mb-3" data-test="executive-ratings">
        <div class="d-flex flex-wrap ga-2">
          <v-chip
            v-for="item in RATED"
            :key="item.name"
            :color="RATING_COLOR[view.ratings[item.name].rating]"
            :variant="item.name === 'overall' ? 'flat' : 'tonal'"
            :data-test="`executive-rating-${item.name}`"
          >
            {{ item.title }}: {{ RATING_TEXT[view.ratings[item.name].rating] }}
          </v-chip>
        </div>
      </section>

      <section class="mb-3" data-test="executive-summary">
        <h2 class="text-subtitle-1">Summary</h2>
        <p class="pre">{{ view.summary || 'No summary was written.' }}</p>
      </section>

      <section class="mb-3" data-test="executive-decisions">
        <h2 class="text-subtitle-1">Decisions needed from the sponsor</h2>
        <p class="pre">{{ view.decisions || 'None.' }}</p>
      </section>

      <section class="mb-3" data-test="executive-milestones">
        <h2 class="text-subtitle-1">Milestones that matter most</h2>
        <ul v-if="view.milestones.length" class="pl-4">
          <li v-for="m in view.milestones" :key="m.id" data-test="executive-milestone">
            <strong>{{ m.name }}</strong>: {{ milestoneText(m) }}
          </li>
        </ul>
        <p v-else class="text-body-2">No milestone was late or due next period.</p>
        <p v-if="notShown" class="text-caption" data-test="executive-milestones-more">
          {{ notShown }} more late or due next in the detailed report.
        </p>
        <p
          v-else-if="view.milestones.length"
          class="text-caption"
          data-test="executive-milestones-all"
        >
          The report holds no more late or due-next milestones.
        </p>
      </section>

      <section class="mb-3" data-test="executive-spend">
        <h2 class="text-subtitle-1">Spend against the baseline</h2>
        <p>
          {{ money(view.spend.spend_to_date) }} spent of a
          {{ money(view.spend.baseline_total) }} baseline<span v-if="view.spend.spent_pct">
            ({{ view.spend.spent_pct }}%)</span
          ><span v-if="view.spend.spend_as_of">, as of {{ view.spend.spend_as_of }}</span>.
          <span v-if="view.spend.time_elapsed_pct">
            {{ view.spend.time_elapsed_pct }}% of the delivery time had passed.
          </span>
        </p>
      </section>

      <section data-test="executive-risks">
        <h2 class="text-subtitle-1">High risks</h2>
        <p v-if="view.high_risks === 0">No open High risks.</p>
        <p v-else>
          {{ view.high_risks }} open High risk{{ view.high_risks === 1 ? '' : 's' }}:
          <template v-for="(id, i) in view.high_risk_ids" :key="id">
            <router-link
              :to="{ name: 'risks', query: { project: String(view.project_id), risk: String(id) } }"
              data-test="executive-risk"
              >risk {{ id }}</router-link
            >{{ i < view.high_risk_ids.length - 1 ? ', ' : '.' }}
          </template>
        </p>
      </section>
    </article>
  </v-container>
</template>

<style scoped>
.pre {
  white-space: pre-line;
}
.executive-page {
  max-width: 900px;
}
/* One page: no menu, buttons or margins the page doesn't need, and nothing split across pages. */
@media print {
  @page {
    size: A4;
    margin: 12mm;
  }
  .no-print {
    display: none !important;
  }
  .executive-page {
    max-width: none;
    padding: 0 !important;
    font-size: 11pt;
  }
  .executive-page section {
    break-inside: avoid;
  }
}
</style>

<style>
/* While the executive view is shown, printing leaves out the app's bar, menu and footer, so the
   report alone fills the one page. */
@media print {
  body:has(.print-one-page) .v-app-bar,
  body:has(.print-one-page) .v-navigation-drawer,
  body:has(.print-one-page) .v-footer {
    display: none !important;
  }
  body:has(.print-one-page) .v-main {
    padding: 0 !important;
  }
}
</style>
