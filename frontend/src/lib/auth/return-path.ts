const APP_RETURN_PATH = /^\/(?:home(?:\/|$)|g\/|invite\/|settings(?:\/|$))/;

export function safeReturnPath(value: string | null, origin: string): string | null {
  if (!value || !value.startsWith('/')) return null;
  try {
    const target = new URL(value, origin);
    if (target.origin !== new URL(origin).origin || !APP_RETURN_PATH.test(target.pathname))
      return null;
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return null;
  }
}
