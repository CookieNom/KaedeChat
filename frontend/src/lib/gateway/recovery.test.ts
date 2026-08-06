import { describe, expect, it } from 'vitest';

import { DispatchReplayBuffer } from './recovery';

describe('gateway recovery buffering', () => {
  it('replays each buffered dispatch exactly once', () => {
    const buffer = new DispatchReplayBuffer<number>();
    const batch = buffer.begin();

    expect(buffer.push(1)).toBe(true);
    expect(buffer.push(2)).toBe(true);
    expect(buffer.finish(batch)).toEqual([1, 2]);
    expect(buffer.finish(batch)).toBeNull();
    expect(buffer.push(3)).toBe(false);
  });

  it('carries unapplied dispatches into a superseding snapshot', () => {
    const buffer = new DispatchReplayBuffer<number>();
    const stale = buffer.begin();
    buffer.push(1);
    const current = buffer.begin();
    buffer.push(2);

    expect(buffer.finish(stale)).toBeNull();
    expect(buffer.finish(current)).toEqual([1, 2]);
  });
});
