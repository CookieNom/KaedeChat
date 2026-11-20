// @vitest-environment happy-dom
import { flushSync, mount, tick, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import Consent from './DMContinuationConsent.svelte';

const { api } = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, userErrorMessage: String }));

it('ignores a stale poll and lets an approved conversation finish once the call ends', async () => {
  const room = '1@example.org';
  const account = '2@example.org';
  const participants = ['3@example.org', account];
  const request = { request_id: 'r'.repeat(43), context: { participants } };
  const pending = {
    request: { ...request, status: 'pending', approved_by: participants.slice(0, 1) }
  };
  const late = Promise.withResolvers<typeof pending>();
  const approved = { request: { ...request, status: 'approved', approved_by: participants } };
  api
    .mockResolvedValueOnce(pending)
    .mockReturnValueOnce(late.promise)
    .mockResolvedValueOnce(approved)
    .mockResolvedValueOnce({ channel_ref: '4@example.org' });
  const onContinue = vi.fn();
  const view = $state({ callActive: true });
  vi.useFakeTimers();
  const component = mount(Consent, {
    target: document.body,
    props: {
      room,
      account,
      onContinue,
      get callActive() {
        return view.callActive;
      }
    }
  });
  const button = (name: string) =>
    [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === name)!;
  try {
    flushSync();
    await vi.advanceTimersByTimeAsync(5000);
    document.querySelector<HTMLButtonElement>('details button.primary')!.click();
    document.querySelector<HTMLButtonElement>('dialog button.primary')!.click();
    await tick();
    late.resolve(pending);
    await tick();
    expect(api).toHaveBeenCalledTimes(3);
    expect(button('Finish turning off encryption').disabled).toBe(true);
    view.callActive = false;
    flushSync();
    button('Finish turning off encryption').click();
    await tick();
    expect(onContinue).toHaveBeenCalledExactlyOnceWith('4@example.org');
  } finally {
    await unmount(component);
    vi.useRealTimers();
  }
});
