export const DISMISS_FLOATING_LAYERS_EVENT = 'kaede-dismiss-floating-layers';

/** Close any transient popover/menu before opening another one. */
export function dismissFloatingLayers(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(DISMISS_FLOATING_LAYERS_EVENT));
  }
}
