import 'vuetify/styles'

import { createVuetify, type ThemeDefinition } from 'vuetify'

export const brandColors = {
  navy: '#2E3F55',
  teal: '#1B8A94',
  gold: '#E8B35A',
} as const

export const pragmattiePmo: ThemeDefinition = {
  dark: false,
  colors: {
    primary: brandColors.navy,
    secondary: brandColors.teal,
    accent: brandColors.gold,
    background: '#FFFFFF',
    surface: '#FFFFFF',
  },
}

export function makeVuetify() {
  return createVuetify({
    theme: {
      defaultTheme: 'pragmattiePmo',
      themes: { pragmattiePmo },
    },
  })
}
