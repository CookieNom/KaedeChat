import { Track, type Room } from 'livekit-client';
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
    setMicrophoneEnabled: vi.fn(async () => undefined),
    setCameraEnabled: vi.fn(async () => undefined),
    setScreenShareEnabled: vi.fn(async () => undefined),
    getTrackPublication: vi.fn(() => undefined as unknown),
    unpublishTrack: vi.fn(async () => undefined),
    publishTrack: vi.fn(async () => undefined)
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

describe('self voice-state publication', () => {
  it('enforces no-VAD grants with hold-to-talk instead of opening the microphone', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const publish = vi.fn(async () => undefined);
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room, publish);

    await voice.connect(
      grant({ can_use_vad: false, expires_at: '2099-07-19T12:15:00Z' }),
      plaintextChannel
    );

    expect(voice.canSpeak).toBe(true);
    expect(voice.pushToTalkRequired).toBe(true);
    expect(voice.microphone).toBe(false);
    expect(candidate.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();

    await voice.toggleMicrophone();
    expect(candidate.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();

    await voice.startPushToTalk();
    expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(true);
    expect(voice.microphone).toBe(true);

    await voice.stopPushToTalk();
    expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(false);
    expect(voice.microphone).toBe(false);
    expect(publish).not.toHaveBeenCalled();
  });

  it('serializes a release behind an in-flight push-to-talk track enable', async () => {
    const enabled = deferred<undefined>();
    const candidate = new FakeVoiceRoom();
    candidate.localParticipant.setMicrophoneEnabled.mockImplementationOnce(() => enabled.promise);
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);
    await voice.connect(
      grant({ can_use_vad: false, expires_at: '2099-07-19T12:15:00Z' }),
      plaintextChannel
    );

    const pressing = voice.startPushToTalk();
    await vi.waitFor(() =>
      expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(true)
    );
    const releasing = voice.stopPushToTalk();
    enabled.resolve(undefined);
    await Promise.all([pressing, releasing]);

    expect(candidate.localParticipant.setMicrophoneEnabled.mock.calls).toEqual([[true], [false]]);
    expect(voice.microphone).toBe(false);
  });

  it('closes an open VAD microphone when USE_VAD is revoked mid-session', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);

    await voice.reconcileBrowserPermissions({
      canConnect: true,
      canSpeak: true,
      canStream: true,
      canUseVad: false
    });

    expect(candidate.localParticipant.setMicrophoneEnabled.mock.calls).toEqual([[true], [false]]);
    expect(voice.canSpeak).toBe(true);
    expect(voice.pushToTalkRequired).toBe(true);
    expect(voice.microphone).toBe(false);
  });

  it('closes microphone capture when SPEAK is revoked mid-session', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);

    await voice.reconcileBrowserPermissions({
      canConnect: true,
      canSpeak: false,
      canStream: true,
      canUseVad: false
    });

    expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(false);
    expect(voice.canSpeak).toBe(false);
    expect(voice.pushToTalkRequired).toBe(false);
    expect(voice.microphone).toBe(false);
  });

  it('leaves the room when CONNECT is revoked mid-session', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);

    await voice.reconcileBrowserPermissions({
      canConnect: false,
      canSpeak: false,
      canStream: false,
      canUseVad: false
    });

    expect(candidate.disconnect).toHaveBeenCalled();
    expect(voice.connected).toBe(false);
  });

  it('stops published video when STREAM is revoked mid-session', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);
    await voice.toggleCamera();
    await voice.startScreenShare({
      screenProfile: 'smooth',
      audioQuality: 'standard',
      shareAudio: false,
      dtx: true
    });

    await voice.reconcileBrowserPermissions({
      canConnect: true,
      canSpeak: true,
      canStream: false,
      canUseVad: true
    });

    expect(candidate.localParticipant.setCameraEnabled).toHaveBeenLastCalledWith(false);
    expect(candidate.localParticipant.setScreenShareEnabled).toHaveBeenLastCalledWith(false);
    expect(voice.canStream).toBe(false);
    expect(voice.camera).toBe(false);
    expect(voice.screen).toBe(false);
  });

  it('gives promoted Stage speakers VAD and closes every track on demotion', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);
    await voice.connect(
      grant({
        can_speak: false,
        can_stream: false,
        can_use_vad: false,
        expires_at: '2099-07-19T12:15:00Z'
      }),
      plaintextChannel
    );

    await voice.reconcileParticipantPermissions({
      canSpeak: true,
      canStream: true,
      canUseVad: true
    });
    expect(voice.canSpeak).toBe(true);
    expect(voice.canStream).toBe(true);
    expect(voice.pushToTalkRequired).toBe(false);

    await voice.toggleMicrophone();
    await voice.toggleCamera();
    await voice.startScreenShare({
      screenProfile: 'smooth',
      audioQuality: 'standard',
      shareAudio: false,
      dtx: true
    });
    await voice.reconcileParticipantPermissions({
      canSpeak: false,
      canStream: false,
      canUseVad: false
    });

    expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(false);
    expect(candidate.localParticipant.setCameraEnabled).toHaveBeenLastCalledWith(false);
    expect(candidate.localParticipant.setScreenShareEnabled).toHaveBeenLastCalledWith(false);
    expect(voice.canSpeak).toBe(false);
    expect(voice.canStream).toBe(false);
    expect(voice.microphone).toBe(false);
    expect(voice.camera).toBe(false);
    expect(voice.screen).toBe(false);
  });

  it('keeps a local mute when the authoritative update fails', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const publish = vi.fn(async () => {
      throw new Error('gateway offline');
    });
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room, publish);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);

    await expect(voice.toggleMicrophone()).rejects.toThrow('gateway offline');

    expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(false);
    expect(voice.microphone).toBe(false);
    expect(publish).toHaveBeenCalledWith({ self_mute: true, self_deaf: false });
  });

  it('does not open capture when an authoritative unmute cannot be sent', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const publish = vi
      .fn<() => Promise<void>>()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('gateway offline'));
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room, publish);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);
    await voice.toggleMicrophone();
    candidate.localParticipant.setMicrophoneEnabled.mockClear();

    await expect(voice.toggleMicrophone()).rejects.toThrow('gateway offline');

    expect(candidate.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
    expect(voice.microphone).toBe(false);
  });

  it('publishes deafen with the implied mute and keeps mute after undeafening', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const publish = vi.fn(async () => undefined);
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room, publish);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);

    await voice.toggleDeafen();
    expect(voice.deafened).toBe(true);
    expect(voice.microphone).toBe(false);
    expect(publish).toHaveBeenLastCalledWith({ self_mute: true, self_deaf: true });

    await voice.toggleDeafen();
    expect(voice.deafened).toBe(false);
    expect(voice.microphone).toBe(false);
    expect(publish).toHaveBeenLastCalledWith({ self_mute: true, self_deaf: false });
  });
});

