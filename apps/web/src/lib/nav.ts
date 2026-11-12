// Menu pages and the action each one needs. Which actions a user has comes from the API
// (`actions` on /auth/me), which holds the only permissions table; this list only says what each
// page is for. A page without a view yet (see the router) is hidden from the menu for everyone,
// and its address leads home.

export interface NavItem {
  name: string
  path: string
  title: string
  action: string
}

export const NAV_ITEMS: NavItem[] = [
  // The decisions waiting on the signed-in person; the menu shows a dot while any are waiting.
  { name: 'inbox', path: '/inbox', title: 'Inbox', action: 'project:read' },
  // Every request and project as a card by stage, with what each is waiting on.
  { name: 'board', path: '/board', title: 'Board', action: 'project:read' },
  { name: 'projects', path: '/projects', title: 'Projects', action: 'project:read' },
  { name: 'intake', path: '/intake', title: 'Intake', action: 'project:read' },
  { name: 'risks', path: '/risks', title: 'Risks', action: 'project:read' },
  // The audit log, read-only.
  { name: 'audit', path: '/audit', title: 'Audit', action: 'audit:read' },
  { name: 'standards', path: '/standards', title: 'Standards', action: 'standards:read' },
]

// The menu item a page belongs to, so a detail page highlights its menu page: a project under
// Projects, a request under Intake.
const PARENTS: Record<string, string> = {
  project: 'projects',
  request: 'intake',
  'proposal-print': 'projects',
  'status-reports': 'projects',
  'status-report-executive': 'projects',
}

export function menuItemFor(routeName: string | null | undefined): string | null {
  if (!routeName) return null
  return PARENTS[routeName] ?? routeName
}
