import { describe, expect, it } from 'vitest';
import { bottomVirtualWindow } from './virtualization';

describe('bottomVirtualWindow', () => {
  it('renders the tail of a long conversation before initial bottom alignment', () => {
    expect(bottomVirtualWindow(150, 760, 76, 24)).toEqual({ start: 92, end: 150 });
  });

  it('keeps short and empty conversations in bounds', () => {
    expect(bottomVirtualWindow(12, 760, 76, 24)).toEqual({ start: 0, end: 12 });
    expect(bottomVirtualWindow(0, 760, 76, 24)).toEqual({ start: 0, end: 0 });
  });
});
