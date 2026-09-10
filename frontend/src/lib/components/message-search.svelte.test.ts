// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MessageSearch from './MessageSearch.svelte';
import { api } from '$lib/api/client';
import type { Channel } from '$lib/chat/types';

vi.mock('$lib/api/client', () => ({ api: vi.fn(), userErrorMessage: () => 'Failed' }));
vi.mock('$lib/auth/config', () => ({ loadAuthConfiguration: vi.fn() }));
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});

it('defaults to the current channel and allows another channel or all channels', async () => {
  vi.mocked(api).mockResolvedValue({
    results: [],
    next_cursor: null,
    coverage: {},
    encrypted_channel_refs: []
  });
  component = mount(MessageSearch, {
    target: document.body,
    props: {
      open: false,
      placement: 'header',
      scope: 'guild',
      scopeRef: '1@home.example',
      accountRef: null,
      channels: Array.from(
        { length: 600 },
        (_, i) =>
          ({ id: String(i + 100), origin_domain: 'home.example', name: `channel-${i}` }) as Channel
      ),
      channel: { id: '7', origin_domain: 'home.example', name: 'general' } as Channel
    }
  });
  flushSync();
  const input = document.querySelector('input')!;
  flushSync(() => input.dispatchEvent(new FocusEvent('focus')));
  input.value = 'hello';
  flushSync(() => input.dispatchEvent(new Event('input', { bubbles: true })));
  expect(document.querySelector('.scope-choice')!.textContent).toContain('general');
  const submit = () =>
    flushSync(() =>
      document.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
    );
  submit();
  await vi.waitFor(() => expect(api).toHaveBeenCalledTimes(1));
  expect(JSON.parse(vi.mocked(api).mock.calls[0][1]!.body as string)).toMatchObject({
    scope: 'channel',
    scope_ref: '7@home.example'
  });
  flushSync();
  const chooseChannel = (name: string) => {
    flushSync(() =>
      document.querySelector<HTMLButtonElement>('[aria-label="Change search channel"]')!.click()
    );
    const input = document.querySelector<HTMLInputElement>('[aria-label="Search channels"]')!;
    flushSync(() => {
      input.value = name;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const options = document.querySelectorAll('[role="option"]');
    expect([...options].filter((item) => item.textContent?.includes('channel-'))).toHaveLength(1);
    flushSync(() =>
      input.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
      )
    );
  };
  chooseChannel('channel-599');
  submit();
  await vi.waitFor(() => expect(api).toHaveBeenCalledTimes(2));
  expect(JSON.parse(vi.mocked(api).mock.calls[1][1]!.body as string)).toMatchObject({
    scope: 'channel',
    scope_ref: '699@home.example',
    cursor: null
  });
  flushSync();
  flushSync(() => document.querySelector<HTMLButtonElement>('.scope-all')!.click());
  submit();
  await vi.waitFor(() => expect(api).toHaveBeenCalledTimes(3));
  expect(JSON.parse(vi.mocked(api).mock.calls[2][1]!.body as string)).toMatchObject({
    scope: 'guild',
    scope_ref: '1@home.example',
    cursor: null
  });
});
