<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import ChangesSection from '../components/ChangesSection.vue'
import DeliverySection from '../components/DeliverySection.vue'
import GateSection from '../components/GateSection.vue'
import PageTrail from '../components/PageTrail.vue'
import PlanSection from '../components/PlanSection.vue'
import ProposalSection from '../components/ProposalSection.vue'
import type { AnyGate, Baseline } from '../lib/gates'
import { moneyText } from '../lib/intake'
import {
  type Assumption,
  type Brief,
  budgetText,
  label,
  type ProjectDetail,
  projectApi,
  type ProjectSummary,
  risksLink,
  type Stakeholder,
} from '../lib/projects'

// Projects (item 4.6). Without an id, the list of projects; with one, the project: its brief,
// plan (item 5.1), proposal (item 5.2), the go-ahead gate and its latest baseline (item 5.4), its
// change requests and delivery progress once in delivery (items 5.5 and 7.2), stakeholders and
// assumptions, links to its status reports (item 7.3) and risks, and one back to its request.
// The API guards every route; the baseline is read-only.
const route = useRoute()
const projectId = computed(() => (route.params.id ? Number(route.params.id) : null))

const projects = ref<ProjectSummary[]>([])
const project = ref<ProjectDetail | null>(null)
const brief = ref<Brief | null>(null)
const stakeholders = ref<Stakeholder[]>([])
const assumptions = ref<Assumption[]>([])
const loadError = ref('')
const briefError = ref('')
const baseline = ref<Baseline | null>(null)
const message = ref('')
const messageType = ref<'success' | 'warning'>('success')
const missing = ref<string[]>([])

function gateLoaded(gate: AnyGate) {
  baseline.value = 'baseline' in gate ? gate.baseline : null
}

function show(text: string, type: 'success' | 'warning', items: string[] = []) {
  message.value = text
  messageType.value = type
  missing.value = items
}

// A gate decision changes the project's stage or status, and whether its plan and proposal can
// change: reload the page.
async function gateDecided(text: string) {
  await load()
  show(text, 'success')
}

async function gateConflict(text: string, items: string[] = []) {
  await load()
  show(text, 'warning', items)
}

async function loadProject(id: number) {
  project.value = await projectApi.get(id)
  const [stakeholderPage, assumptionPage] = await Promise.all([
    projectApi.stakeholders(id),
    projectApi.assumptions(id),
  ])
  stakeholders.value = stakeholderPage.items
  assumptions.value = assumptionPage.items
  // A project made before item 4.6 has no brief.
  try {
    brief.value = await projectApi.brief(id)
  } catch (error) {
    brief.value = null
    briefError.value = (error as Error).message
  }
}

async function load() {
  loadError.value = ''
  briefError.value = ''
  project.value = null
  brief.value = null
  baseline.value = null
  message.value = ''
  try {
    if (projectId.value === null) projects.value = (await projectApi.list()).items
    else await loadProject(projectId.value)
  } catch (error) {
    loadError.value = (error as Error).message
  }
}

watch(projectId, load, { immediate: true })
</script>

