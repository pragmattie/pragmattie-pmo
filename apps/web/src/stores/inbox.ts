import { defineStore } from 'pinia'

import { inboxApi, POLL_MS } from '../lib/inbox'

// The inbox count for the navigation badge and the home page. While polling, it refreshes every
// 15 seconds; it pauses while the tab is hidden and refreshes as soon as it is visible again.
let timer: ReturnType<typeof setInterval> | null = null
let onVisibility: (() => void) | null = null

export const useInboxStore = defineStore('inbox', {
  state: () => ({ count: null as number | null }),
  actions: {
    async refresh() {
      try {
        this.count = (await inboxApi.count()).count
      } catch {
        // Keep the last count; the next poll tries again.
      }
    },
    startPolling() {
      this.stopPolling()
      const run = () => {
        if (timer === null) timer = setInterval(() => this.refresh(), POLL_MS)
      }
      const pause = () => {
        if (timer !== null) clearInterval(timer)
        timer = null
      }
      onVisibility = () => {
        if (document.hidden) {
          pause()
        } else {
          this.refresh()
          run()
        }
      }
      document.addEventListener('visibilitychange', onVisibility)
      if (!document.hidden) {
        this.refresh()
        run()
      }
    },
    stopPolling() {
      if (timer !== null) clearInterval(timer)
      timer = null
      if (onVisibility) document.removeEventListener('visibilitychange', onVisibility)
      onVisibility = null
    },
  },
})
