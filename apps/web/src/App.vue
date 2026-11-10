<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDisplay } from 'vuetify'

import { waitingText } from './lib/inbox'
import { menuItemFor, NAV_ITEMS } from './lib/nav'
import { hasView } from './router'

import { useAuthStore } from './stores/auth'
import { useInboxStore } from './stores/inbox'

const auth = useAuthStore()
const inbox = useInboxStore()
const router = useRouter()
const route = useRoute()
// At phone width the menu folds away behind a button so pages get the full screen.
const { smAndDown } = useDisplay()
const menuOpen = ref(!smAndDown.value)
watch(smAndDown, (small) => (menuOpen.value = !small))
// Only the pages this role may use and that have a view; the API still refuses anything else.
const navItems = computed(() =>
  NAV_ITEMS.filter((item) => hasView(item.name) && auth.can(item.action)),
)
// The menu item to highlight, on a detail page too (Projects for a project, Intake for a request).
const current = computed(() => menuItemFor(route.name as string | undefined))
// The inbox dot shows while anything waits on the signed-in person; its label keeps the count.
const waiting = computed(() => inbox.count ?? 0)
// The inbox count polls while someone is signed in (paused while the tab is hidden).
watch(
  () => auth.user?.id,
  (id) => {
    if (id === undefined) {
      inbox.stopPolling()
      inbox.count = null
    } else {
      inbox.startPolling()
    }
  },
  { immediate: true },
)
onUnmounted(() => inbox.stopPolling())

async function logout() {
  await auth.logout()
  await router.push({ name: 'login' })
}
</script>

<template>
  <v-app>
    <v-app-bar color="primary" flat>
      <v-app-bar-nav-icon
        v-if="auth.user && smAndDown"
        aria-label="Menu"
        data-test="menu-toggle"
        @click="menuOpen = !menuOpen"
      />
      <v-app-bar-title data-test="brand">PragMattie PMO</v-app-bar-title>
      <template v-if="auth.user">
        <span class="text-body-2 mr-2" data-test="user-name">{{ auth.user.display_name }}</span>
        <v-btn variant="outlined" class="mr-2" data-test="logout" @click="logout">Sign out</v-btn>
      </template>
    </v-app-bar>
    <v-navigation-drawer
      v-if="auth.user"
      v-model="menuOpen"
      :permanent="!smAndDown"
      :temporary="smAndDown"
      data-test="nav"
    >
      <v-list nav>
        <v-list-item :to="{ name: 'home' }" title="Home" exact data-test="nav-home" />
        <v-list-item
          v-for="item in navItems"
          :key="item.name"
          :to="{ name: item.name }"
          :title="item.title"
          :active="current === item.name"
          :data-test="`nav-${item.name}`"
        >
          <template v-if="item.name === 'inbox' && waiting > 0" #append>
            <span
              class="inbox-dot"
              role="img"
              :aria-label="waitingText(waiting)"
              data-test="inbox-dot"
            />
          </template>
        </v-list-item>
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

<style scoped>
.inbox-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: rgb(var(--v-theme-accent));
}
</style>
