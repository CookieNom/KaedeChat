import { describe, expect, it, vi } from 'vitest';
import { formatDateTime } from './locale';

describe('formatDateTime', () => {
  const instant = '2026-07-30T14:05:00.000Z';

  it('uses the requested locale', () => {
    const DateTimeFormat = Intl.DateTimeFormat;
    const formatter = vi
      .spyOn(Intl, 'DateTimeFormat')
      .mockImplementation(function (locales, options) {
        return new DateTimeFormat(locales, { ...options, timeZone: 'UTC' });
      });
    try {
      expect(formatDateTime(instant, 'en-US')).toMatch(/Jul 30, 2026,?\s+2:05\s+PM/);
      expect(formatDateTime(instant, 'ja-JP')).toMatch(/2026[/年]0?7[/月]30日?\s+14:05/);
    } finally {
      formatter.mockRestore();
    }
  });
  it('handles invalid values and locales safely', () => {
    expect(formatDateTime('not-a-date', 'en-US')).toEqual(expect.stringMatching(/\S/));
    expect(formatDateTime(instant, 'not_a_locale')).toContain('2026');
  });
});

import { get } from 'svelte/store';
import { IntlMessageFormat } from 'intl-messageformat';
import {
  activeLocale,
  applyLocale,
  catalogs,
  matchLanguage,
  preferredLocale,
  resolveSystemLanguage,
  setSystemLanguages,
  suggestedLanguage,
  t,
  translate
} from './locale';

describe('interface languages', () => {
  it('matches regional variants and falls back for unsupported languages', () => {
    expect(matchLanguage('ja_JP')).toBe('ja');
    expect(matchLanguage('en-GB')).toBe('en');
    expect(matchLanguage('invalid_locale')).toBeUndefined();
    expect(resolveSystemLanguage(['fr-FR', 'ja-JP'])).toBe('ja');
    expect(resolveSystemLanguage(['fr-FR'])).toBe('en');
  });
  it('never suggests a language for English devices, including multilingual ones', () => {
    for (const english of ['en', 'en-US', 'en-GB', 'EN_au']) {
      expect(suggestedLanguage([english, 'ja-JP'], 'en-US', false)).toBeUndefined();
    }
    expect(suggestedLanguage(['ja-JP'], 'en-US', false)).toBe('ja');
    expect(suggestedLanguage(['ja-JP'], 'ja', false)).toBeUndefined();
    expect(suggestedLanguage(['ja-JP'], 'en', true)).toBeUndefined();
    expect(suggestedLanguage(['fr-FR', 'ja-JP'], 'en', false)).toBeUndefined();
    expect(suggestedLanguage([], 'en', false)).toBeUndefined();
  });
  it('updates live translations, follows the device, and preserves an explicit choice', () => {
    setSystemLanguages(['ja-JP']);
    applyLocale('system');
    expect(get(t)('language_settings')).toBe('言語');
    expect(preferredLocale()).toBe('ja-JP');
    setSystemLanguages(['en-GB']);
    expect(get(activeLocale)).toBe('en');
    applyLocale('ja-JP');
    setSystemLanguages(['en-US']);
    expect(get(t)('language_settings')).toBe('言語');
    applyLocale('en-US');
    setSystemLanguages();
  });
  it('safely falls back for missing, empty, or malformed translations', () => {
    catalogs.en.test_fallback = 'Hello, {name}';
    expect(translate('test_fallback', { name: '<script>' }, 'ja')).toBe('Hello, <script>');
    catalogs.ja.test_fallback = '';
    expect(translate('test_fallback', { name: 'Kaede' }, 'ja')).toBe('Hello, Kaede');
    catalogs.ja.test_fallback = '{broken';
    expect(translate('test_fallback', { name: 'Kaede' }, 'ja')).toBe('Hello, Kaede');
    delete catalogs.en.test_fallback;
    delete catalogs.ja.test_fallback;
  });
  it('supports ICU plural rules and accepts all checked-in messages', () => {
    expect(
      new IntlMessageFormat('{count, plural, one {# member} other {# members}}', 'en').format({
        count: 2
      })
    ).toBe('2 members');
    for (const [locale, messages] of Object.entries(catalogs)) {
      for (const [key, message] of Object.entries(messages)) {
        expect(
          () => new IntlMessageFormat(message, locale, undefined, { ignoreTag: true }),
          `${locale}:${key}`
        ).not.toThrow();
      }
    }
  });
});
