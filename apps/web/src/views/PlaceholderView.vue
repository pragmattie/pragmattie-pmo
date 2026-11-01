<script setup lang="ts">
defineProps<{ title: string }>()
</script>

<template>
  <v-container class="py-12">
    <h1 class="text-h4 text-primary mb-4" data-test="page-title">{{ title }}</h1>
    <p class="text-body-1" data-test="placeholder">This page arrives in a later milestone.</p>
  </v-container>
</template>
