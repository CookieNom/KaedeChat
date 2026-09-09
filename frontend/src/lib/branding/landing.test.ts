import { describe, expect, it } from 'vitest';
import { normalizeLandingVariant } from './landing';

describe('landing page selection', () => {
  it.each([
    ['unset', undefined, 'default'],
    ['null', null, 'default'],
    ['empty', '', 'default'],
    ['custom', 'custom', 'custom'],
    ['mixed case', 'Custom', 'custom'],
    ['padded uppercase', '  CUSTOM  ', 'custom'],
    ['default', 'default', 'default'],
    ['unknown', 'operator', 'default'],
    ['non-string', 1, 'default']
  ])('%s selects %s', (_name, input, expected) => {
    expect(normalizeLandingVariant(input)).toBe(expected);
  });
});
