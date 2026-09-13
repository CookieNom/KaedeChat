import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';
import { IntlMessageFormat } from 'intl-messageformat';
import { derived, get, writable } from 'svelte/store';

const files = import.meta.glob<Record<string, string>>('../locales/*.json', {
  eager: true,
  import: 'default'
});
export const catalogs: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(files).map(([path, messages]) => [
    path.split('/').pop()!.replace('.json', ''),
    messages
  ])
);
export const languages = Object.keys(catalogs)
  .sort()
  .map((code) => ({
    code,
    name: catalogs[code].language_name || code
  }));
export const activeLocale = writable('en');
export const localePreference = writable('en-US');
const formats = new Map<string, IntlMessageFormat>();
let deviceLanguages: readonly string[] | undefined;

export function matchLanguage(value: string): string | undefined {
  try {
    const tag = Intl.getCanonicalLocales(value.replaceAll('_', '-'))[0];
    return (
      Object.keys(catalogs).find((key) => key.toLowerCase() === tag.toLowerCase()) ??
      Object.keys(catalogs).find((key) => key.toLowerCase() === new Intl.Locale(tag).language)
    );
  } catch {
    return undefined;
  }
}

export function systemLanguages(): readonly string[] {
  return deviceLanguages ?? (typeof navigator !== 'undefined' ? navigator.languages : ['en']);
}

export function resolveSystemLanguage(values = systemLanguages()): string {
  for (const value of values) {
    const supported = matchLanguage(value);
    if (supported) return supported;
  }
  return 'en';
}

export function suggestedLanguage(
  values: readonly string[],
  current: string,
  handled: boolean
): string | undefined {
  if (handled || !values.length) return undefined;
  // Never offer another language when English is the device's first preference.
  if (/^en(?:[-_]|$)/i.test(values[0])) return undefined;
  const candidate = matchLanguage(values[0]);
  return candidate && candidate !== 'en' && candidate !== matchLanguage(current)
    ? candidate
    : undefined;
}

export function translate(
  key: string,
  values: Record<string, string | number> = {},
  locale = get(activeLocale)
): string {
  const translated = catalogs[locale]?.[key];
  const message = translated?.trim() ? translated : catalogs.en[key];
  if (!message) return key;
  const cacheKey = `${locale}:${key}:${message}`;
  try {
    let format = formats.get(cacheKey);
    if (!format) {
      format = new IntlMessageFormat(message, locale, undefined, { ignoreTag: true });
      formats.set(cacheKey, format);
    }
    return String(format.format(values));
  } catch {
    // A malformed community translation must never break the interface.
    if (locale !== 'en') return translate(key, values, 'en');
    return message;
  }
}

export const t = derived(
  activeLocale,
  (locale) =>
    (key: string, values: Record<string, string | number> = {}) =>
      translate(key, values, locale)
);

export function storedLocale(): string {
  try {
    return localStorage.getItem('kaede.locale') || 'en-US';
  } catch {
    return 'en-US';
  }
}
export function languagePromptHandled(): boolean {
  try {
    return localStorage.getItem('kaede.language-choice') === '1';
  } catch {
    return false;
  }
}
export function markLanguageChosen(): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('kaede:language-choice'));
  try {
    localStorage.setItem('kaede.language-choice', '1');
  } catch {
    /* Session state also dismisses the pane. */
  }
}
export function preferredLocale(): string {
  const preference = get(localePreference);
  if (preference === 'system') {
    return (
      systemLanguages().find((value) => matchLanguage(value) === get(activeLocale)) ??
      get(activeLocale)
    );
  }
  return matchLanguage(preference) ? preference : 'en-US';
}
export function applyLocale(locale: string): void {
  const preference = locale === 'system' || matchLanguage(locale) ? locale : 'en-US';
  localePreference.set(preference);
  const resolved = preference === 'system' ? resolveSystemLanguage() : matchLanguage(preference)!;
  activeLocale.set(resolved);
  if (isNativeDesktop()) {
    void nativeInvoke('native_set_menu_language', {
      labels: {
        show: translate('desktop_show', {}, resolved),
        leave_voice: translate('desktop_leave_voice', {}, resolved),
        quit: translate('desktop_quit', {}, resolved)
      }
    }).catch(() => {
      /* Older desktop shells still support the translated frontend. */
    });
  }
  if (typeof document !== 'undefined') {
    document.documentElement.lang = resolved;
    document.documentElement.dir = /^(ar|fa|he|ur)(-|$)/.test(resolved) ? 'rtl' : 'ltr';
  }
  try {
    localStorage.setItem('kaede.locale', preference);
  } catch {
    /* In-memory choice still works. */
  }
}
export function setSystemLanguages(values?: readonly string[]): void {
  const changed = JSON.stringify(deviceLanguages) !== JSON.stringify(values);
  deviceLanguages = values;
  if (changed && typeof window !== 'undefined')
    window.dispatchEvent(new Event('kaede:system-language'));
  if (get(localePreference) === 'system') applyLocale('system');
}
export function formatDateTime(value: string | number | Date, locale = preferredLocale()): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return translate('unknown_date');
  try {
    return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(
      date
    );
  } catch {
    return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(
      date
    );
  }
}
