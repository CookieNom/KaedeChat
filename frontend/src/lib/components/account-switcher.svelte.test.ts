// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import Accounts from '../../routes/(auth)/accounts/+page.svelte';
const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock('$lib/platform/native', () => ({
  isNativeDesktop: () => true,
  initializeNativeInstance: async () => ({}),
  nativeInvoke: invoke
}));
vi.mock('$lib/api/client', () => ({
  userErrorMessage: (_: unknown, fallback: string) => fallback
}));

it('selects exact saved accounts on the same server and retains them after a failed switch', async () => {
  const accounts = [
    { account_key: 'alice@chat.example', instance: 'chat.example', label: 'Alice' },
    { account_key: 'bob@chat.example', instance: 'chat.example', label: 'Bob' }
  ];
  invoke.mockImplementation(async (command: string) => {
    if (command === 'native_saved_accounts') return { accounts, active: accounts[0].account_key };
    if (command === 'native_switch_account') throw new Error('offline');
    return undefined;
  });
  const component = mount(Accounts, { target: document.body });
  try {
    await vi.waitFor(() => expect(document.querySelectorAll('.account')).toHaveLength(2));
    const alice = document.querySelectorAll('.account')[0];
    expect(alice.querySelectorAll('button')).toHaveLength(1);
    document.querySelectorAll<HTMLButtonElement>('.account > button:first-child')[1].click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(
        'Could not switch accounts'
      )
    );
    expect(invoke).toHaveBeenCalledWith('native_switch_account', {
      accountKey: 'bob@chat.example'
    });
    expect(document.querySelectorAll('.account')).toHaveLength(2);
    expect(document.querySelector('a[href="/login?add-account=1"]')).not.toBeNull();
    flushSync(() =>
      document
        .querySelector<HTMLButtonElement>('[aria-label="Remove saved sign-in for Bob"]')!
        .click()
    );
    await vi.waitFor(() =>
      expect(invoke).toHaveBeenCalledWith('native_forget_account', {
        accountKey: 'bob@chat.example'
      })
    );
  } finally {
    await unmount(component);
    vi.clearAllMocks();
  }
});
