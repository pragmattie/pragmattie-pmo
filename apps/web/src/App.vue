<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { NAV_ITEMS } from './lib/nav'

import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const router = useRouter()
// Only the pages this role may use; the API still refuses anything else.
const navItems = computed(() => NAV_ITEMS.filter((item) => auth.can(item.action)))

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
    <v-navigation-drawer v-if="auth.user" permanent data-test="nav">
      <v-list nav>
        <v-list-item :to="{ name: 'home' }" title="Home" exact data-test="nav-home" />
        <v-list-item
          v-for="item in navItems"
          :key="item.name"
          :to="{ name: item.name }"
          :title="item.title"
          :data-test="`nav-${item.name}`"
        />
      </v-list>
    </v-navigation-drawer>
    <v-main>
      <router-view />
    </v-main>
    <v-footer app color="primary" class="justify-center text-body-2" data-test="footer">
      &copy; PragMattie Growth Partners, LLC. All rights reserved.
    </v-footer>
  </v-app>
</template>
