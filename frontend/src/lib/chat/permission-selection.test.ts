import { describe, expect, it } from 'vitest';
import {
  permissionMask,
  permissionSelected,
  selectedPermissionMetadata,
  setPermissionSelected
} from './permission-selection';

describe('named application permission selection', () => {
  it('preserves exact high and unknown bits while toggling a named permission', () => {
    const futureBit = 1n << 62n;
    const current = futureBit.toString();
    const selected = setPermissionSelected(current, 8n, true);

    expect(permissionMask(selected)).toBe(futureBit | 8n);
    expect(permissionSelected(selected, 8n)).toBe(true);
    expect(permissionMask(setPermissionSelected(selected, 8n, false))).toBe(futureBit);
  });

  it('projects readable permission labels and rejects malformed masks', () => {
    expect(selectedPermissionMetadata('3').map((item) => item.label)).toEqual([
      'Create invites',
      'Kick members'
    ]);
    expect(() => permissionMask('-1')).toThrow('non-negative whole number');
    expect(() => permissionMask('1.5')).toThrow('non-negative whole number');
  });
});
