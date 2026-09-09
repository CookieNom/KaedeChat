import { describe, expect, it } from 'vitest';
import { contextMenuPosition } from './context-menu';

describe('context menu positioning', () => {
  it.each([
    {
      name: 'opens beside the pointer',
      x: 100,
      y: 120,
      width: 220,
      height: 180,
      vw: 1280,
      vh: 720,
      direction: 'after'
    },
    {
      name: 'opens above and left near the edge',
      x: 1260,
      y: 700,
      width: 220,
      height: 180,
      vw: 1280,
      vh: 720,
      direction: 'before'
    },
    {
      name: 'clamps the origin of an oversized menu',
      x: 5,
      y: 5,
      width: 400,
      height: 300,
      vw: 320,
      vh: 240,
      direction: 'clamp'
    }
  ])('$name', ({ x, y, width, height, vw, vh, direction }) => {
    const { left, top } = contextMenuPosition(x, y, width, height, vw, vh);
    expect(left).toBeGreaterThanOrEqual(0);
    expect(top).toBeGreaterThanOrEqual(0);
    expect(left).toBeLessThan(vw);
    expect(top).toBeLessThan(vh);
    if (direction === 'after') {
      expect(left).toBeGreaterThanOrEqual(x);
      expect(top).toBeGreaterThanOrEqual(y);
    } else if (direction === 'before') {
      expect(left + width).toBeLessThanOrEqual(x);
      expect(top + height).toBeLessThanOrEqual(y);
    }
    if (width <= vw && height <= vh) {
      expect(left + width).toBeLessThanOrEqual(vw);
      expect(top + height).toBeLessThanOrEqual(vh);
    }
  });
});
