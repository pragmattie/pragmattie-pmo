import type { Component } from 'vue'
import { createMemoryHistory, createRouter, createWebHistory } from 'vue-router'

import { setUnauthorizedHandler } from '../lib/api'
import { NAV_ITEMS } from '../lib/nav'
import { safeRedirect } from '../lib/redirect'
import { useAuthStore } from '../stores/auth'
import AuditView from '../views/AuditView.vue'
import BoardView from '../views/BoardView.vue'
import ExecutiveReportView from '../views/ExecutiveReportView.vue'
import HomeView from '../views/HomeView.vue'
import InboxView from '../views/InboxView.vue'
import IntakeView from '../views/IntakeView.vue'
import LoginView from '../views/LoginView.vue'
import ProjectView from '../views/ProjectView.vue'
import ProposalPrintView from '../views/ProposalPrintView.vue'
import RequestView from '../views/RequestView.vue'
import RisksView from '../views/RisksView.vue'
import StatusReportsView from '../views/StatusReportsView.vue'

// Menu pages that have a real view. The rest are hidden from the menu and their address leads
// home.
const VIEWS: Record<string, Component> = {
  inbox: InboxView,
  board: BoardView,
  projects: ProjectView,
  intake: IntakeView,
  risks: RisksView,
  audit: AuditView,
}

export function hasView(name: string): boolean {
  return name in VIEWS
}

export function makeRouter(memory = false) {
  const router = createRouter({
    history: memory ? createMemoryHistory() : createWebHistory(),
    routes: [
      { path: '/', name: 'home', component: HomeView },
      { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
      ...NAV_ITEMS.map((item) =>
        hasView(item.name)
          ? {
              path: item.path,
              name: item.name,
              component: VIEWS[item.name],
              meta: { action: item.action },
            }
          : { path: item.path, name: item.name, redirect: { name: 'home' } },
      ),
      // One project request: its fields, history and the buttons its status allows.
      {
        path: '/intake/:id(\\d+)',
        name: 'request',
        component: RequestView,
        meta: { action: 'project:read' },
      },
      // One project: its brief, stakeholders, assumptions and a link to its risks.
      {
        path: '/projects/:id(\\d+)',
        name: 'project',
        component: ProjectView,
        meta: { action: 'project:read' },
      },
      // A project's status reports: its periods and one report (`?report=<id>`).
      {
        path: '/projects/:id(\\d+)/status-reports',
        name: 'status-reports',
        component: StatusReportsView,
        meta: { action: 'project:read' },
      },
      // One published status report on one page (item 7.5), where a sponsor lands.
      {
        path: '/projects/:id(\\d+)/status-reports/:reportId(\\d+)/executive',
        name: 'status-report-executive',
        component: ExecutiveReportView,
        meta: { action: 'project:read' },
      },
      // A print view of the approved version of a project's proposal.
      {
        path: '/projects/:id(\\d+)/proposal/print',
        name: 'proposal-print',
        component: ProposalPrintView,
        meta: { action: 'project:read' },
      },
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
    // A page the role cannot use is hidden from the menu; typing its address leads home.
    if (typeof to.meta.action === 'string' && !auth.can(to.meta.action)) return { name: 'home' }
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
