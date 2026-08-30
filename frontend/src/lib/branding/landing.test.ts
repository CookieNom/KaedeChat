import { describe, expect, it } from 'vitest';
import { normalizeLandingVariant } from './landing';

describe('landing page selection', () => {
  it('keeps the public homepage by default when the variable is unset', () => {
    expect(normalizeLandingVariant(undefined)).toBe('default');
    expect(normalizeLandingVariant(null)).toBe('default');
    expect(normalizeLandingVariant('')).toBe('default');
  });

  it('selects the operator homepage only for the exact custom value', () => {
    expect(normalizeLandingVariant('custom')).toBe('custom');
  });

  it('treats the custom value case- and whitespace-insensitively', () => {
    expect(normalizeLandingVariant('Custom')).toBe('custom');
    expect(normalizeLandingVariant('  CUSTOM  ')).toBe('custom');
  });

  it('falls back to the public homepage for anything unrecognized', () => {
    expect(normalizeLandingVariant('default')).toBe('default');
    expect(normalizeLandingVariant('operator')).toBe('default');
    expect(normalizeLandingVariant(1)).toBe('default');
  });
});
