<script setup lang="ts">
import { onMounted } from 'vue'

import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'
import { useInboxStore } from '../stores/inbox'

const health = useHealthStore()
const auth = useAuthStore()
// The inbox count, kept fresh by the menu's poll.
const inbox = useInboxStore()
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
    <p v-if="auth.user && inbox.count !== null" class="text-body-1 mt-6" data-test="home-inbox">
      <router-link :to="{ name: 'inbox' }">
        {{ inbox.count === 1 ? '1 decision needs you' : `${inbox.count} decisions need you` }}
      </router-link>
    </p>
  </v-container>
</template>
