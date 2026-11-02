/**
 * The Content Security Policy for the built web app, written into index.html as a <meta> tag.
 *
 * Build only: Vite's dev server injects inline scripts for hot reload, which this policy blocks.
 * A <meta> policy can't set frame-ancestors (or report), so once a production web server exists
 * the same policy should move to a response header (docs/security.md).
 *
 * script-src is strict: only the app's own bundled files. style-src allows 'unsafe-inline'
 * because Vuetify writes its theme as an inline <style> and components set inline styles; without
 * it the app renders unstyled.
 */
import type { Plugin } from 'vite'

export const CSP_DIRECTIVES: Record<string, string[]> = {
  'default-src': ["'self'"],
  'script-src': ["'self'"],
  'style-src': ["'self'", "'unsafe-inline'"],
  'img-src': ["'self'", 'data:'],
  'font-src': ["'self'", 'data:'],
  'connect-src': ["'self'"],
  'object-src': ["'none'"],
  'base-uri': ["'self'"],
  'form-action': ["'self'"],
}

export function cspContent(directives: Record<string, string[]> = CSP_DIRECTIVES): string {
  return Object.entries(directives)
    .map(([name, sources]) => `${name} ${sources.join(' ')}`)
    .join('; ')
}

export function cspPlugin(): Plugin {
  return {
    name: 'pmo-csp',
    apply: 'build',
    transformIndexHtml: {
      order: 'post',
      handler: () => [
        {
          tag: 'meta',
          attrs: { 'http-equiv': 'Content-Security-Policy', content: cspContent() },
          injectTo: 'head-prepend',
        },
      ],
    },
  }
}