function grant(overrides: Partial<VoiceToken> = {}): VoiceToken {
  return {
    token: 'a'.repeat(64),
    url: 'wss://chat.example/livekit',
    room: 'g.1.2',
    generation: 0,
    connection_id: 'c'.repeat(43),
    expires_at: '2026-07-19T12:15:00Z',
    can_speak: true,
    can_stream: true,
    can_use_vad: true,
    bitrate: 64_000,
    user_limit: 0,
    rtc_region: null,
    video_quality_mode: 1,
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
    expect(isUsableVoiceToken({ ...grant(), can_speak: 'yes' } as unknown as VoiceToken, 0)).toBe(
      false
    );
    expect(isUsableVoiceToken({ ...grant(), can_stream: 1 } as unknown as VoiceToken, 0)).toBe(
      false
    );
    expect(isUsableVoiceToken({ ...grant(), can_use_vad: null } as unknown as VoiceToken, 0)).toBe(
      false
    );
    expect(isUsableVoiceToken(grant({ channel_id: null }), 0)).toBe(false);
  });

  it('requires a complete and bounded effective media policy', () => {
    expect(isUsableVoiceToken(grant({ bitrate: 7_999 }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ bitrate: 384_001 }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ user_limit: 100 }), 0)).toBe(true);
    expect(isUsableVoiceToken(grant({ user_limit: 10_001 }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ rtc_region: '' }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ rtc_region: 'x'.repeat(65) }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ video_quality_mode: 3 as 1 }), 0)).toBe(false);
    expect(isUsableVoiceToken({ ...grant(), bitrate: undefined } as unknown as VoiceToken, 0)).toBe(
      false
    );
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

  it('binds every effective channel policy field and preserves opaque regions', () => {
    const configuredChannel = {
      ...plaintextChannel,
      bitrate: 32_000,
      user_limit: 17,
      rtc_region: 'future-region/alpha',
      video_quality_mode: 2 as const
    } satisfies VoiceChannelPolicy;
    const configured = grant({
      bitrate: 32_000,
      user_limit: 17,
      rtc_region: 'future-region/alpha',
      video_quality_mode: 2,
      expires_at: '2099-07-19T12:15:00Z'
    });

    expect(voiceGrantMatchesChannelPolicy(configured, configuredChannel)).toBe(true);
    expect(
      voiceGrantMatchesChannelPolicy({ ...configured, bitrate: 48_000 }, configuredChannel)
    ).toBe(false);
    expect(
      voiceGrantMatchesChannelPolicy({ ...configured, user_limit: 18 }, configuredChannel)
    ).toBe(false);
    expect(
      voiceGrantMatchesChannelPolicy({ ...configured, rtc_region: 'other' }, configuredChannel)
    ).toBe(false);
    expect(
      voiceGrantMatchesChannelPolicy({ ...configured, video_quality_mode: 1 }, configuredChannel)
    ).toBe(false);

    const expected = expectedVoicePolicy(configured, configuredChannel);
    expect(expected).toMatchObject({
      bitrate: 32_000,
      user_limit: 17,
      rtc_region: 'future-region/alpha',
      video_quality_mode: 2
    });
    expect(voiceGrantMatchesExpectedPolicy(configured, expected)).toBe(true);
    expect(voiceGrantMatchesExpectedPolicy({ ...configured, bitrate: 24_000 }, expected)).toBe(
      false
    );
  });

  it('uses the grant bitrate and video mode for browser publication defaults', async () => {
    const configuredChannel = {
      ...plaintextChannel,
      bitrate: 32_000,
      video_quality_mode: 2 as const
    } satisfies VoiceChannelPolicy;
    const options: Array<ConstructorParameters<typeof Room>[0]> = [];
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession((roomOptions) => {
      options.push(roomOptions);
      return rooms.shift() as unknown as Room;
    });

    await voice.connect(
      grant({
        bitrate: 32_000,
        video_quality_mode: 2,
        expires_at: '2099-07-19T12:15:00Z'
      }),
      configuredChannel
    );

    expect(options[1]?.publishDefaults).toMatchObject({
      audioPreset: { maxBitrate: 32_000 },
      videoEncoding: { maxBitrate: 1_700_000 }
    });
    expect(options[1]?.videoCaptureDefaults?.resolution).toMatchObject({
      width: 1280,
      height: 720
    });
    expect(voice.voiceMediaPolicy).toEqual({
      bitrate: 32_000,
      user_limit: 0,
      rtc_region: null,
      video_quality_mode: 2
    });

    const microphone = {
      isMuted: false,
      mute: vi.fn(async () => undefined)
    };
    candidate.localParticipant.getTrackPublication.mockReturnValue({
      track: microphone,
      options: { audioPreset: { maxBitrate: 32_000 } }
    });
    await voice.startScreenShare({
      screenProfile: 'sharp',
      audioQuality: 'studio',
      shareAudio: true,
      dtx: false
    });

    expect(candidate.localParticipant.getTrackPublication).toHaveBeenCalledWith(
      Track.Source.Microphone
    );
    expect(candidate.localParticipant.publishTrack).toHaveBeenCalledWith(
      microphone,
      expect.objectContaining({ audioPreset: { maxBitrate: 32_000 } })
    );
    expect(candidate.localParticipant.setScreenShareEnabled).toHaveBeenCalledWith(
      true,
      expect.any(Object),
      expect.objectContaining({
        audioPreset: { maxBitrate: 128_000 },
        screenShareEncoding: { maxBitrate: 4_500_000, maxFramerate: 30 }
      })
    );
  });
});
