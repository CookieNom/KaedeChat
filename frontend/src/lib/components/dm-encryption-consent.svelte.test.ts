// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import DMEncryptionConsent from './DMEncryptionConsent.svelte';

const { api } = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api }));
afterEach(() => vi.resetAllMocks());

const pending = {
  request_id: 'r'.repeat(43),
  requester: { id: '1', domain: 'home.example' },
  requester_name: 'Alice',
  status: 'pending'
};

it.each(['agree', 'disagree'])(
  'records %s once and enables encryption only after agreement',
  async (action) => {
    api.mockResolvedValueOnce({ request: pending });
    let respond!: (value: unknown) => void;
    api.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          respond = resolve;
        })
    );
    const onEnable = vi.fn(async () => {});
    const target = document.createElement('div');
    document.body.append(target);
    const component = mount(DMEncryptionConsent, {
      target,
      props: {
        channelRef: '10@home.example',
        userRef: '2@home.example',
        onEnable,
        onStatus: vi.fn()
      }
    });
    try {
      flushSync();
      await vi.waitFor(() => expect(target.textContent).toContain('Alice requested'));
      expect(target.textContent).toContain('cannot be turned off');
      const buttons = target.querySelectorAll('button');
      const button = buttons[action === 'agree' ? 0 : 1];
      flushSync(() => button.click());
      expect(button.disabled).toBe(true);
      flushSync(() => button.click());
      expect(api).toHaveBeenCalledTimes(2);
      expect(onEnable).not.toHaveBeenCalled();
      expect(JSON.parse(api.mock.calls[1][1].body)).toEqual({
        action,
        request_id: pending.request_id
      });
      respond({ request: { ...pending, status: action === 'agree' ? 'approved' : 'declined' } });
      await vi.waitFor(() =>
        expect(target.textContent).toContain(
          action === 'agree' ? 'You both agreed' : 'request was declined'
        )
      );
      expect(onEnable).toHaveBeenCalledTimes(action === 'agree' ? 1 : 0);
    } finally {
      await unmount(component);
      target.remove();
    }
  }
);

it('shows a waiting status without consent buttons to the requester', async () => {
  api.mockResolvedValue({ request: pending });
  const target = document.createElement('div');
  const component = mount(DMEncryptionConsent, {
    target,
    props: {
      channelRef: '10@home.example',
      userRef: '1@home.example',
      onEnable: vi.fn(),
      onStatus: vi.fn()
    }
  });
  try {
    flushSync();
    await vi.waitFor(() =>
      expect(target.textContent).toContain('Waiting for the other participant')
    );
    expect(target.querySelector('button')).toBeNull();
  } finally {
    await unmount(component);
  }
});

it('does not activate a different conversation if the request card closes while agreeing', async () => {
  api.mockResolvedValueOnce({ request: pending });
  let respond!: (value: unknown) => void;
  api.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        respond = resolve;
      })
  );
  const onEnable = vi.fn(async () => {});
  const target = document.createElement('div');
  const component = mount(DMEncryptionConsent, {
    target,
    props: {
      channelRef: '10@home.example',
      userRef: '2@home.example',
      onEnable,
      onStatus: vi.fn()
    }
  });
  flushSync();
  await vi.waitFor(() => expect(target.querySelector('button')).not.toBeNull());
  flushSync(() => target.querySelector('button')!.click());
  await unmount(component);
  respond({ request: { ...pending, status: 'approved' } });
  await Promise.resolve();
  expect(onEnable).not.toHaveBeenCalled();
});
