import type { Room } from 'livekit-client';
import { describe, expect, it, vi } from 'vitest';
import type { Channel } from '$lib/chat/types';

import {
  expectedVoicePolicy,
  isUsableVoiceToken,
  VoiceConnectionFence,
  VoiceSession,
  voiceGrantMatchesChannelPolicy,
  voiceGrantMatchesExpectedPolicy,
  withVoiceConnectTimeout,
  type VoiceChannelPolicy,
  type VoiceToken
} from './session';

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

class FakeVoiceRoom {
  readonly connect;
  readonly disconnect = vi.fn(async () => undefined);
  readonly on = vi.fn(() => this);
  readonly localParticipant = {
    setMicrophoneEnabled: vi.fn(async () => undefined)
  };

  constructor(connect: () => Promise<void> = async () => undefined) {
    this.connect = vi.fn(connect);
  }
}

const plaintextChannel = {
  id: '2',
  origin_domain: 'chat.example',
  encryption_mode: 'plaintext',
  encryption_state: 'plaintext',
  encryption_policy_generation: '0',
  encryption_epoch: null
} satisfies VoiceChannelPolicy;

describe('voice connection generation fence', () => {
  it('makes an in-flight move stale when the user leaves', () => {
    const fence = new VoiceConnectionFence();
    const moving = fence.begin();

    fence.invalidate();

    expect(fence.isCurrent(moving)).toBe(false);
  });

  it('allows only the newest competing join to publish', () => {
    const fence = new VoiceConnectionFence();
    const first = fence.begin();
    const second = fence.begin();

    expect(fence.isCurrent(first)).toBe(false);
    expect(fence.isCurrent(second)).toBe(true);
  });

  it('disconnects an overlapping stale candidate before it can enable the microphone', async () => {
    const firstConnect = deferred<void>();
    const idle = new FakeVoiceRoom();
    const first = new FakeVoiceRoom(() => firstConnect.promise);
    const second = new FakeVoiceRoom();
    const rooms = [idle, first, second];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);

    const firstAttempt = voice.connect(
      grant({ token: 'a'.repeat(64), expires_at: '2099-07-19T12:15:00Z' }),
      plaintextChannel
    );
    await vi.waitFor(() => expect(first.connect).toHaveBeenCalledOnce());

    const secondAttempt = voice.connect(
      grant({ token: 'b'.repeat(64), expires_at: '2099-07-19T12:15:00Z' }),
      plaintextChannel
    );
    await secondAttempt;
    firstConnect.resolve();
    await firstAttempt;

    expect(first.disconnect).toHaveBeenCalled();
    expect(first.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
    expect(second.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(true);
    expect(voice.room).toBe(second);
    expect(voice.connected).toBe(true);
    expect(voice.connecting).toBe(false);
  });

  it('makes disconnect win over an in-flight connect without stale microphone activation', async () => {
    const connectGate = deferred<void>();
    const idle = new FakeVoiceRoom();
    const candidate = new FakeVoiceRoom(() => connectGate.promise);
    const rooms = [idle, candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);

    const attempt = voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);
    await vi.waitFor(() => expect(candidate.connect).toHaveBeenCalledOnce());
    await voice.disconnect();
    connectGate.resolve();
    await attempt;

    expect(candidate.disconnect).toHaveBeenCalled();
    expect(candidate.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
    expect(voice.connected).toBe(false);
    expect(voice.connecting).toBe(false);
    expect(voice.microphone).toBe(false);
  });
});

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
    e2ee: false,
    channel_id: '2',
    channel_domain: 'chat.example',
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
    expect(isUsableVoiceToken({ ...grant(), e2ee: undefined } as unknown as VoiceToken, 0)).toBe(
      false
    );
    expect(isUsableVoiceToken(grant({ channel_id: null }), 0)).toBe(false);
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

  it('enforces the channel mode in both directions', () => {
    const plaintextChannel = {
      ...channel,
      encryption_mode: 'plaintext',
      encryption_state: 'plaintext',
      encryption_policy_generation: '0',
      encryption_epoch: null
    } satisfies VoiceChannelPolicy;
    const plaintext = grant();

    expect(voiceGrantMatchesChannelPolicy(plaintext, plaintextChannel)).toBe(true);
    expect(voiceGrantMatchesChannelPolicy(plaintext, channel)).toBe(false);
    expect(voiceGrantMatchesChannelPolicy(encrypted, plaintextChannel)).toBe(false);
  });

  it('pins the full validated policy for a native grant refetch', () => {
    const current = { ...encrypted, expires_at: '2099-07-19T12:15:00Z' };
    const expected = expectedVoicePolicy(current, channel);
    expect(voiceGrantMatchesExpectedPolicy(current, expected)).toBe(true);
    expect(voiceGrantMatchesExpectedPolicy({ ...current, e2ee: false }, expected)).toBe(false);
    expect(
      voiceGrantMatchesExpectedPolicy({ ...current, media_session_id: 'b'.repeat(43) }, expected)
    ).toBe(false);
  });
});
