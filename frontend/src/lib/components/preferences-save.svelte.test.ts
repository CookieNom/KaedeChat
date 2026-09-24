// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import Settings from '../../routes/(app)/settings/+page.svelte';
import { desktopThemes } from '$lib/ui/desktop-themes';
import { applyLocale } from '$lib/ui/locale';

const network = vi.hoisted(() => ({ api: vi.fn(), native: true }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
vi.mock('$lib/auth/config', () => ({
  loadAuthConfiguration: vi.fn().mockResolvedValue({ password_recovery_enabled: false })
}));
vi.mock('$lib/e2ee/client', () => ({
  initializeE2EE: vi.fn().mockRejectedValue(new Error('unavailable'))
}));
vi.mock('$lib/platform/native', async (original) => ({
  ...(await original<typeof import('$lib/platform/native')>()),
  isNativeDesktop: () => network.native,
  nativeInvoke: vi.fn(async (command: string) => {
    if (command === 'native_audio_devices')
      return { inputs: [], outputs: [], cameras: [], screens: [] };
    return {};
  })
}));
let component: ReturnType<typeof mount> | undefined;
let saved: Record<string, unknown>;
const save = (section = 'appearance') =>
  document.querySelector<HTMLButtonElement>(`#${section} .form-actions button`)!;
function choose(selector: string, value: string) {
  const el = document.querySelector<HTMLSelectElement>(selector)!;
  const query = el.querySelector.bind(el);
  vi.spyOn(el, 'querySelector').mockImplementation((selector: string) =>
    selector === ':checked'
      ? (Array.from(el.options).find((option) => option.value === el.value) ?? null)
      : query(selector)
  );
  flushSync(() => {
    el.value = value;
    for (const option of el.options) option.selected = option.value === value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  });
}
function toggle(selector: string) {
  flushSync(() => document.querySelector<HTMLInputElement>(selector)!.click());
}
async function render() {
  component = mount(Settings, { target: document.body });
  flushSync();
  await vi.waitFor(() => expect(save()).not.toBeNull());
}
beforeEach(() => {
  network.native = true;
  localStorage.clear();
  applyLocale('en');
  desktopThemes.set({
    themes: [
      {
        id: 'cyberpunk-midnight.theme.css',
        name: 'Cyberpunk Midnight',
        description: '',
        base: 'dark'
      }
    ],
    selected: '',
    error: '',
    missing: false,
    skipped: []
  });
  saved = {
    locale: 'en-US',
    theme: 'system',
    dm_privacy: 'shared_guild',
    share_locale_with_bots: true,
    age_restricted_dm_commands_enabled: false,
    notification_settings: {}
  };
  network.api.mockReset().mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === '/users/@me/settings') {
      if (options?.method === 'PATCH') saved = { ...saved, ...JSON.parse(String(options.body)) };
      return structuredClone(saved);
    }
    if (path === '/users/@me')
      return {
        id: '1',
        origin_domain: 'chat.example',
        username: 'User',
        handle: '@user@chat.example',
        age_assurance_state: 'adult'
      };
    if (path === '/users/@me/content-deletion') return { status: 'none' };
    if (path === '/e2ee/devices') return { devices: [] };
    return [];
  });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = undefined;
  document.body.replaceChildren();
  applyLocale('en');
});

it('stages Cyberpunk Midnight, disables reverted edits, and persists only when Save is clicked', async () => {
  await render();
  expect(save().disabled).toBe(true);
  const selector = '.desktop-theme-settings select';
  choose(selector, 'cyberpunk-midnight.theme.css');
  expect(save().disabled).toBe(false);
  expect(get(desktopThemes).selected).toBe('');
  expect(localStorage.getItem('kaede.desktop-theme')).toBeNull();
  choose(selector, '');
  expect(save().disabled).toBe(true);
  choose(selector, 'cyberpunk-midnight.theme.css');
  network.api.mockRejectedValueOnce(new Error('offline'));
  flushSync(() => save().click());
  await vi.waitFor(() => expect(save().disabled).toBe(false));
  expect(get(desktopThemes).selected).toBe('');
  expect(localStorage.getItem('kaede.desktop-theme')).toBeNull();
  flushSync(() => save().click());
  expect(save().disabled).toBe(true);
  await vi.waitFor(() =>
    expect(document.querySelector<HTMLSelectElement>(selector)!.disabled).toBe(false)
  );
  expect(get(desktopThemes).selected).toBe('cyberpunk-midnight.theme.css');
  expect(save().disabled).toBe(true);
  expect(localStorage.getItem('kaede.desktop-theme')).toBe('cyberpunk-midnight.theme.css');
  expect(document.querySelector('.settings-toast')?.textContent).toContain('saved');
  choose(selector, '');
  expect(save().disabled).toBe(false);
});

it('tracks account theme and language changes on web and keeps failed saves retryable', async () => {
  network.native = false;
  await render();
  expect(save().disabled).toBe(true);
  toggle('#appearance input[value="dark"]');
  expect(save().disabled).toBe(false);
  expect(saved.theme).toBe('system');
  toggle('#appearance input[value="system"]');
  expect(save().disabled).toBe(true);
  choose('#appearance select', 'system');
  expect(save().disabled).toBe(false);
  network.api.mockRejectedValueOnce(new Error('offline'));
  flushSync(() => save().click());
  await vi.waitFor(() => expect(document.body.textContent).toContain('Could not reach the server'));
  expect(save().disabled).toBe(false);
  flushSync(() => save().click());
  await vi.waitFor(() =>
    expect(document.querySelector<HTMLSelectElement>('#appearance select')!.disabled).toBe(false)
  );
  expect(saved.locale).toBe('system');
  expect(save().disabled).toBe(true);
});

it('preserves appearance and privacy drafts when immediate settings or another form are saved', async () => {
  await render();
  choose('.desktop-theme-settings select', 'cyberpunk-midnight.theme.css');
  toggle('#appearance input[type="checkbox"]');
  choose('#privacy select', 'friends');
  for (const selector of [
    '#advanced input[type="checkbox"]',
    '#accessibility input[type="checkbox"]'
  ]) {
    toggle(selector);
    await vi.waitFor(() =>
      expect(document.querySelector<HTMLInputElement>(selector)!.disabled).toBe(false)
    );
    expect(save().disabled).toBe(false);
    expect(save('privacy').disabled).toBe(false);
    expect(
      document.querySelector<HTMLInputElement>('#appearance input[type="checkbox"]')!.checked
    ).toBe(true);
  }
  flushSync(() => save('privacy').click());
  await vi.waitFor(() => expect(save().disabled).toBe(false));
  expect(saved.dm_privacy).toBe('friends');
  expect(save('privacy').disabled).toBe(true);
  expect(save().disabled).toBe(false);
  expect(saved.age_restricted_dm_commands_enabled).toBe(false);
  expect(get(desktopThemes).selected).toBe('');
  flushSync(() => save().click());
  await vi.waitFor(() =>
    expect(
      document.querySelector<HTMLInputElement>('#appearance input[type="checkbox"]')!.disabled
    ).toBe(false)
  );
  expect(saved.age_restricted_dm_commands_enabled).toBe(true);
  expect(save().disabled).toBe(true);
});
