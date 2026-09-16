export type ThemePreference = 'system' | 'light' | 'dark';

let desktopBase: ThemePreference | null = null;

export function applyDesktopTheme(theme: { base: ThemePreference; css: string } | null): void {
  desktopBase = theme?.base ?? null;
  let style = document.getElementById('kaede-desktop-theme');
  if (theme) {
    if (!style) {
      style = document.createElement('style');
      style.id = 'kaede-desktop-theme';
    }
    if (style.textContent !== theme.css) style.textContent = theme.css;
    if (document.head.lastElementChild !== style) document.head.append(style);
  } else style?.remove();
  applyTheme(storedTheme(), false);
}

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
  document.documentElement.dataset.theme = desktopBase ?? theme;
  if (persist) {
    try {
      localStorage.setItem('kaede.theme', theme);
    } catch {
      // The current page still receives the theme when storage is unavailable.
    }
  }

  theme = desktopBase ?? theme;
  const dark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', dark ? '#181715' : '#f3efe8');
}
