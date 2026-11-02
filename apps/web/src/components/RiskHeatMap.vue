<script setup lang="ts">
import { computed } from 'vue'

import { bandColor, type HeatCell, heatGrid, LEVELS } from '../lib/risks'

const props = defineProps<{
  cells: HeatCell[]
  // The cell the table is filtered to, if any.
  selected: { probability: number | null; impact: number | null }
}>()
const emit = defineEmits<{ select: [cell: HeatCell] }>()

const rows = computed(() => heatGrid(props.cells))

function isSelected(cell: HeatCell) {
  return props.selected.probability === cell.probability && props.selected.impact === cell.impact
}

function cellLabel(cell: HeatCell) {
  const risks = cell.count === 1 ? '1 open risk' : `${cell.count} open risks`
  const band = cell.band ? `, ${cell.band} band` : ''
  return `Probability ${cell.probability}, impact ${cell.impact}: ${risks}${band}`
}
</script>

<template>
  <div class="heat-map" data-test="heat-map">
    <div class="heat-map__axis heat-map__axis--y text-caption">Impact</div>
    <div class="heat-map__grid" role="grid" aria-label="Open risks by probability and impact">
      <div v-for="row in rows" :key="row[0].impact" class="heat-map__row" role="row">
        <span class="heat-map__tick text-caption">{{ row[0].impact }}</span>
        <button
          v-for="cell in row"
          :key="cell.probability"
          type="button"
          role="gridcell"
          :class="[
            'heat-map__cell',
            `bg-${bandColor(cell.band || null)}`,
            { 'heat-map__cell--selected': isSelected(cell), 'heat-map__cell--empty': !cell.count },
          ]"
          :aria-label="cellLabel(cell)"
          :aria-pressed="isSelected(cell)"
          :title="cellLabel(cell)"
          :data-test="`heat-cell-${cell.probability}-${cell.impact}`"
          @click="emit('select', cell)"
        >
          {{ cell.count || '' }}
        </button>
      </div>
      <div class="heat-map__row">
        <span class="heat-map__tick" />
        <span v-for="level in LEVELS" :key="level" class="heat-map__tick text-caption">
          {{ level }}
        </span>
      </div>
      <div class="heat-map__axis text-caption">Probability</div>
    </div>
  </div>
</template>

<style scoped>
.heat-map {
  display: flex;
  align-items: center;
  gap: 4px;
  max-width: 360px;
}
.heat-map__axis--y {
  writing-mode: vertical-rl;
  transform: rotate(180deg);
}
.heat-map__axis {
  text-align: center;
}
.heat-map__grid {
  flex: 1;
}
.heat-map__row {
  display: grid;
  grid-template-columns: 20px repeat(5, 1fr);
  gap: 4px;
  margin-bottom: 4px;
}
.heat-map__tick {
  display: flex;
  align-items: center;
  justify-content: center;
}
.heat-map__cell {
  aspect-ratio: 1;
  min-height: 36px;
  border-radius: 4px;
  font-weight: 600;
  border: 2px solid transparent;
  cursor: pointer;
}
.heat-map__cell--empty {
  opacity: 0.45;
}
.heat-map__cell--selected {
  border-color: rgb(var(--v-theme-primary));
  outline: 2px solid rgb(var(--v-theme-on-primary));
  outline-offset: -4px;
  opacity: 1;
}
.heat-map__cell:focus-visible {
  outline: 3px solid rgb(var(--v-theme-primary));
}
</style>
