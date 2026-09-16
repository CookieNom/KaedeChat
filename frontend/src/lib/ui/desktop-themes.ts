import { writable } from 'svelte/store';
import { isNativeDesktop, nativeError, nativeInvoke } from '$lib/platform/native';
import { applyDesktopTheme, type ThemePreference } from './theme';

interface DesktopTheme {
  id: string;
  name: string;
  description: string;
  base: ThemePreference;
}

interface Catalog {
  themes: DesktopTheme[];
  css: string | null;
  skipped: string[];
}

const SELECTION = 'kaede.desktop-theme';
const CACHE = 'kaede.desktop-theme-cache';
export const desktopThemes = writable({
  themes: [] as DesktopTheme[],
  selected: '',
  error: '',
  missing: false,
  skipped: [] as string[]
});

let selected = '';
let refresh: (() => Promise<void>) | null = null;

export function selectDesktopTheme(id: string): void {
  selected = id;
  try {
    localStorage.setItem(SELECTION, id);
    localStorage.removeItem(CACHE);
  } catch {
    /* The selection still works for this session. */
  }
  desktopThemes.update((state) => ({ ...state, selected, missing: false }));
  if (!id) applyDesktopTheme(null);
  void refresh?.();
}

export function startDesktopThemes(): () => void {
  if (!isNativeDesktop()) return () => {};
  let stopped = false;
  let revision = 0;
  let timer: ReturnType<typeof setTimeout>;
  try {
    selected = localStorage.getItem(SELECTION) ?? '';
    const cached = JSON.parse(localStorage.getItem(CACHE) ?? 'null');
    if (
      cached?.id === selected &&
      typeof cached.css === 'string' &&
      ['light', 'dark', 'system'].includes(cached.base)
    )
      applyDesktopTheme(cached);
  } catch {
    /* Missing or invalid cache falls back to the account theme. */
  }
  desktopThemes.update((state) => ({ ...state, selected }));

  const scan = async () => {
    const request = ++revision;
    const requested = selected;
    try {
      const catalog = await nativeInvoke<Catalog>('native_themes', { selected: requested });
      if (stopped || request !== revision || requested !== selected) return;
      const theme = catalog.themes.find((item) => item.id === selected);
      const active = theme && catalog.css !== null ? { ...theme, css: catalog.css } : null;
      applyDesktopTheme(active);
      try {
        if (active) localStorage.setItem(CACHE, JSON.stringify(active));
        else localStorage.removeItem(CACHE);
      } catch {
        /* Theme files remain the source of truth if caching is unavailable. */
      }
      desktopThemes.set({
        themes: catalog.themes,
        selected,
        error: '',
        missing: !!selected && !active,
        skipped: catalog.skipped
      });
    } catch (caught) {
      if (stopped || request !== revision || requested !== selected) return;
      applyDesktopTheme(null);
      desktopThemes.update((state) => ({
        ...state,
        error: nativeError(caught).message ?? 'Could not load desktop themes.'
      }));
    }
  };
  refresh = scan;
  const poll = async () => {
    await scan();
    if (!stopped) timer = setTimeout(() => void poll(), 2000);
  };
  const storageChanged = (event: StorageEvent) => {
    if (event.key !== SELECTION) return;
    selected = event.newValue ?? '';
    desktopThemes.update((state) => ({ ...state, selected }));
    if (!selected) applyDesktopTheme(null);
    void scan();
  };
  window.addEventListener('storage', storageChanged);
  window.addEventListener('focus', scan);
  void poll();
  return () => {
    stopped = true;
    clearTimeout(timer);
    refresh = null;
    window.removeEventListener('storage', storageChanged);
    window.removeEventListener('focus', scan);
  };
}
