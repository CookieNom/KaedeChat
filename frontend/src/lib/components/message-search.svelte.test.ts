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

it('defaults to the current channel and lets the checkbox switch to guild search', async () => {
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
      channel: { id: '7', origin_domain: 'home.example', name: 'general' } as Channel
    }
  });
  flushSync();
  const input = document.querySelector('input')!;
  flushSync(() => input.dispatchEvent(new FocusEvent('focus')));
  input.value = 'hello';
  flushSync(() => input.dispatchEvent(new Event('input', { bubbles: true })));
  const checkbox = document.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  expect(checkbox.checked).toBe(true);
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
  const resultCheckbox = document.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  flushSync(() => {
    resultCheckbox.checked = false;
    resultCheckbox.dispatchEvent(new Event('change', { bubbles: true }));
  });
  submit();
  await vi.waitFor(() => expect(api).toHaveBeenCalledTimes(2));
  expect(JSON.parse(vi.mocked(api).mock.calls[1][1]!.body as string)).toMatchObject({
    scope: 'guild',
    scope_ref: '1@home.example',
    cursor: null
  });
});
