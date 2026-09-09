import { describe, expect, it } from 'vitest';
import { bottomVirtualWindow } from './virtualization';

describe('bottomVirtualWindow', () => {
  it('renders the tail of a long conversation before initial bottom alignment', () => {
    const { start, end } = bottomVirtualWindow(150, 760, 76, 24);
    expect(end).toBe(150);
    expect(start).toBeGreaterThan(0);
    expect(end - start).toBeGreaterThanOrEqual(10);
    expect(end - start).toBeLessThanOrEqual(10 + 2 * 24);
  });

  it('keeps short and empty conversations in bounds', () => {
    expect(bottomVirtualWindow(12, 760, 76, 24)).toEqual({ start: 0, end: 12 });
    expect(bottomVirtualWindow(0, 760, 76, 24)).toEqual({ start: 0, end: 0 });
  });
});
