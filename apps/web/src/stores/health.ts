import { defineStore } from 'pinia'

export const useHealthStore = defineStore('health', {
  state: () => ({ status: 'unknown', version: '' }),
  actions: {
    async check() {
      try {
        const response = await fetch('/api/v1/health')
        const body = await response.json()
        this.status = body.status
        this.version = body.version
      } catch {
        this.status = 'unreachable'
      }
    },
  },
})
