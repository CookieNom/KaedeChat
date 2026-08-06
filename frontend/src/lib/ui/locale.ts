const DEFAULT_LOCALE = 'en-US';

export function preferredLocale(): string {
  if (typeof document !== 'undefined') {
    const language = document.documentElement.lang.trim();
    if (language) return language;
  }
  if (typeof localStorage !== 'undefined') {
    try {
      const stored = localStorage.getItem('kaede.locale')?.trim();
      if (stored) return stored;
    } catch {
      // Hardened browser contexts may expose storage but deny access.
    }
  }
  return DEFAULT_LOCALE;
}

export function applyLocale(locale: string): void {
  const normalized = locale.trim() || DEFAULT_LOCALE;
  document.documentElement.lang = normalized;
  try {
    localStorage.setItem('kaede.locale', normalized);
  } catch {
    // The current page still receives the locale when storage is unavailable.
  }
}

export function formatDateTime(value: string | number | Date, locale = preferredLocale()): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unknown date';
  try {
    return new Intl.DateTimeFormat(locale, {
      dateStyle: 'medium',
      timeStyle: 'short'
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat(DEFAULT_LOCALE, {
      dateStyle: 'medium',
      timeStyle: 'short'
    }).format(date);
  }
}
