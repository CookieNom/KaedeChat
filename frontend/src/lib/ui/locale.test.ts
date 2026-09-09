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
