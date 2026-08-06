export interface ContextMenuPosition {
  left: number;
  top: number;
}

export function contextMenuPosition(
  pointerX: number,
  pointerY: number,
  menuWidth: number,
  menuHeight: number,
  viewportWidth: number,
  viewportHeight: number,
  margin = 8,
  gap = 4
): ContextMenuPosition {
  const availableRight = viewportWidth - margin;
  const availableBottom = viewportHeight - margin;
  const maximumLeft = Math.max(margin, availableRight - menuWidth);
  const maximumTop = Math.max(margin, availableBottom - menuHeight);
  const preferredLeft =
    pointerX + gap + menuWidth <= availableRight ? pointerX + gap : pointerX - gap - menuWidth;
  const preferredTop =
    pointerY + gap + menuHeight <= availableBottom ? pointerY + gap : pointerY - gap - menuHeight;

  return {
    left: Math.round(Math.min(Math.max(margin, preferredLeft), maximumLeft)),
    top: Math.round(Math.min(Math.max(margin, preferredTop), maximumTop))
  };
}

export function placeContextMenu(element: HTMLElement, pointerX: number, pointerY: number): void {
  const bounds = element.getBoundingClientRect();
  const position = contextMenuPosition(
    pointerX,
    pointerY,
    bounds.width,
    bounds.height,
    window.innerWidth,
    window.innerHeight
  );
  // Property-level CSSOM updates remain compatible with the strict style-src
  // policy; no style attribute is emitted into the server-rendered markup.
  element.style.setProperty('left', `${position.left}px`);
  element.style.setProperty('top', `${position.top}px`);
  element.style.setProperty('right', 'auto');
  element.style.setProperty('bottom', 'auto');
}
