export interface AutosizeOptions {
  value: string;
  maxHeight?: number;
}

export function autosizeTextarea(
  node: HTMLTextAreaElement,
  options: AutosizeOptions
): { update: (next: AutosizeOptions) => void; destroy: () => void } {
  let maxHeight = options.maxHeight ?? 180;

  function resize() {
    node.style.height = 'auto';
    const nextHeight = Math.min(node.scrollHeight, maxHeight);
    node.style.height = `${nextHeight}px`;
    node.style.overflowY = node.scrollHeight > maxHeight ? 'auto' : 'hidden';
  }

  node.addEventListener('input', resize);
  queueMicrotask(resize);
  return {
    update(next) {
      maxHeight = next.maxHeight ?? 180;
      resize();
    },
    destroy() {
      node.removeEventListener('input', resize);
      node.style.removeProperty('height');
      node.style.removeProperty('overflow-y');
    }
  };
}
