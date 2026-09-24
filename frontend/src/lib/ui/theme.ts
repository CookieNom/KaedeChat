export type ThemePreference = 'system' | 'light' | 'dark';

declare global {
  interface Document {
    kaedeDesktopTheme?: { sheet: CSSStyleSheet; css: string };
  }
}

let desktopBase: ThemePreference | null = null;

export function applyDesktopTheme(theme: { base: ThemePreference; css: string } | null): void {
  desktopBase = theme?.base ?? null;
  // CSSOM styles work under the static app's strict style-src policy.
  const active = document.kaedeDesktopTheme;
  if (theme) {
    const sheet = active?.sheet ?? new CSSStyleSheet();
    if (active?.css !== theme.css) sheet.replaceSync(theme.css);
    document.adoptedStyleSheets = [
      ...document.adoptedStyleSheets.filter((item) => item !== sheet),
      sheet
    ];
    document.kaedeDesktopTheme = { sheet, css: theme.css };
  } else if (active) {
    document.adoptedStyleSheets = document.adoptedStyleSheets.filter(
      (item) => item !== active.sheet
    );
    delete document.kaedeDesktopTheme;
  }
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
