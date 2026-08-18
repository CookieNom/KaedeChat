import { describe, expect, it } from 'vitest';
import { hasAdminCapability } from './capabilities';

describe('hasAdminCapability', () => {
  it('accepts an explicitly granted capability', () => {
    expect(hasAdminCapability(['admin.read', 'reports.read'], 'reports.read')).toBe(true);
  });

  it('treats the owner wildcard as every capability', () => {
    expect(hasAdminCapability(['*'], 'reports.read')).toBe(true);
    expect(hasAdminCapability(['*'], 'future.capability')).toBe(true);
  });

  it('rejects missing capabilities and an unavailable identity', () => {
    expect(hasAdminCapability(['admin.read'], 'reports.read')).toBe(false);
    expect(hasAdminCapability(null, 'admin.read')).toBe(false);
  });
});
