// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import Login from '../../routes/(auth)/login/+page.svelte';
const calls = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$app/state', () => ({ page: { url: new URL('https://chat.example/login') } }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: calls.api
}));
vi.mock('$lib/auth/config', () => ({
  loadAuthConfiguration: async () => ({
    password_recovery_enabled: false,
    turnstile: { enabled: false, site_key: null }
  })
}));
vi.mock('$lib/auth/password-kdf', () => ({
  loadPasswordKdfContext: async () => ({ version: 2 }),
  preparePassword: async () => ({
    authenticationSecret: 'prepared',
    context: { version: 2 },
    vaultKey: {}
  }),
  savePreparedVaultKey: async () => {}
}));
vi.mock('$lib/platform/native', () => ({
  initializeNativeInstance: async () => {},
  isNativeDesktop: () => false,
  storedNativeInstance: () => '',
  setNativeInstance: async () => ''
}));
it('submits MFA after the instance field unmounts and permits a code retry', async () => {
  calls.api.mockImplementation(async (path) => {
    if (path === '/auth/login') return { mfa_required: true, mfa_ticket: 'ticket' };
    throw new Error('Wrong code');
  });
  const component = mount(Login, { target: document.body });
  const fill = (selector: string, value: string) =>
    flushSync(() => {
      const input = document.querySelector<HTMLInputElement>(selector)!;
      input.value = value;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  const submit = () =>
    flushSync(() =>
      document
        .querySelector('form')!
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    );
  try {
    fill('[autocomplete="username"]', 'alice');
    fill('[autocomplete="current-password"]', 'password');
    submit();
    await vi.waitFor(() =>
      expect(document.querySelector('[autocomplete="one-time-code"]')).not.toBeNull()
    );
    for (const code of ['123456', '654321']) {
      fill('[autocomplete="one-time-code"]', code);
      submit();
      await vi.waitFor(() =>
        expect(calls.api).toHaveBeenCalledWith('/auth/mfa', {
          method: 'POST',
          body: JSON.stringify({ ticket: 'ticket', code })
        })
      );
      await vi.waitFor(() =>
        expect(document.querySelector<HTMLButtonElement>('form button')!.disabled).toBe(false)
      );
    }
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});
