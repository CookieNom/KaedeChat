import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ReadAcknowledgementQueue } from './read-ack';

interface Target {
  id: string;
  origin_domain: string;
  channel_id: string;
  channel_domain: string;
}

const first: Target = {
  id: '1',
  origin_domain: 'home.test',
  channel_id: '10',
  channel_domain: 'home.test'
};
const latest: Target = { ...first, id: '2' };

describe('ReadAcknowledgementQueue', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('keeps unread state until a retry succeeds and then clears the warning', async () => {
    const send = vi
      .fn<(message: Target) => Promise<void>>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockRejectedValueOnce(new Error('offline'))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(undefined);
    const acknowledged = vi.fn();
    const warningChanged = vi.fn();
    const queue = new ReadAcknowledgementQueue({ send, acknowledged, warningChanged });

    await queue.acknowledge(first);
    await vi.advanceTimersByTimeAsync(1_000);
    await vi.advanceTimersByTimeAsync(2_000);

    expect(acknowledged).not.toHaveBeenCalled();
    expect(warningChanged.mock.calls.at(-1)?.[0]).toContain('Read state may be out of date');

    await queue.retryNow();
    expect(acknowledged).toHaveBeenCalledWith(first);
    expect(warningChanged.mock.calls.at(-1)?.[0]).toBe('');
  });

  it('coalesces a newer message while an acknowledgement is in flight', async () => {
    let finish!: () => void;
    const send = vi
      .fn<(message: Target) => Promise<void>>()
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            finish = resolve;
          })
      )
      .mockResolvedValue(undefined);
    const acknowledged = vi.fn();
    const queue = new ReadAcknowledgementQueue({
      send,
      acknowledged,
      warningChanged: vi.fn()
    });

    const initial = queue.acknowledge(first);
    void queue.acknowledge(latest);
    finish();
    await initial;
    await vi.runAllTimersAsync();

    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenLastCalledWith(latest);
    expect(acknowledged).toHaveBeenCalledOnce();
    expect(acknowledged).toHaveBeenLastCalledWith(latest);
  });

  it('sends a new route acknowledgement after reset even if the old request finishes later', async () => {
    let finish!: () => void;
    const send = vi
      .fn<(message: Target) => Promise<void>>()
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            finish = resolve;
          })
      )
      .mockResolvedValue(undefined);
    const acknowledged = vi.fn();
    const queue = new ReadAcknowledgementQueue({
      send,
      acknowledged,
      warningChanged: vi.fn()
    });

    const oldRequest = queue.acknowledge(first);
    queue.reset();
    void queue.acknowledge(latest);
    finish();
    await oldRequest;
    await vi.runAllTimersAsync();

    expect(send).toHaveBeenCalledTimes(2);
    expect(acknowledged).toHaveBeenCalledOnce();
    expect(acknowledged).toHaveBeenCalledWith(latest);
  });
});
