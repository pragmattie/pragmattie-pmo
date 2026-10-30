import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App.vue'
import { brandColors, makeVuetify } from '../plugins/vuetify'
import { makeRouter } from '../router'

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

async function mountApp() {
  const router = makeRouter(true)
  router.push('/')
  await router.isReady()
  const vuetify = makeVuetify()
  const wrapper = mount({ template: '<v-layout><App /></v-layout>', components: { App } }, {
    global: { plugins: [createPinia(), router, vuetify] },
  })
  return { wrapper, vuetify }
}

describe('App', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ json: async () => ({ status: 'ok', version: '0.1.0' }) })),
    )
  })
  afterEach(() => vi.unstubAllGlobals())

  it('renders the brand name', async () => {
    const { wrapper } = await mountApp()
    expect(wrapper.get('[data-test="brand"]').text()).toBe('PragMattie PMO')
  })

  it('credits PragMattie Growth Partners, LLC in the footer', async () => {
    const { wrapper } = await mountApp()
    expect(wrapper.get('[data-test="footer"]').text()).toContain('PragMattie Growth Partners, LLC')
  })

  it('uses the pragmattiePmo theme with the brand colours', async () => {
    const { vuetify } = await mountApp()
    expect(brandColors).toEqual({ navy: '#2E3F55', teal: '#1B8A94', gold: '#E8B35A' })
    const theme = vuetify.theme
    expect(theme.global.name.value).toBe('pragmattiePmo')
    const colors = theme.themes.value.pragmattiePmo.colors
    expect(String(colors.primary).toUpperCase()).toBe('#2E3F55')
    expect(String(colors.secondary).toUpperCase()).toBe('#1B8A94')
    expect(String(colors.accent).toUpperCase()).toBe('#E8B35A')
  })
})
