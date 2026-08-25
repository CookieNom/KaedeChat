export interface NormalizedCrop {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type CropCorner = 'nw' | 'ne' | 'sw' | 'se';

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.min(maximum, Math.max(minimum, value));

export function moveCrop(crop: NormalizedCrop, dx: number, dy: number): NormalizedCrop {
  return {
    ...crop,
    x: clamp(crop.x + dx, 0, 1 - crop.width),
    y: clamp(crop.y + dy, 0, 1 - crop.height)
  };
}

export function resizeCrop(
  crop: NormalizedCrop,
  corner: CropCorner,
  dx: number,
  dy: number,
  minimumSize = 0.1
): NormalizedCrop {
  let { x, y, width, height } = crop;
  if (corner.includes('w')) {
    const right = x + width;
    x = clamp(x + dx, 0, right - minimumSize);
    width = right - x;
  } else {
    width = clamp(width + dx, minimumSize, 1 - x);
  }
  if (corner.includes('n')) {
    const bottom = y + height;
    y = clamp(y + dy, 0, bottom - minimumSize);
    height = bottom - y;
  } else {
    height = clamp(height + dy, minimumSize, 1 - y);
  }
  return { x, y, width, height };
}
