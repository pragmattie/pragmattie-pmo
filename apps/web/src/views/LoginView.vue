<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { safeRedirect } from '../lib/redirect'
import { LOGIN_FAILED, useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const email = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)
const expired = computed(() => auth.sessionExpired || route.query.expired === '1')

async function submit() {
  error.value = ''
  busy.value = true
  try {
    await auth.login(email.value, password.value)
    password.value = ''
    await router.replace(safeRedirect(route.query.redirect))
  } catch {
    error.value = LOGIN_FAILED
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <v-container class="py-12" style="max-width: 480px">
    <h1 class="text-h4 text-primary mb-6">Sign in</h1>
    <v-alert
      v-if="expired && !error"
      type="info"
      variant="tonal"
      class="mb-4"
      data-test="session-expired"
    >
      Your session expired. Please sign in again.
    </v-alert>
    <v-alert v-if="error" type="error" variant="tonal" class="mb-4" data-test="login-error">
      {{ error }}
    </v-alert>
    <v-form @submit.prevent="submit">
      <v-text-field
        v-model="email"
        label="Email"
        type="email"
        autocomplete="username"
        required
        data-test="email"
      />
      <v-text-field
        v-model="password"
        label="Password"
        type="password"
        autocomplete="current-password"
        maxlength="256"
        required
        data-test="password"
      />
      <v-btn type="submit" color="primary" :loading="busy" block data-test="submit">
        Sign in
      </v-btn>
    </v-form>
  </v-container>
</template>
