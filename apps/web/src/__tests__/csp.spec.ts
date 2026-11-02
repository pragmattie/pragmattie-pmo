import type { HtmlTagDescriptor, IndexHtmlTransformHook } from 'vite'
import { describe, expect, it } from 'vitest'

import { CSP_DIRECTIVES, cspContent, cspPlugin } from '../../build/csp'
import viteConfig from '../../vite.config'

// Vitest checks the policy text and where it goes. It can't prove the app works under it: that
// still needs a check of the built app in a browser (see docs/security.md).

function metaTags(): HtmlTagDescriptor[] {
  const hook = cspPlugin().transformIndexHtml as { handler: IndexHtmlTransformHook }
  return hook.handler.call({} as never, '<html></html>', {} as never) as HtmlTagDescriptor[]
}

function directive(name: string): string[] {
  const part = cspContent()
    .split('; ')
    .find((entry) => entry.startsWith(`${name} `))
  return part ? part.split(' ').slice(1) : []
}

describe('content security policy', () => {
  it('is written as one meta tag at the top of head', () => {
    const tags = metaTags()
    expect(tags).toHaveLength(1)
    expect(tags[0]).toMatchObject({
      tag: 'meta',
      attrs: { 'http-equiv': 'Content-Security-Policy', content: cspContent() },
      injectTo: 'head-prepend',
    })
  })

  it('applies to the build only, not the dev server', () => {
    expect(cspPlugin().apply).toBe('build')
  })

  it('is part of the Vite config', () => {
    const names = (viteConfig.plugins ?? []).flat().map((p) => (p as { name?: string })?.name)
    expect(names).toContain('pmo-csp')
  })

  it('allows scripts from the app only: no inline code, no eval', () => {
    expect(directive('script-src')).toEqual(["'self'"])
    expect(cspContent()).not.toContain("'unsafe-eval'")
    expect(directive('default-src')).toEqual(["'self'"])
  })

  it("allows inline styles, which Vuetify's theme needs", () => {
    expect(directive('style-src')).toEqual(["'self'", "'unsafe-inline'"])
  })

  it('blocks plugins and keeps base, forms and API calls on this origin', () => {
    expect(directive('object-src')).toEqual(["'none'"])
    expect(directive('base-uri')).toEqual(["'self'"])
    expect(directive('form-action')).toEqual(["'self'"])
    expect(directive('connect-src')).toEqual(["'self'"])
  })

  it('names no outside host', () => {
    for (const sources of Object.values(CSP_DIRECTIVES)) {
      for (const source of sources) {
        expect(source).toMatch(/^('self'|'none'|'unsafe-inline'|data:)$/)
      }
    }
  })
})
