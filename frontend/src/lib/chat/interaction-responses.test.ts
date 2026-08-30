import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Channel } from './types';
import type { KaedeE2EEClient } from '$lib/e2ee/client';
import { interactionResponses } from './interaction-responses.svelte';

function responseEvent(
  operation: 'CREATE' | 'UPDATE' | 'DELETE',
  overrides: Record<string, unknown> = {}
) {
  const authority = String(overrides.authority_domain ?? 'c1.example');
  const interactionId = String(overrides.interaction_id ?? '10');
  const responseId = String(overrides.response_id ?? '20');
  return {
    authority_domain: authority,
    user_ref: '1@users.example',
    invoker_ref: '1@users.example',
    channel_ref: `2@${authority}`,
    application_ref: '3@apps.example',
    response_grant_id: `${'A'.repeat(42)}A`,
    interaction_id: interactionId,
    interaction_ref: `${interactionId}@${authority}`,
    response_id: responseId,
    response_ref: `${responseId}@${authority}`,
    sequence: 0,
    callback_type: 4,
    ephemeral: false,
    message_ref: null,
    autocomplete_generation: null,
    revision: '1',
    operation,
    expires_at: '2099-01-01T00:00:00Z',
    deleted_at: null,
    data: {},
    ...overrides
  };
}

