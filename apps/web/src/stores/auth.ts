import { defineStore } from 'pinia'

import { apiFetch } from '../lib/api'

export interface User {
  id: number
  email: string
  display_name: string
  role: string
  // What this role may do, from the API's permissions table (app/authz.py). The web only uses it
  // to hide things; the API checks every request.
  actions: string[]
}

export const LOGIN_FAILED = 'Invalid email or password.'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as User | null,
    // True once we have asked the API who is signed in.
    checked: false,
    sessionExpired: false,
  }),
  getters: {
    isAuthenticated: (state) => state.user !== null,
    can: (state) => (action: string) => state.user?.actions?.includes(action) ?? false,
  },
  actions: {
    async fetchMe() {
      try {
        const response = await apiFetch('/api/v1/auth/me', { handle401: false })
        this.user = response.ok ? ((await response.json()) as User) : null
      } catch {
        this.user = null
      }
      this.checked = true
      return this.user
    },
    async login(email: string, password: string) {
      const response = await apiFetch('/api/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email, password }),
        handle401: false,
      })
      if (!response.ok) throw new Error(LOGIN_FAILED)
      this.user = (await response.json()) as User
      this.checked = true
      this.sessionExpired = false
      return this.user
    },
    async logout() {
      try {
        await apiFetch('/api/v1/auth/logout', { method: 'POST', handle401: false })
      } catch {
        // Unreachable API: still sign out locally; the session expires on its own.
      }
      this.user = null
      this.checked = true
      this.sessionExpired = false
    },
    markExpired() {
      this.user = null
      this.checked = true
      this.sessionExpired = true
    },
  },
})
