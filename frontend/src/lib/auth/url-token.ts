/**
 * Read a one-time credential from a URL fragment (preferred) or a legacy query
 * parameter, then remove it before any later navigation or referrer can expose it.
 */
export function consumeUrlToken(name = 'token'): string | null {
  const url = new URL(window.location.href);
  const fragment = new URLSearchParams(url.hash.startsWith('#') ? url.hash.slice(1) : url.hash);
  const token = fragment.get(name) ?? url.searchParams.get(name);
  if (token === null) return null;

  fragment.delete(name);
  url.searchParams.delete(name);
  url.hash = fragment.size ? `#${fragment.toString()}` : '';
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  return token;
}
