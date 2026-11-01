// Only a same-site path may be a redirect target after login.
export function safeRedirect(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/')) return '/'
  // "//host" and "/\host" would leave the site.
  return value.startsWith('//') || value.startsWith('/\\') ? '/' : value
}
