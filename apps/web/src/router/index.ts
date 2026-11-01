import { createMemoryHistory, createRouter, createWebHistory } from 'vue-router'

import { setUnauthorizedHandler } from '../lib/api'
import { safeRedirect } from '../lib/redirect'
import { useAuthStore } from '../stores/auth'
import HomeView from '../views/HomeView.vue'
import LoginView from '../views/LoginView.vue'

export function makeRouter(memory = false) {
  const router = createRouter({
    history: memory ? createMemoryHistory() : createWebHistory(),
    routes: [
      { path: '/', name: 'home', component: HomeView },
      { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    ],
  })

  // The web only hides pages; the API enforces access on every request.
  router.beforeEach(async (to) => {
    const auth = useAuthStore()
    if (!auth.checked) await auth.fetchMe()
    if (to.meta.public) {
      return to.name === 'login' && auth.isAuthenticated ? safeRedirect(to.query.redirect) : true
    }
    if (!auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
    return true
  })

  // A 401 from any API call means the session ended: back to login with a note.
  setUnauthorizedHandler(() => {
    useAuthStore().markExpired()
    const current = router.currentRoute.value
    if (current.name === 'login') return
    router.push({ name: 'login', query: { redirect: current.fullPath, expired: '1' } })
  })

  return router
}
