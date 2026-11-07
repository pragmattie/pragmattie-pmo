// Menu pages and the action each one needs. Which actions a user has comes from the API
// (`actions` on /auth/me), which holds the only permissions table; this list only says what each
// page is for. Pages without a view yet (see the router) are placeholders until their milestones
// arrive.

export interface NavItem {
  name: string
  path: string
  title: string
  action: string
}

export const NAV_ITEMS: NavItem[] = [
  // The decisions waiting on the signed-in person; the menu shows their count.
  { name: 'inbox', path: '/inbox', title: 'Needs you', action: 'project:read' },
  { name: 'projects', path: '/projects', title: 'Projects', action: 'project:read' },
  { name: 'intake', path: '/intake', title: 'Intake', action: 'project:read' },
  { name: 'risks', path: '/risks', title: 'Risks', action: 'project:read' },
  { name: 'audit', path: '/audit', title: 'Audit', action: 'audit:read' },
  { name: 'standards', path: '/standards', title: 'Standards', action: 'standards:read' },
]
