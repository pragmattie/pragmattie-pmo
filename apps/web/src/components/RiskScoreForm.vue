<script setup lang="ts">
import { computed } from 'vue'

import {
  bandColor,
  type HeatCell,
  type RiskScales,
  scaleHint,
  scaleItems,
  scorePreview,
} from '../lib/risks'

// Scores a risk in place (a `score_risk` item in the Inbox): probability and impact on the
// labelled 1 to 5 scales of the risk policy, each level with its definition, the score and band
// they give, and Save. The page saves through the existing risk update route, where the API checks every rule again and works out the real score.
const props = defineProps<{
  cells: HeatCell[]
  busy: boolean
  scales?: RiskScales | null
}>()
const probability = defineModel<number | null>('probability', { required: true })
const impact = defineModel<number | null>('impact', { required: true })
const emit = defineEmits<{ save: [] }>()

const probabilityItems = computed(() => scaleItems(props.scales?.probability))
const impactItems = computed(() => scaleItems(props.scales?.impact))
const preview = computed(() => scorePreview(props.cells, probability.value, impact.value))
</script>

<template>
  <v-card variant="outlined" class="mb-3" data-test="score-form">
    <v-card-title class="text-subtitle-1">Score this risk</v-card-title>
    <v-card-text>
      <v-row density="compact">
        <v-col cols="6">
          <v-select
            v-model="probability"
            label="Probability (1 to 5)"
            :items="probabilityItems"
            :hint="scaleHint(scales?.probability, probability)"
            persistent-hint
            data-test="score-probability"
          />
        </v-col>
        <v-col cols="6">
          <v-select
            v-model="impact"
            label="Impact (1 to 5)"
            :items="impactItems"
            :hint="scaleHint(scales?.impact, impact)"
            persistent-hint
            data-test="score-impact"
          />
        </v-col>
      </v-row>
      <p class="text-body-2 mt-3 mb-0" data-test="score-preview">
        <template v-if="preview">
          Score {{ preview.score }}
          <v-chip
            v-if="preview.band"
            :color="bandColor(preview.band)"
            variant="flat"
            size="small"
            class="ml-1"
            data-test="score-band"
          >
            {{ preview.band }}
          </v-chip>
        </template>
        <template v-else>Choose a probability and an impact to see the score and band.</template>
      </p>
    </v-card-text>
    <v-card-actions>
      <v-spacer />
      <v-btn
        color="primary"
        variant="flat"
        :disabled="!preview"
        :loading="busy"
        data-test="score-save"
        @click="emit('save')"
      >
        Save
      </v-btn>
    </v-card-actions>
  </v-card>
</template>
