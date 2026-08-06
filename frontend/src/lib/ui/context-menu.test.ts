import { describe, expect, it } from 'vitest';
import { contextMenuPosition } from './context-menu';

describe('context menu positioning', () => {
  it('opens beside the pointer when there is room', () => {
    expect(contextMenuPosition(100, 120, 220, 180, 1280, 720)).toEqual({
      left: 104,
      top: 124
    });
  });

  it('opens above and to the left near the bottom-right edge', () => {
    expect(contextMenuPosition(1260, 700, 220, 180, 1280, 720)).toEqual({
      left: 1036,
      top: 516
    });
  });

  it('keeps oversized menus inside the viewport margin', () => {
    expect(contextMenuPosition(5, 5, 400, 300, 320, 240)).toEqual({
      left: 8,
      top: 8
    });
  });
});
