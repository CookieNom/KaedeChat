export type ThemePreference = 'system' | 'light' | 'dark';

export function storedTheme(): ThemePreference {
  try {
    const stored = localStorage.getItem('kaede.theme');
    if (stored === 'light' || stored === 'dark') return stored;
  } catch {
    // Hardened browser contexts may expose storage but deny access.
  }
  return 'system';
}

export function applyTheme(theme: ThemePreference, persist = true): void {
  document.documentElement.dataset.theme = theme;
  if (persist) {
    try {
      localStorage.setItem('kaede.theme', theme);
    } catch {
      // The current page still receives the theme when storage is unavailable.
    }
  }

  const dark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', dark ? '#181715' : '#f3efe8');
}
