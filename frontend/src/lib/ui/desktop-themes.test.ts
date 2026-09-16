// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  localStorage.clear();
  document.head.innerHTML = '';
});

afterEach(() => {
  vi.useRealTimers();
  delete window.__TAURI__;
});

it('reloads additions and edits, preserves the local choice across account updates, and restores defaults', async () => {
  let css = ':root { --accent: cyan; }';
  let themes = [{ id: 'Night.theme.css', name: 'Night', description: '', base: 'dark' }];
  const invoke = vi.fn(async () => ({ themes, css: themes.length ? css : null, skipped: [] }));
  window.__TAURI__ = {
    core: { invoke: invoke as NonNullable<typeof window.__TAURI__>['core']['invoke'] }
  };
  const { startDesktopThemes, selectDesktopTheme, desktopThemes } =
    await import('./desktop-themes');
  const { applyTheme } = await import('./theme');
  const stop = startDesktopThemes();
  try {
    await vi.advanceTimersByTimeAsync(0);
    expect(get(desktopThemes).themes).toHaveLength(1);
    selectDesktopTheme('Night.theme.css');
    await vi.advanceTimersByTimeAsync(0);
    applyTheme('light');
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(localStorage.getItem('kaede.theme')).toBe('light');
    expect(document.getElementById('kaede-desktop-theme')?.textContent).toBe(css);
    css = ':root { --accent: pink; }';
    themes.push({ id: 'New.theme.css', name: 'New', description: '', base: 'light' });
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(desktopThemes).themes).toHaveLength(2);
    expect(document.getElementById('kaede-desktop-theme')?.textContent).toBe(css);
    themes = [];
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(desktopThemes).missing).toBe(true);
    expect(document.getElementById('kaede-desktop-theme')).toBeNull();
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(localStorage.getItem('kaede.desktop-theme-cache')).toBeNull();
    selectDesktopTheme('');
    await vi.advanceTimersByTimeAsync(0);
    expect(get(desktopThemes).missing).toBe(false);
  } finally {
    stop();
  }
  const calls = invoke.mock.calls.length;
  await vi.advanceTimersByTimeAsync(4000);
  expect(invoke).toHaveBeenCalledTimes(calls);
});

it('restores the cache immediately, follows selections from another window, and ignores stale requests', async () => {
  localStorage.setItem('kaede.desktop-theme', 'Night.theme.css');
  localStorage.setItem(
    'kaede.desktop-theme-cache',
    JSON.stringify({ id: 'Night.theme.css', base: 'dark', css: ':root { --accent: cyan; }' })
  );
  let resolveOld: (value: unknown) => void = () => {};
  const invoke = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        })
    )
    .mockResolvedValue({
      themes: [{ id: 'Light.theme.css', name: 'Light', base: 'light', description: '' }],
      css: ':root { --accent: green; }',
      skipped: []
    });
  window.__TAURI__ = { core: { invoke } };
  const { startDesktopThemes, desktopThemes } = await import('./desktop-themes');
  const stop = startDesktopThemes();
  try {
    expect(document.documentElement.dataset.theme).toBe('dark');
    window.dispatchEvent(
      new StorageEvent('storage', { key: 'kaede.desktop-theme', newValue: 'Light.theme.css' })
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(document.documentElement.dataset.theme).toBe('light');
    resolveOld({ themes: [], css: null, skipped: [] });
    await vi.advanceTimersByTimeAsync(0);
    expect(get(desktopThemes).selected).toBe('Light.theme.css');
    expect(document.getElementById('kaede-desktop-theme')?.textContent).toContain('green');
  } finally {
    stop();
  }
});

it('does not load desktop themes in a browser', async () => {
  const { startDesktopThemes } = await import('./desktop-themes');
  startDesktopThemes()();
  expect(vi.getTimerCount()).toBe(0);
  expect(document.getElementById('kaede-desktop-theme')).toBeNull();
});
