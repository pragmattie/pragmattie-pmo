<script setup lang="ts">
import { onMounted } from 'vue'

import { useHealthStore } from '../stores/health'

const health = useHealthStore()
onMounted(() => health.check())
</script>

<template>
  <v-container class="py-12">
    <h1 class="text-h3 text-primary mb-4">PragMattie PMO</h1>
    <p class="text-h6 text-secondary mb-8">
      Portfolio orchestration for a consulting practice.
    </p>
    <v-chip color="accent" variant="flat" data-test="api-status">
      API: {{ health.status }}<span v-if="health.version">&nbsp;(v{{ health.version }})</span>
    </v-chip>
  </v-container>
</template>
