// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import AccountPanel from './AccountPanel.svelte';

const { api, setPresence } = vi.hoisted(() => ({ api: vi.fn(), setPresence: vi.fn() }));
vi.mock('$lib/api/client', () => ({
  api,
  userErrorMessage: (_: unknown, fallback: string) => fallback
}));
vi.mock('$lib/gateway/runtime.svelte', () => ({
  authenticatedGateway: { client: { setPresence } }
}));
vi.mock('$lib/platform/native', () => ({ isNativeDesktop: () => true }));
const user = {
  id: '42',
  origin_domain: 'chat.example',
  username: 'cookie',
  display_name: 'Cookie',
  avatar_hash: null,
  handle: '@cookie@chat.example',
  custom_status: 'Reading'
};
afterEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = '';
});

it('edits and clears status, persists presence, and exposes profile/account actions', async () => {
  api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/settings')) return { presence_preference: 'idle' };
    return init ? { ...user, ...JSON.parse(String(init.body)) } : user;
  });
  const component = mount(AccountPanel, { target: document.body, props: { user } });
  try {
    flushSync();
    const panel = document.querySelector('[popover]')!;
    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    await vi.waitFor(() =>
      expect(document.querySelector('.presence-picker-trigger')?.textContent).toContain('Idle')
    );
    expect(panel.querySelector('a[href="/accounts"]')).not.toBeNull();
    expect(
      panel.querySelector('a[aria-label="Change your profile picture"]')?.getAttribute('href')
    ).toBe('/settings#profile');
    flushSync(() => document.querySelector<HTMLButtonElement>('.status-bubble')!.click());
    const input = document.querySelector<HTMLInputElement>('input')!;
    flushSync(() => {
      input.value = 'Baking';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    document
      .querySelector('form')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('.status-bubble')?.textContent).toBe('Baking')
    );
    expect(api).toHaveBeenCalledWith('/users/@me', {
      method: 'PATCH',
      body: JSON.stringify({ custom_status: 'Baking' })
    });
    flushSync(() => document.querySelector<HTMLButtonElement>('.status-bubble')!.click());
    const clear = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent === 'Clear status'
    )!;
    clear.click();
    await vi.waitFor(() =>
      expect(document.querySelector('.status-bubble')?.textContent).toBe('Set a custom status')
    );
    flushSync(() => document.querySelector<HTMLButtonElement>('.presence-picker-trigger')!.click());
    const invisible = Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')
    ).find((button) => button.textContent?.includes('Invisible'))!;
    invisible.click();
    await vi.waitFor(() => expect(setPresence).toHaveBeenCalledWith('invisible'));
    expect(api).toHaveBeenCalledWith('/users/@me/settings', {
      method: 'PATCH',
      body: JSON.stringify({ presence_preference: 'invisible' })
    });
  } finally {
    await unmount(component);
  }
});

it('keeps the status editor and shows an error when saving fails', async () => {
  api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (init) throw new Error('offline');
    return path.endsWith('/settings') ? { presence_preference: 'online' } : user;
  });
  const component = mount(AccountPanel, { target: document.body, props: { user } });
  try {
    flushSync();
    document
      .querySelector('[popover]')!
      .dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    await vi.waitFor(() => {
      flushSync();
      expect(api).toHaveBeenCalledTimes(2);
      expect(document.querySelector<HTMLButtonElement>('.status-bubble')?.disabled).toBe(false);
    });
    flushSync(() => document.querySelector<HTMLButtonElement>('.status-bubble')!.click());
    const save = document.querySelector<HTMLButtonElement>('.status-actions .primary-button')!;
    expect(save.disabled).toBe(true);
    const input = document.querySelector<HTMLInputElement>('input')!;
    flushSync(() => {
      input.value = 'Baking';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(save.disabled).toBe(false);
    document.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toBe(
        'Could not save your status.'
      )
    );
    expect(document.querySelector('input')).not.toBeNull();
    expect(save.disabled).toBe(false);
  } finally {
    await unmount(component);
  }
});
