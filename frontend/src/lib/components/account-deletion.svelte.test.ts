// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import AccountDeletion from './AccountDeletion.svelte';
import { ApiError } from '$lib/api/errors';

const { api, preparePassword } = vi.hoisted(() => ({ api: vi.fn(), preparePassword: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api/client')>()),
  api
}));
vi.mock('$lib/auth/password-kdf', () => ({
  loadPasswordKdfContext: vi.fn(async () => ({ version: 2 })),
  preparePassword
}));
afterEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = '';
});

it.each(['content', 'account'] as const)(
  'confirms %s deletion with derived password and MFA',
  async (action) => {
    api.mockResolvedValue({ status: 'idle' });
    preparePassword.mockResolvedValue({
      authenticationSecret: 'derived-secret',
      context: { version: 2 }
    });
    const onDeleted = vi.fn(async () => {});
    const component = mount(AccountDeletion, {
      target: document.body,
      props: { handle: 'alice@example.test', mfaEnabled: true, onDeleted }
    });
    try {
      await vi.waitFor(() => expect(api).toHaveBeenCalled());
      flushSync(() =>
        Array.from(document.querySelectorAll('button'))
          .find(
            (button) =>
              button.textContent ===
              (action === 'account' ? 'Delete account' : 'Delete all content')
          )!
          .click()
      );
      const inputs = Array.from(document.querySelectorAll('input'));
      expect(inputs).toHaveLength(3);
      const submit = document.querySelector<HTMLButtonElement>('form button')!;
      expect(submit.disabled).toBe(true);
      flushSync(() =>
        inputs.forEach((input, index) => {
          input.value = ['my password', '123456', 'DELETE'][index];
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })
      );
      expect(submit.disabled).toBe(false);
      document
        .querySelector('form')!
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      await vi.waitFor(() =>
        expect(api).toHaveBeenCalledWith(
          action === 'account' ? '/users/@me' : '/users/@me/content',
          {
            method: 'DELETE',
            body: JSON.stringify({
              password: 'derived-secret',
              password_kdf_version: 2,
              current_code: '123456'
            })
          }
        )
      );
      await vi.waitFor(() => expect(document.querySelector('form')).toBeNull());
      expect(onDeleted).toHaveBeenCalledTimes(action === 'account' ? 1 : 0);
      expect(document.body.textContent).toContain('cleanup will continue');
    } finally {
      await unmount(component);
    }
  }
);

it('disables deletion while cleanup is pending', async () => {
  api.mockResolvedValue({ status: 'pending' });
  const component = mount(AccountDeletion, {
    target: document.body,
    props: { handle: 'alice@example.test', mfaEnabled: false, onDeleted: async () => {} }
  });
  try {
    await vi.waitFor(() =>
      expect(
        Array.from(document.querySelectorAll('button')).every((button) => button.disabled)
      ).toBe(true)
    );
    expect(document.body.textContent).toContain('You can close the app');
  } finally {
    await unmount(component);
  }
});

it('shows the ownership blocker and keeps the account form available after rejection', async () => {
  const message =
    'You still own one or more guilds. Transfer ownership or delete those guilds before deleting your account.';
  api.mockImplementation(async (_path, options) => {
    if (options?.method === 'DELETE') throw new ApiError('GUILD_OWNER', message, 409);
    return { status: 'idle' };
  });
  preparePassword.mockResolvedValue({
    authenticationSecret: 'derived-secret',
    context: { version: 2 }
  });
  const onDeleted = vi.fn(async () => {});
  const component = mount(AccountDeletion, {
    target: document.body,
    props: { handle: 'alice@example.test', mfaEnabled: false, onDeleted }
  });
  try {
    await vi.waitFor(() => expect(api).toHaveBeenCalled());
    flushSync(() =>
      Array.from(document.querySelectorAll('button'))
        .find((button) => button.textContent === 'Delete account')!
        .click()
    );
    flushSync(() =>
      Array.from(document.querySelectorAll('input')).forEach((input, index) => {
        input.value = ['my password', 'DELETE'][index];
        input.dispatchEvent(new Event('input', { bubbles: true }));
      })
    );
    document
      .querySelector('form')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toBe(message)
    );
    expect(document.querySelector<HTMLButtonElement>('form button')!.disabled).toBe(false);
    expect(onDeleted).not.toHaveBeenCalled();
  } finally {
    await unmount(component);
  }
});
