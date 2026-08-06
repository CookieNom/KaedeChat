export interface VirtualWindow {
  start: number;
  end: number;
}

export function bottomVirtualWindow(
  itemCount: number,
  viewportHeight: number,
  estimatedItemHeight: number,
  overscan: number
): VirtualWindow {
  if (itemCount <= 0) return { start: 0, end: 0 };
  const safeHeight = Math.max(1, estimatedItemHeight);
  const visibleItems = Math.max(1, Math.ceil(viewportHeight / safeHeight));
  return {
    start: Math.max(0, itemCount - visibleItems - overscan * 2),
    end: itemCount
  };
}
