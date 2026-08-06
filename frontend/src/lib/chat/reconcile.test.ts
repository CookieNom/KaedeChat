import { describe, expect, it } from 'vitest';
import type { Message } from './types';
import {
  applyMessageDeliveryUpdate,
  failPendingMessage,
  LoadFence,
  mergeMessageSnapshot,
  reconcileMessage
} from './reconcile';

function message(id: string, nonce: string | null = null, pending = false): Message {
  return {
    id,
    origin_domain: id.startsWith('pending-') ? '' : 'chat.example',
    channel_id: '1',
    channel_domain: 'chat.example',
    author_id: '2',
    author_domain: 'chat.example',
    author: null,
    content: id,
    message_type: 0,
    flags: 0,
    client_nonce: nonce,
    referenced_message_id: null,
    referenced_message_domain: null,
    mention_user_refs: [],
    edited_at: null,
    deleted_at: null,
    created_at: '2026-07-20T00:00:00Z',
    pending
  };
}

describe('chat completion races', () => {
  it('reconciles a gateway echo with an optimistic REST send exactly once', () => {
    const optimistic = message('pending-a', 'a', true);
    const saved = message('12', 'a');
    const afterGateway = reconcileMessage([message('10'), optimistic], saved);
    const afterRest = reconcileMessage(afterGateway, saved);
    expect(afterRest.map((item) => item.id)).toEqual(['10', '12']);
  });

  it('does not mark a gateway-confirmed message failed when the REST response is lost', () => {
    const optimistic = message('pending-a', 'a', true);
    const confirmed = message('12', 'a');
    const afterGateway = reconcileMessage([optimistic], confirmed);

    expect(failPendingMessage(afterGateway, 'a')).toEqual([confirmed]);
    expect(failPendingMessage([optimistic], 'a')[0]).toMatchObject({
      pending: false,
      failed: true
    });
  });

  it('does not regress a terminal delivery update when the original POST response arrives later', () => {
    const delivered = { ...message('12', 'a'), delivery_status: 'delivered' as const };
    const originalResponse = { ...message('12', 'a'), delivery_status: 'pending' as const };

    expect(reconcileMessage([delivered], originalResponse)).toEqual([delivered]);
  });

  it('fences a history completion after navigation starts', () => {
    const fence = new LoadFence();
    const first = fence.begin();
    const second = fence.begin();
    expect(fence.isCurrent(first)).toBe(false);
    expect(fence.isCurrent(second)).toBe(true);
  });

  it('merges a recovery snapshot without losing a newer confirmed or pending send', () => {
    const pending = message('pending-a', 'a', true);
    const confirmed = message('14', 'b');
    const recovered = message('12', 'a');

    const merged = mergeMessageSnapshot([message('10'), pending, confirmed], [recovered]);

    expect(merged.map((item) => item.id)).toEqual(['10', '12', '14']);
    expect(merged.some((item) => item.id === 'pending-a')).toBe(false);
  });

  it('treats the latest recovery window as authoritative without dropping older history', () => {
    const locallyConfirmed = message('15', 'local');
    const pending = message('pending-b', 'b', true);
    const merged = mergeMessageSnapshot(
      [message('5'), message('10'), message('11'), message('12'), locallyConfirmed, pending],
      [message('10'), message('12')],
      {
        authoritative: true,
        preserveNonces: new Set(['local'])
      }
    );

    expect(new Set(merged.map((item) => item.id))).toEqual(
      new Set(['5', '10', '12', '15', 'pending-b'])
    );
  });

  it('removes confirmed rows missing from a complete recovery snapshot', () => {
    expect(
      mergeMessageSnapshot([message('5'), message('10')], [message('10')], {
        authoritative: true,
        complete: true
      }).map((item) => item.id)
    ).toEqual(['10']);
  });

  it('reports whether a delivery update had a server message to update', () => {
    const update = {
      message_id: '12',
      message_domain: 'chat.example',
      channel_id: '1',
      channel_domain: 'chat.example',
      status: 'failed' as const,
      code: 'KAED_FED_DELIVERY_EXPIRED'
    };

    const missing = applyMessageDeliveryUpdate([message('pending-a', 'a', true)], update);
    expect(missing.matched).toBe(false);

    const applied = applyMessageDeliveryUpdate([message('12', 'a')], update);
    expect(applied.matched).toBe(true);
    expect(applied.messages[0]).toMatchObject({ delivery_status: 'failed', failed: true });
  });
});
