import { describe, expect, it } from 'vitest';
import type { Channel } from '$lib/chat/types';

import {
  isUsableVoiceToken,
  voiceGrantMatchesChannelPolicy,
  withVoiceConnectTimeout,
  type VoiceToken
} from './session';

function grant(overrides: Partial<VoiceToken> = {}): VoiceToken {
  return {
    token: 'a'.repeat(64),
    url: 'wss://chat.example/livekit',
    room: 'g.1.2',
    generation: 0,
    expires_at: '2026-07-19T12:15:00Z',
    can_speak: true,
    can_stream: true,
    can_use_vad: true,
    ...overrides
  };
}

describe('voice grant validation', () => {
  it('accepts a future, scoped LiveKit grant', () => {
    expect(isUsableVoiceToken(grant(), Date.parse('2026-07-19T12:00:00Z'))).toBe(true);
  });

  it('rejects expired, non-WebSocket, and malformed room grants', () => {
    expect(isUsableVoiceToken(grant(), Date.parse('2026-07-19T12:15:00Z'))).toBe(false);
    expect(isUsableVoiceToken(grant({ url: 'https://chat.example/livekit' }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ room: '../g.1.2' }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ move_session_id: 'too-short' }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ move_session_id: '!'.repeat(32) }), 0)).toBe(false);
    expect(
      isUsableVoiceToken(grant({ move_session_id: ['a'.repeat(32)] as unknown as string }), 0)
    ).toBe(false);
  });

  it('requires a complete, internally consistent encrypted media context', () => {
    const encrypted = grant({
      e2ee: true,
      channel_id: '2',
      channel_domain: 'chat.example',
      encryption_policy_generation: '4',
      encryption_epoch: '7',
      media_protocol: 'livekit-e2ee-v1',
      media_suite: 'AES-256-GCM',
      media_session_id: 'a'.repeat(43),
      media_epoch: '7'
    });
    expect(isUsableVoiceToken(encrypted, 0)).toBe(true);
    expect(isUsableVoiceToken({ ...encrypted, media_epoch: '6' }, 0)).toBe(false);
    expect(isUsableVoiceToken({ ...encrypted, media_session_id: 'short' }, 0)).toBe(false);
    expect(
      isUsableVoiceToken(
        grant({ media_protocol: 'livekit-e2ee-v1', media_suite: 'AES-256-GCM' }),
        0
      )
    ).toBe(false);
  });
});

describe('voice connection timeout', () => {
  it('rejects a signaling attempt that never settles', async () => {
    await expect(withVoiceConnectTimeout(new Promise(() => undefined), 1)).rejects.toThrow(
      'Voice connection timed out'
    );
  });
});

describe('voice media key rotation', () => {
  const channel = {
    id: '2',
    origin_domain: 'chat.example',
    encryption_mode: 'e2ee',
    encryption_state: 'active',
    encryption_policy_generation: '4',
    encryption_epoch: '7'
  } satisfies Pick<
    Channel,
    | 'id'
    | 'origin_domain'
    | 'encryption_mode'
    | 'encryption_state'
    | 'encryption_policy_generation'
    | 'encryption_epoch'
  >;

  const encrypted = grant({
    e2ee: true,
    channel_id: '2',
    channel_domain: 'chat.example',
    encryption_policy_generation: '4',
    encryption_epoch: '7',
    media_protocol: 'livekit-e2ee-v1',
    media_suite: 'AES-256-GCM',
    media_session_id: 'a'.repeat(43),
    media_epoch: '7'
  });

  it('rejects the old grant after epoch rotation and accepts the replacement', () => {
    expect(voiceGrantMatchesChannelPolicy(encrypted, channel)).toBe(true);
    const rotatedChannel = {
      ...channel,
      encryption_policy_generation: '5',
      encryption_epoch: '8'
    };
    expect(voiceGrantMatchesChannelPolicy(encrypted, rotatedChannel)).toBe(false);
    expect(
      voiceGrantMatchesChannelPolicy(
        {
          ...encrypted,
          encryption_policy_generation: '5',
          encryption_epoch: '8',
          media_session_id: 'b'.repeat(43),
          media_epoch: '8'
        },
        rotatedChannel
      )
    ).toBe(true);
  });

  it('rejects grants while the room is rekeying or for another channel', () => {
    expect(
      voiceGrantMatchesChannelPolicy(encrypted, { ...channel, encryption_state: 'rekeying' })
    ).toBe(false);
    expect(voiceGrantMatchesChannelPolicy({ ...encrypted, channel_id: '3' }, channel)).toBe(false);
  });
});