<template>
  <v-container class="py-8">
    <v-alert v-if="loadError" type="error" variant="tonal" class="mb-4" data-test="load-error">
      {{ loadError }}
    </v-alert>

    <v-alert
      v-if="message"
      :type="messageType"
      variant="tonal"
      class="mb-4"
      closable
      data-test="message"
      @click:close="message = ''"
    >
      {{ message }}
      <ul v-if="missing.length" class="mt-2 pl-4" data-test="missing">
        <li v-for="item in missing" :key="item">{{ item }}</li>
      </ul>
    </v-alert>

    <template v-if="projectId === null">
      <h1 class="text-h4 text-primary mb-4" data-test="page-title">Projects</h1>
      <p v-if="!projects.length && !loadError" class="text-body-1" data-test="no-projects">
        No projects yet. A request becomes a project when a sponsor passes it at the approval gate.
      </p>
      <v-list v-else density="compact" data-test="project-list">
        <v-list-item
          v-for="item in projects"
          :key="item.id"
          :to="{ name: 'project', params: { id: item.id } }"
          :title="item.name"
          :subtitle="`${label(item.stage)}, ${label(item.status)}`"
          data-test="project-item"
        />
      </v-list>
    </template>

    <template v-else-if="project">
      <PageTrail :crumbs="[{ title: 'Projects', to: { name: 'projects' } }, { title: project.name }]" />
      <div class="d-flex flex-wrap align-center ga-2 mb-2">
        <h1 class="text-h4 text-primary" data-test="page-title">{{ project.name }}</h1>
        <v-spacer />
        <v-btn
          v-if="project.request_id"
          variant="text"
          :to="{ name: 'request', params: { id: project.request_id } }"
          data-test="request-link"
        >
          Its request
        </v-btn>
        <v-btn
          variant="outlined"
          :to="{ name: 'status-reports', params: { id: project.id } }"
          data-test="status-reports-link"
        >
          Status reports
        </v-btn>
        <v-btn color="primary" variant="outlined" :to="risksLink(project.id)" data-test="risks-link">
          Risks
        </v-btn>
      </div>
      <p class="text-body-2 text-medium-emphasis mb-4" data-test="project-summary">
        {{ label(project.stage) }}, {{ label(project.status) }}.
        <span v-if="project.client_name">Client: {{ project.client_name }}.</span>
        Sponsor: {{ project.sponsor }}. Project manager: {{ project.pm }}.
      </p>

      <section class="mb-6" data-test="brief">
        <h2 class="text-subtitle-1 mb-2">Brief</h2>
        <v-alert v-if="!brief" type="info" variant="tonal" data-test="no-brief">
          {{ briefError || 'This project has no brief.' }}
        </v-alert>
        <v-table v-else density="compact">
          <tbody>
            <tr>
              <th scope="row">Purpose</th>
              <td class="pre" data-test="brief-purpose">{{ brief.purpose }}</td>
            </tr>
            <tr>
              <th scope="row">Objectives</th>
              <td class="pre" data-test="brief-objectives">{{ brief.objectives }}</td>
            </tr>
            <tr>
              <th scope="row">Scope in</th>
              <td class="pre">{{ brief.scope_in ?? 'Not set' }}</td>
            </tr>
            <tr>
              <th scope="row">Scope out</th>
              <td class="pre">{{ brief.scope_out ?? 'Not set' }}</td>
            </tr>
            <tr>
              <th scope="row">Wanted by</th>
              <td>{{ brief.wanted_by ?? 'Not set' }}</td>
            </tr>
            <tr>
              <th scope="row">Rough budget</th>
              <td data-test="brief-budget">{{ budgetText(brief.budget_low, brief.budget_high) }}</td>
            </tr>
            <tr>
              <th scope="row">Who decides what</th>
              <td>
                <ul class="pl-4" data-test="decision-rights">
                  <li v-for="right in brief.decision_rights" :key="right.role">
                    {{ right.title }}: {{ right.decides }}
                  </li>
                </ul>
              </td>
            </tr>
          </tbody>
        </v-table>
      </section>

      <PlanSection :project-id="project.id" />
      <ProposalSection :project-id="project.id" :client-name="project.client_name" />

      <GateSection
        subject="project"
        :subject-id="project.id"
        class="mb-6"
        @loaded="gateLoaded"
        @decided="gateDecided"
        @conflict="gateConflict"
      />

      <section v-if="baseline" id="baseline" class="mb-6" data-test="baseline">
        <h2 class="text-subtitle-1 mb-2">Baseline {{ baseline.number }}</h2>
        <p class="text-caption text-medium-emphasis mb-2" data-test="baseline-made-by">
          The plan as it stood when
          {{ baseline.change_request_id ? 'a change request was approved' : 'the go-ahead gate was passed' }}
          on {{ new Date(baseline.created_at).toLocaleDateString() }}. It doesn't change; change
          requests are measured against it.
        </p>
        <v-table density="compact">
          <tbody>
            <tr>
              <th scope="row">Scope in</th>
              <td class="pre">{{ baseline.plan.scope_in ?? 'Not set' }}</td>
            </tr>
            <tr>
              <th scope="row">Scope out</th>
              <td class="pre">{{ baseline.plan.scope_out ?? 'Not set' }}</td>
            </tr>
            <tr>
              <th scope="row">Milestones</th>
              <td>
                <ul class="pl-4" data-test="baseline-milestones">
                  <li v-for="item in baseline.plan.milestones" :key="item.id">
                    {{ item.name }}, {{ item.target_date }}
                  </li>
                </ul>
              </td>
            </tr>
            <tr>
              <th scope="row">Deliverables</th>
              <td>
                <ul class="pl-4" data-test="baseline-deliverables">
                  <li v-for="item in baseline.plan.deliverables" :key="item.id">
                    {{ item.name }}<span v-if="item.milestone_name"> ({{ item.milestone_name }})</span>
                  </li>
                </ul>
              </td>
            </tr>
            <tr>
              <th scope="row">Estimate</th>
              <td>
                <ul class="pl-4" data-test="baseline-lines">
                  <li v-for="line in baseline.plan.estimate_lines" :key="line.id">
                    {{ line.description }} ({{ label(line.category) }}): {{ moneyText(line.amount) }}
                  </li>
                </ul>
              </td>
            </tr>
            <tr>
              <th scope="row">Contingency</th>
              <td>
                {{ baseline.plan.contingency_pct }}%:
                {{ moneyText(baseline.plan.contingency_amount) }}
              </td>
            </tr>
            <tr>
              <th scope="row">Total</th>
              <td data-test="baseline-total">{{ moneyText(baseline.total) }}</td>
            </tr>
          </tbody>
        </v-table>
      </section>

      <ChangesSection
        v-if="baseline"
        :project-id="project.id"
        :baseline="baseline"
        @decided="gateDecided"
      />

      <!-- A new baseline moves the targets days late are measured against: read them again. -->
      <DeliverySection
        v-if="project.stage === 'delivery'"
        :key="baseline?.number ?? 0"
        :project-id="project.id"
      />

      <section id="stakeholders" class="mb-6" data-test="stakeholders">
        <h2 class="text-subtitle-1 mb-2">Stakeholders</h2>
        <p v-if="!stakeholders.length" class="text-body-2">No stakeholders yet.</p>
        <v-table v-else density="compact">
          <thead>
            <tr>
              <th>Name</th>
              <th>Role or interest</th>
              <th>Needs to hear</th>
              <th>How often</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="person in stakeholders" :key="person.id" data-test="stakeholder">
              <td>{{ person.name }}</td>
              <td>{{ person.role_or_interest }}</td>
              <td>{{ person.needs_to_hear ?? 'Not set' }}</td>
              <td>{{ person.how_often ?? 'Not set' }}</td>
            </tr>
          </tbody>
        </v-table>
      </section>

      <section data-test="assumptions">
        <h2 class="text-subtitle-1 mb-2">Assumptions</h2>
        <p v-if="!assumptions.length" class="text-body-2" data-test="no-assumptions">
          No assumptions yet.
        </p>
        <v-table v-else density="compact">
          <thead>
            <tr>
              <th>Assumption</th>
              <th>Owner</th>
              <th>Confirm by</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in assumptions" :key="item.id" data-test="assumption">
              <td>{{ item.text }}</td>
              <td>{{ item.owner_name ?? `User ${item.owner_user_id}` }}</td>
              <td>{{ item.confirm_by }}</td>
              <td>{{ label(item.status) }}</td>
            </tr>
          </tbody>
        </v-table>
      </section>
    </template>
  </v-container>
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
