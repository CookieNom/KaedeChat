import { describe, expect, it } from 'vitest';
import { formatDateTime } from './locale';

describe('formatDateTime', () => {
  const instant = '2026-07-30T14:05:00.000Z';

  it('uses the requested locale', () => {
    expect(formatDateTime(instant, 'en-US')).toContain('2026');
    expect(formatDateTime(instant, 'ja-JP')).toContain('2026');
    expect(formatDateTime(instant, 'en-US')).not.toBe(formatDateTime(instant, 'ja-JP'));
  });

  it('handles invalid values and locales safely', () => {
    expect(formatDateTime('not-a-date', 'en-US')).toBe('Unknown date');
    expect(formatDateTime(instant, 'not_a_locale')).toContain('2026');
  });
});
