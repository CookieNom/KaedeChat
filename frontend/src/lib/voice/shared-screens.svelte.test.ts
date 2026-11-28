// @vitest-environment happy-dom
import { Track } from 'livekit-client';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import SharedScreens from './SharedScreens.svelte';
import type { VoiceTile } from './session';

it('keeps the chosen screen as video arrives and restores the grid when its share ends', async () => {
  const screen = (name: string, local = false): VoiceTile => ({
    key: name,
    identity: name,
    name,
    local,
    source: Track.Source.ScreenShare
  });
  const local = screen('You', true);
  const maya = screen('Maya');
  const view = $state({ screens: [local, maya] });
  const panel = document.createElement('section');
  document.body.append(panel);
  Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true });
  panel.requestFullscreen = async () => {
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: panel });
  };
  document.exitFullscreen = vi.fn(async () => {
    Reflect.deleteProperty(document, 'fullscreenElement');
  });
  const component = mount(SharedScreens, {
    target: panel,
    props: {
      panel,
      get screens() {
        return view.screens;
      },
      cameras: [],
      cameraTile: createRawSnippet(() => ({ render: () => '<span></span>' })),
      listeningVolume: () => 1,
      setVolume: vi.fn(),
      onError: vi.fn()
    }
  });
  const button = (label: string, within: Element = panel) =>
    within.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
  try {
    flushSync();
    flushSync(() => button('Focus Maya’s screen').click());
    const chosen = panel.querySelector('article[aria-label="Maya’s screen"]')!;
    flushSync(() => {
      view.screens = [
        { ...maya, nativeFrame: { width: 1, height: 1, rgba: new Uint8ClampedArray(4) } },
        local
      ];
    });
    expect(chosen.querySelector('canvas')).toBeTruthy();
    expect(button('Show all screens', chosen)).toBeTruthy();
    flushSync(() => button('Full screen', chosen).click());
    expect(document.fullscreenElement).toBe(panel);
    expect(panel.querySelectorAll('article')).toHaveLength(1);
    expect(button('Exit full screen', chosen)).toBeTruthy();

    flushSync(() => (view.screens = [local, screen('Aki')]));
    await vi.waitFor(() => expect(panel.querySelectorAll('article')).toHaveLength(2));
    expect(document.exitFullscreen).toHaveBeenCalledOnce();
    expect(button('Focus Your screen')).toBeTruthy();
    expect(button('Focus Aki’s screen')).toBeTruthy();
    expect(button('Show all screens')).toBeNull();
  } finally {
    await unmount(component);
    panel.remove();
    Reflect.deleteProperty(document, 'fullscreenEnabled');
    Reflect.deleteProperty(document, 'fullscreenElement');
    Reflect.deleteProperty(document, 'exitFullscreen');
  }
});
