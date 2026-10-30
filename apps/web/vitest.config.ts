import { defineConfig, mergeConfig } from 'vitest/config'

import viteConfig from './vite.config.ts'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      // Vuetify imports CSS; the tests only check markup and theme values.
      css: false,
      server: { deps: { inline: ['vuetify'] } },
    },
  }),
)
