import 'vuetify/styles'

import { createVuetify, type ThemeDefinition } from 'vuetify'
import { aliases, mdi } from 'vuetify/iconsets/mdi-svg'

export const brandColors = {
  navy: '#2E3F55',
  teal: '#1B8A94',
  gold: '#E8B35A',
} as const

// Risk priority bands, by the band names in the risk policy. The Risks page reads them from the
// theme as `band-low`, `band-medium` and `band-high`.
export const bandColors = {
  'band-low': '#3E8E5E',
  'band-medium': brandColors.gold,
  'band-high': '#B23A3A',
} as const

export const pragmattiePmo: ThemeDefinition = {
  dark: false,
  colors: {
    primary: brandColors.navy,
    secondary: brandColors.teal,
    accent: brandColors.gold,
    background: '#FFFFFF',
    surface: '#FFFFFF',
    ...bandColors,
  },
}

export function makeVuetify() {
  return createVuetify({
    // Vuetify's built-in SVG icons (menu, close, select arrows, paging): no icon font to load.
    icons: { defaultSet: 'mdi', aliases, sets: { mdi } },
    theme: {
      defaultTheme: 'pragmattiePmo',
      themes: { pragmattiePmo },
    },
  })
}
