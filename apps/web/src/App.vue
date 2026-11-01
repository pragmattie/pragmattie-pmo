<script setup lang="ts">
import { useRouter } from 'vue-router'

import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const router = useRouter()

async function logout() {
  await auth.logout()
  await router.push({ name: 'login' })
}
</script>

<template>
  <v-app>
    <v-app-bar color="primary" flat>
      <v-app-bar-title data-test="brand">PragMattie PMO</v-app-bar-title>
      <template v-if="auth.user">
        <span class="text-body-2 mr-2" data-test="user-name">{{ auth.user.display_name }}</span>
        <v-btn variant="outlined" class="mr-2" data-test="logout" @click="logout">Sign out</v-btn>
      </template>
    </v-app-bar>
    <v-main>
      <router-view />
    </v-main>
    <v-footer app color="primary" class="justify-center text-body-2" data-test="footer">
      &copy; PragMattie Growth Partners, LLC. All rights reserved.
    </v-footer>
  </v-app>
</template>
