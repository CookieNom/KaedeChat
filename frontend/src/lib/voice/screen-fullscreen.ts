import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';

// Fullscreen the host window on desktop and the call element in a browser.
export async function showScreenFullscreen(panel: HTMLElement, signal: AbortSignal) {
  const native = isNativeDesktop();
  let restoreWindow = false;
  let usingPopover = false;
  let finish!: () => void;
  const dismissed = new Promise<void>((resolve) => (finish = resolve));
  const background: HTMLElement[] = [];
  const fullscreenChanged = () => {
    if (document.fullscreenElement !== panel) finish();
  };
  const escape = (event: KeyboardEvent) => {
    if (
      event.key === 'Escape' &&
      !panel.querySelector('dialog[open], .audio-menu [aria-expanded="true"]')
    )
      finish();
  };
  signal.addEventListener('abort', finish, { once: true });
  try {
    if (native) {
      const alreadyFullscreen = await nativeInvoke<boolean>('plugin:window|is_fullscreen');
      if (signal.aborted) return;
      // The top layer escapes the chat pane's clipping and resize constraints.
      // Older WebKit versions use the same fixed call panel without popover support.
      panel.dataset.windowFullscreen = '';
      if (typeof panel.showPopover === 'function') {
        panel.popover = 'manual';
        usingPopover = true;
        panel.showPopover();
      }
      // Tab and assistive navigation should stay in the visible call.
      for (
        let node = panel;
        node.parentElement && node !== document.body;
        node = node.parentElement
      ) {
        for (const sibling of node.parentElement.children) {
          if (sibling instanceof HTMLElement && sibling !== node && !sibling.inert) {
            sibling.inert = true;
            background.push(sibling);
          }
        }
      }
      window.addEventListener('keydown', escape);
      if (!alreadyFullscreen) {
        await nativeInvoke('plugin:window|set_fullscreen', { value: true });
        restoreWindow = true;
      }
    } else {
      document.addEventListener('fullscreenchange', fullscreenChanged);
      await panel.requestFullscreen();
    }
    await dismissed;
  } finally {
    signal.removeEventListener('abort', finish);
    window.removeEventListener('keydown', escape);
    document.removeEventListener('fullscreenchange', fullscreenChanged);
    for (const sibling of background) sibling.inert = false;
    delete panel.dataset.windowFullscreen;
    if (usingPopover) {
      if (panel.matches(':popover-open')) panel.hidePopover();
      panel.removeAttribute('popover');
    } else if (document.fullscreenElement === panel) {
      await document.exitFullscreen();
    }
    if (restoreWindow) await nativeInvoke('plugin:window|set_fullscreen', { value: false });
  }
}
