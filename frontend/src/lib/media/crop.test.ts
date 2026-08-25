import { describe, expect, it } from 'vitest';
import { moveCrop, resizeCrop } from './crop';

describe('normalized crop interactions', () => {
  it('moves the crop box without letting it escape the image', () => {
    const crop = { x: 0.2, y: 0.25, width: 0.5, height: 0.4 };
    expect(moveCrop(crop, 0.2, -0.5)).toEqual({
      x: 0.4,
      y: 0,
      width: 0.5,
      height: 0.4
    });
    expect(moveCrop(crop, 1, 1)).toEqual({
      x: 0.5,
      y: 0.6,
      width: 0.5,
      height: 0.4
    });
  });

  it('resizes each axis from a corner and enforces the minimum size', () => {
    const northwest = resizeCrop({ x: 0.2, y: 0.2, width: 0.5, height: 0.5 }, 'nw', 0.2, -0.4);
    expect(northwest.x).toBeCloseTo(0.4);
    expect(northwest.y).toBeCloseTo(0);
    expect(northwest.width).toBeCloseTo(0.3);
    expect(northwest.height).toBeCloseTo(0.7);
    expect(resizeCrop({ x: 0.2, y: 0.2, width: 0.5, height: 0.5 }, 'se', -1, -1)).toEqual({
      x: 0.2,
      y: 0.2,
      width: 0.1,
      height: 0.1
    });
  });
});
