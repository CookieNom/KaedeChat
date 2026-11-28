import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';

// Keep the call controls with the selected screen. A native webview also needs
// its host window to fill the display, even when the DOM element is fullscreen.
export async function showScreenFullscreen(panel: HTMLElement, signal: AbortSignal) {
  const native = isNativeDesktop();
  let restoreWindow = false;
  let usingPopover = false;
  const dismiss = Promise.withResolvers<void>();
  const finish = () => dismiss.resolve();
  const fullscreenChanged = () => {
    if (document.fullscreenElement !== panel) finish();
  };
  const escape = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && !panel.querySelector('dialog[open], :popover-open')) finish();
  };
  signal.addEventListener('abort', finish, { once: true });
  try {
    if (native) {
      const alreadyFullscreen = await nativeInvoke<boolean>('plugin:window|is_fullscreen');
      if (signal.aborted) return;
      // The top layer escapes the chat pane's clipping and resize constraints.
      panel.popover = 'manual';
      usingPopover = true;
      panel.showPopover();
      window.addEventListener('keydown', escape);
      if (!alreadyFullscreen) {
        await nativeInvoke('plugin:window|set_fullscreen', { value: true });
        restoreWindow = true;
      }
    } else {
      document.addEventListener('fullscreenchange', fullscreenChanged);
      await panel.requestFullscreen();
    }
    await dismiss.promise;
  } finally {
    signal.removeEventListener('abort', finish);
    window.removeEventListener('keydown', escape);
    document.removeEventListener('fullscreenchange', fullscreenChanged);
    if (usingPopover) {
      if (panel.matches(':popover-open')) panel.hidePopover();
      panel.removeAttribute('popover');
    } else if (document.fullscreenElement === panel) {
      await document.exitFullscreen();
    }
    if (restoreWindow) await nativeInvoke('plugin:window|set_fullscreen', { value: false });
  }
}