function registerEncrypted(
  decryptInteractionResponse: KaedeE2EEClient['decryptInteractionResponse'],
  interactionRef = '10@c1.example'
): void {
  interactionResponses.register(interactionRef, {
    channelRef: '2@c1.example',
    applicationRef: '3@apps.example',
    e2ee: {
      client: { decryptInteractionResponse } as unknown as KaedeE2EEClient,
      channel: {} as Channel,
      integrationType: 'guild_install',
      interactionContext: 'guild'
    }
  });
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe('interaction response state', () => {
  beforeEach(() => {
    interactionResponses.reset();
    interactionResponses.register('10@c1.example', {
      channelRef: '2@c1.example',
      applicationRef: '3@apps.example'
    });
  });

  it('correlates callbacks, opens modals, and applies deletion', () => {
    interactionResponses.reset();
    interactionResponses.register('10@c1.example', {
      channelRef: '2@c1.example',
      applicationRef: '3@apps.example'
    });
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        callback_type: 9,
        data: { title: 'Reason', custom_id: 'reason', components: [] }
      })
    );
    expect(interactionResponses.response('10@c1.example')?.response_id).toBe('20');
    expect(interactionResponses.activeModal?.modal.title).toBe('Reason');

    interactionResponses.apply(
      'INTERACTION_RESPONSE_DELETE',
      responseEvent('DELETE', {
        revision: '2',
        callback_type: 9,
        deleted_at: '2026-08-28T00:00:00Z'
      })
    );
    expect(interactionResponses.response('10@c1.example')).toBeNull();
    expect(interactionResponses.activeModal).toBeNull();
  });

  it('resolves a pending autocomplete response from the gateway lifecycle', async () => {
    interactionResponses.reset();
    interactionResponses.register('30@c1.example', {
      channelRef: '2@c1.example',
      applicationRef: '3@apps.example'
    });
    const pending = interactionResponses.wait('30@c1.example');
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        interaction_id: '30',
        interaction_ref: '30@c1.example',
        callback_type: 8,
        autocomplete_generation: '4',
        data: { choices: [{ name: 'Kaede', value: 'kaede' }] }
      })
    );
    await expect(pending).resolves.toMatchObject({
      interaction_id: '30',
      callback_type: 8,
      autocomplete_generation: '4'
    });
  });

  it('keeps follow-ups independently addressable and deletes only the named response', () => {
    interactionResponses.reset();
    interactionResponses.register('40@c1.example', {
      channelRef: '2@c1.example',
      applicationRef: '3@apps.example'
    });
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        interaction_id: '40',
        interaction_ref: '40@c1.example',
        response_id: '41',
        response_ref: '41@c1.example',
        sequence: 0,
        callback_type: 4,
        ephemeral: true,
        data: { flags: 64 }
      })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', {
        interaction_id: '40',
        interaction_ref: '40@c1.example',
        response_id: '41',
        response_ref: '41@c1.example',
        sequence: 0,
        revision: '2',
        callback_type: 4,
        ephemeral: true,
        data: { content: 'Finished', flags: 64 }
      })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        interaction_id: '40',
        interaction_ref: '40@c1.example',
        response_id: '42',
        response_ref: '42@c1.example',
        sequence: 1,
        callback_type: 4,
        ephemeral: true,
        data: { content: 'Follow-up', flags: 64 }
      })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', {
        interaction_id: '40',
        interaction_ref: '40@c1.example',
        response_id: '41',
        response_ref: '41@c1.example',
        sequence: 0,
        revision: '3',
        callback_type: 4,
        ephemeral: true,
        data: { content: 'Finished again', flags: 64 }
      })
    );

    expect(Object.values(interactionResponses.byResponse)).toHaveLength(2);
    expect(interactionResponses.response('40@c1.example')?.response_id).toBe('42');
    expect(
      Object.values(interactionResponses.byResponse).find((event) => event.response_id === '41')
        ?.data
    ).toMatchObject({ content: 'Finished again' });

    interactionResponses.apply(
      'INTERACTION_RESPONSE_DELETE',
      responseEvent('DELETE', {
        interaction_id: '40',
        interaction_ref: '40@c1.example',
        response_id: '42',
        response_ref: '42@c1.example',
        sequence: 1,
        revision: '2',
        ephemeral: true,
        deleted_at: '2026-08-28T00:00:00Z'
      })
    );
    expect(Object.values(interactionResponses.byResponse)).toHaveLength(1);
    expect(interactionResponses.response('40@c1.example')?.response_id).toBe('41');
  });

  it('retains revision and tombstone highwater across reordered delivery', () => {
    interactionResponses.reset();
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', { revision: '3', data: { content: 'new' } })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', { revision: '2', data: { content: 'stale' } })
    );
    expect(interactionResponses.response('10@c1.example')?.data).toEqual({ content: 'new' });

    interactionResponses.apply(
      'INTERACTION_RESPONSE_DELETE',
      responseEvent('DELETE', { revision: '4', deleted_at: '2026-08-28T00:00:00Z' })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', { revision: '5', data: { content: 'revived' } })
    );
    expect(interactionResponses.response('10@c1.example')).toBeNull();
  });

  it('isolates colliding numeric IDs by response authority', async () => {
    interactionResponses.reset();
    const first = interactionResponses.wait('10@c1.example');
    const second = interactionResponses.wait('10@c2.example');
    interactionResponses.register('10@c2.example', {
      channelRef: '2@c2.example',
      applicationRef: '3@apps.example'
    });
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', { data: { content: 'one' } })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        authority_domain: 'c2.example',
        interaction_ref: '10@c2.example',
        response_ref: '20@c2.example',
        data: { content: 'two' }
      })
    );

    await expect(first).resolves.toMatchObject({ interaction_ref: '10@c1.example' });
    await expect(second).resolves.toMatchObject({ interaction_ref: '10@c2.example' });
    expect(Object.keys(interactionResponses.byResponse).sort()).toEqual([
      '20@c1.example',
      '20@c2.example'
    ]);
  });

  it('rejects authority-inconsistent event references', () => {
    interactionResponses.reset();
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', { interaction_ref: '10@other.example' })
    );
    expect(interactionResponses.byResponse).toEqual({});
  });

  it('rejects malformed authorities and response identity mutation', () => {
    interactionResponses.reset();
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        authority_domain: 'c1..example',
        interaction_ref: '10@c1..example',
        response_ref: '20@c1..example'
      })
    );
    expect(interactionResponses.byResponse).toEqual({});

    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', { data: { content: 'original' } })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', {
        interaction_id: '11',
        interaction_ref: '11@c1.example',
        revision: '2',
        data: { content: 'wrong interaction' }
      })
    );
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', {
        revision: '3',
        expires_at: '2098-01-01T00:00:00Z',
        data: { content: 'changed expiry' }
      })
    );
    expect(interactionResponses.response('10@c1.example')?.data).toEqual({
      content: 'original'
    });
  });

  it('rejects malformed exact-wire projections before state or MLS processing', async () => {
    const decrypt = vi.fn(async () => ({ context: {} as never, data: { content: 'no' } }));
    registerEncrypted(decrypt as KaedeE2EEClient['decryptInteractionResponse']);
    const malformed = [
      responseEvent('CREATE', { unexpected: true }),
      responseEvent('CREATE', { response_grant_id: 'not-a-grant' }),
      responseEvent('CREATE', { callback_type: 8, autocomplete_generation: null }),
      responseEvent('CREATE', { message_ref: '9@other.example' }),
      responseEvent('CREATE', { sequence: Number.MAX_SAFE_INTEGER + 1 }),
      responseEvent('UPDATE', { revision: '1' }),
      responseEvent('CREATE', { data: [] })
    ];
    for (const event of malformed) {
      interactionResponses.apply(
        event.operation === 'UPDATE'
          ? 'INTERACTION_RESPONSE_UPDATE'
          : 'INTERACTION_RESPONSE_CREATE',
        event
      );
    }
    await settle();
    expect(decrypt).not.toHaveBeenCalled();
    expect(interactionResponses.byResponse).toEqual({});
  });

  it('holds encrypted callbacks until their request context arrives', async () => {
    interactionResponses.reset();
    const decrypt = vi.fn(async () => ({
      context: {} as never,
      data: { choices: [{ name: 'Kaede', value: 'kaede' }] }
    }));
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        callback_type: 8,
        autocomplete_generation: '2',
        ephemeral: true,
        data: { e2ee: { ciphertext: 'opaque' }, attachments: [] }
      })
    );
    await settle();
    expect(decrypt).not.toHaveBeenCalled();

    registerEncrypted(decrypt as KaedeE2EEClient['decryptInteractionResponse']);
    await settle();
    expect(decrypt).toHaveBeenCalledTimes(1);
    expect(interactionResponses.response('10@c1.example')?.data).toEqual({
      choices: [{ name: 'Kaede', value: 'kaede' }]
    });
  });

  it('gates duplicate/stale revisions before decrypt and never exposes failed ciphertext', async () => {
    interactionResponses.reset();
    const decrypt = vi
      .fn()
      .mockRejectedValueOnce(new Error('SECRET-CIPHERTEXT'))
      .mockResolvedValueOnce({ context: {}, data: { content: 'recovered' } });
    registerEncrypted(decrypt as KaedeE2EEClient['decryptInteractionResponse']);
    const encrypted = {
      ephemeral: true,
      data: { e2ee: { ciphertext: 'SECRET-CIPHERTEXT' }, attachments: [] }
    };
    interactionResponses.apply('INTERACTION_RESPONSE_CREATE', responseEvent('CREATE', encrypted));
    await settle();
    expect(decrypt).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(interactionResponses.byResponse)).not.toContain('SECRET-CIPHERTEXT');
    expect(interactionResponses.response('10@c1.example')).toMatchObject({
      decryption_unavailable: true,
      data: { content: 'This encrypted bot response is unavailable on this device.' }
    });

    interactionResponses.apply('INTERACTION_RESPONSE_CREATE', responseEvent('CREATE', encrypted));
    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', { ...encrypted, revision: '2' })
    );
    await settle();
    expect(decrypt).toHaveBeenCalledTimes(2);
    expect(interactionResponses.response('10@c1.example')?.data).toEqual({
      content: 'recovered'
    });
  });

  it('presents private controls only with matching server view lineage', async () => {
    interactionResponses.reset();
    const decrypt = vi.fn(async () => ({
      context: {
        callback_type: 4,
        interaction_contract_digest: `${'A'.repeat(42)}E`
      } as never,
      data: {
        content: 'Private controls',
        components: [{ type: 1, components: [{ type: 2, custom_id: 'confirm' }] }]
      }
    }));
    registerEncrypted(decrypt as KaedeE2EEClient['decryptInteractionResponse']);
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        ephemeral: true,
        data: {
          e2ee: { ciphertext: 'opaque' },
          attachments: [],
          view_version: 1,
          view_persistent: false,
          view_expires_at: '2098-01-01T00:00:00Z'
        }
      })
    );
    await settle();
    expect(interactionResponses.response('10@c1.example')?.data).toMatchObject({
      content: 'Private controls',
      view_version: 1,
      view_persistent: false,
      view_expires_at: '2098-01-01T00:00:00Z'
    });

    interactionResponses.apply(
      'INTERACTION_RESPONSE_UPDATE',
      responseEvent('UPDATE', {
        revision: '2',
        ephemeral: true,
        data: { e2ee: { ciphertext: 'missing-lineage' }, attachments: [] }
      })
    );
    await settle();
    expect(interactionResponses.response('10@c1.example')).toMatchObject({
      decryption_unavailable: true
    });
    expect(JSON.stringify(interactionResponses.byResponse)).not.toContain('missing-lineage');
  });

  it('fences an in-flight decrypt across reset', async () => {
    interactionResponses.reset();
    let resolveDecrypt!: (value: { context: never; data: Record<string, unknown> }) => void;
    const decrypt = vi.fn(
      () =>
        new Promise<{ context: never; data: Record<string, unknown> }>((resolve) => {
          resolveDecrypt = resolve;
        })
    );
    registerEncrypted(decrypt as KaedeE2EEClient['decryptInteractionResponse']);
    interactionResponses.apply(
      'INTERACTION_RESPONSE_CREATE',
      responseEvent('CREATE', {
        ephemeral: true,
        data: { e2ee: { ciphertext: 'old-session' }, attachments: [] }
      })
    );
    await Promise.resolve();
    interactionResponses.reset();
    resolveDecrypt({ context: {} as never, data: { content: 'must not render' } });
    await settle();
    expect(interactionResponses.byResponse).toEqual({});
  });
});
