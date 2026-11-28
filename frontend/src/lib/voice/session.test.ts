import * as callSounds from './call-sounds';
import { DEFAULT_MEDIA_QUALITY, webCameraDefaults } from './quality';
import {
  ExternalE2EEKeyProvider,
  RoomEvent,
  Track,
  type Participant,
  type Room
} from 'livekit-client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Channel } from '$lib/chat/types';

import {
  decodeNativeVideoFrame,
  attachVideo,
  NativeVideoPresentation,
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

beforeEach(() => {
  vi.stubGlobal('navigator', {
    mediaDevices: Object.assign(new EventTarget(), {
      enumerateDevices: vi.fn(async () => [{ kind: 'videoinput', deviceId: 'first-camera' }])
    })
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

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
  readonly off = vi.fn(() => this);
  readonly remoteParticipants = new Map();
  readonly localParticipant = {
    identity: 'local',
    publishData: vi.fn(async () => undefined),
    trackPublications: new Map(),
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

it('updates the desktop DM roster and speech indicator as native room status changes', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-07-19T12:00:00Z'));
  const cue = vi.spyOn(callSounds, 'playCallSound');
  const peer = { name: 'Alice', speaking: false, microphone: false, camera: false, screen: false };
  const status = {
    state: 'connected',
    can_speak: true,
    screen: true,
    speech_frames: 0,
    input_level: 0,
    remote_participants: { '2@chat.example': peer } as Record<string, typeof peer>
  };
  const invoke = vi.fn(async (command: string, _args?: Record<string, unknown>) => {
    if (command === 'native_voice_status') return { ...status };
    if (command === 'native_voice_next_video') return new Uint8Array();
  });
  vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
  const voice = new VoiceSession(() => new FakeVoiceRoom() as unknown as Room);
  const handle = '3bd81f31-6ab3-4569-bdae-a9258a5b4a5e';
  await voice.connectNative('2', true, grant({ token: handle }), plaintextChannel);
  try {
    expect(invoke).toHaveBeenCalledWith(
      'native_voice_join',
      expect.objectContaining({ grantHandle: handle, connectionId: grant().connection_id })
    );
    expect(voice.participants()[0].speaking).toBe(false);
    expect(voice.screen).toBe(true);
    expect(voice.participants()).toHaveLength(2);
    expect(voice.participants()[1]).toEqual({
      ...peer,
      identity: '2@chat.example',
      key: '2@chat.example',
      local: false
    });
    status.remote_participants = {};
    // Closing the shared window ends capture outside the in-call controls.
    status.state = 'media_error';
    status.screen = false;
    status.speech_frames++;
    await vi.advanceTimersByTimeAsync(75);
    expect(voice.participants()).toHaveLength(1);
    expect(voice.connected).toBe(true);
    expect(voice.screen).toBe(false);
    expect(voice.participants()[0].speaking).toBe(true);
    await vi.advanceTimersByTimeAsync(400);
    expect(voice.participants()[0].speaking).toBe(false);
    status.remote_participants = { '3@chat.example': peer };
    await vi.advanceTimersByTimeAsync(150);
    expect(cue.mock.calls).toEqual([['join'], ['leave'], ['join']]);
  } finally {
    await voice.disconnect();
    expect(cue).toHaveBeenLastCalledWith('leave');
    cue.mockRestore();
  }
});

it('reports native screen sharing after publication and preserves picker errors for retry', async () => {
  let commandResult = deferred<void>();
  const commandSent = deferred<void>();
  const invoke = vi.fn((command: string) => {
    if (command !== 'native_voice_control') return Promise.resolve();
    commandSent.resolve();
    return commandResult.promise;
  });
  vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
  const voice = new VoiceSession(() => new FakeVoiceRoom() as unknown as Room);
  voice.connected = true;
  voice.canStream = true;

  const failed = voice.startScreenShare(DEFAULT_MEDIA_QUALITY);
  await voice.startScreenShare(DEFAULT_MEDIA_QUALITY);
  await commandSent.promise;
  expect(invoke.mock.calls.filter(([name]) => name === 'native_voice_control')).toHaveLength(1);
  expect(voice.screenPending).toBe('starting');
  expect(voice.screen).toBe(false);
  commandResult.reject({ message: 'Screen recording permission was denied.' });
  await expect(failed).rejects.toThrow('Screen recording permission was denied.');
  expect(voice.screenPending).toBeNull();

  commandResult = deferred<void>();
  const start = voice.startScreenShare(DEFAULT_MEDIA_QUALITY);
  commandResult.resolve();
  await start;
  expect(voice.screen).toBe(true);
  commandResult = deferred<void>();
  const stop = voice.stopScreenShare();
  await voice.stopScreenShare();
  expect(voice.screenPending).toBe('stopping');
  expect(voice.screen).toBe(true);
  commandResult.resolve();
  await stop;
  expect(voice.screen).toBe(false);
  expect(voice.screenPending).toBeNull();
});

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

it('tracks camera hotplug, blocks capture without a camera, and selects the first camera', async () => {
  vi.useFakeTimers();
  const devices = navigator.mediaDevices;
  const enumerate = vi.mocked(devices.enumerateDevices);
  enumerate.mockResolvedValue([]);
  const room = new FakeVoiceRoom();
  const voice = new VoiceSession(() => room as unknown as Room);
  voice.connected = true;
  voice.canStream = true;
  const changed = vi.fn();
  voice.addEventListener('change', changed);
  const stop = voice.watchCameras();
  try {
    await vi.advanceTimersByTimeAsync(0);
    expect(voice.cameraAvailable).toBe(false);
    await voice.toggleCamera();
    expect(room.localParticipant.setCameraEnabled).not.toHaveBeenCalled();
    enumerate.mockResolvedValue([
      { kind: 'videoinput', deviceId: 'usb-camera' },
      { kind: 'videoinput', deviceId: 'second-camera' }
    ] as MediaDeviceInfo[]);
    devices.dispatchEvent(new Event('devicechange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(voice.cameraAvailable).toBe(true);
    await voice.toggleCamera();
    expect(room.localParticipant.setCameraEnabled).toHaveBeenLastCalledWith(
      true,
      expect.objectContaining({ deviceId: 'usb-camera' }),
      expect.any(Object)
    );
    enumerate.mockResolvedValue([]);
    await vi.advanceTimersByTimeAsync(2000);
    expect(voice.cameraAvailable).toBe(false);
    await voice.toggleCamera();
    expect(voice.camera).toBe(false);
    stop();
    changed.mockClear();
    devices.dispatchEvent(new Event('devicechange'));
    await vi.advanceTimersByTimeAsync(2000);
    expect(changed).not.toHaveBeenCalled();
  } finally {
    stop();
  }
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
      canUseVad: true
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
      canSpeak: true,
      canStream: true,
      canUseVad: true
    });

    expect(candidate.disconnect).toHaveBeenCalled();
    expect(voice.connected).toBe(false);
  });

  it('stops published video when STREAM is revoked mid-session', async () => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(() => rooms.shift() as unknown as Room);
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);
    voice.camera = true;
    voice.screen = true;

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
    voice.camera = true;
    voice.screen = true;
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
    vi.useFakeTimers();
    try {
      const pending = expect(
        withVoiceConnectTimeout(new Promise(() => undefined), 10_000)
      ).rejects.toThrow(/timed out/i);
      await vi.advanceTimersByTimeAsync(10_000);
      await pending;
    } finally {
      vi.useRealTimers();
    }
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

  it.each([true, false])(
    'waits for worker confirmation before publishing (enabled %s)',
    async (enabled) => {
      vi.stubGlobal(
        'RTCRtpSender',
        class {
          get transform() {
            return null;
          }
        }
      );
      const terminate = vi.fn();
      vi.stubGlobal(
        'Worker',
        class {
          terminate = terminate;
        }
      );
      const keyInstall = vi.spyOn(ExternalE2EEKeyProvider.prototype, 'setKey').mockResolvedValue();
      const candidate = new FakeVoiceRoom();
      const room = candidate as unknown as Room;
      let ack!: (enabled: boolean, participant: Participant) => void;
      room.on = vi.fn((event, listener) => {
        if (event === RoomEvent.ParticipantEncryptionStatusChanged) ack = listener;
        return room;
      }) as Room['on'];
      const enable = vi.fn(async () => undefined);
      room.setE2EEEnabled = enable;
      const idle = new FakeVoiceRoom() as unknown as Room;
      const voice = new VoiceSession((options) => {
        if (!options?.e2ee || !('keyProvider' in options.e2ee)) return idle;
        expect(options.e2ee.keyProvider.getOptions().keySize).toBe(256);
        return room;
      });
      try {
        const attempt = voice.connect(
          { ...encrypted, expires_at: '2099-07-19T12:15:00Z' },
          channel,
          new ArrayBuffer(32)
        );
        await vi.waitFor(() => expect(enable).toHaveBeenCalledWith(true));
        expect(candidate.connect).not.toHaveBeenCalled();
        expect(candidate.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
        room.isE2EEEnabled = enabled;
        Object.assign(room.localParticipant, { isE2EEEnabled: enabled });
        ack(true, room.localParticipant);
        if (enabled) {
          await attempt;
          expect(voice.encrypted).toBe(true);
          expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(true);
        } else {
          await expect(attempt).rejects.toThrow(/enabl.*encrypted/i);
          expect(voice.encrypted).toBeNull();
          expect(candidate.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
          expect(candidate.disconnect).toHaveBeenCalled();
        }
        await voice.disconnect();
        expect(terminate).toHaveBeenCalled();
      } finally {
        keyInstall.mockRestore();
      }
    }
  );

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
    expect(
      voiceGrantMatchesChannelPolicy(encrypted, { ...channel, encryption_policy_generation: '5' })
    ).toBe(false);
    expect(voiceGrantMatchesChannelPolicy(encrypted, { ...channel, encryption_epoch: '8' })).toBe(
      false
    );
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
      videoEncoding: webCameraDefaults(2).publish.videoEncoding
    });
    expect(options[1]?.videoCaptureDefaults?.resolution).toEqual(
      webCameraDefaults(2).capture.resolution
    );
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
      expect.objectContaining({ audioPreset: { maxBitrate: 32_000, priority: 'high' } })
    );
    expect(candidate.localParticipant.setScreenShareEnabled).toHaveBeenCalledWith(
      true,
      expect.any(Object),
      expect.objectContaining({
        audioPreset: { maxBitrate: 128_000, priority: 'high' },
        screenShareEncoding: { maxBitrate: 4_500_000, maxFramerate: 30 }
      })
    );
  });
});

describe('native camera and share frame identity', () => {
  function packet(format: 'KVD1' | 'KVD2', source = 0) {
    const identity = new TextEncoder().encode('alice');
    const header = format === 'KVD2' ? 16 : 15;
    const bytes = new Uint8Array(header + identity.length + 4);
    bytes.set(new TextEncoder().encode(format));
    const view = new DataView(bytes.buffer);
    view.setUint32(4, 1, true);
    view.setUint32(8, 1, true);
    view.setUint16(12, identity.length, true);
    if (format === 'KVD2') bytes[15] = source;
    bytes.set(identity, header);
    return bytes;
  }

  it('starts fetching frames when a native join finishes and ignores an old room response', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-19T12:00:00Z'));
    let state = 'connecting';
    const pendingFrame = deferred<Uint8Array>();
    const nextFrame = vi
      .fn()
      .mockResolvedValueOnce(packet('KVD2', 2))
      .mockReturnValueOnce(pendingFrame.promise)
      .mockResolvedValue(new Uint8Array());
    vi.stubGlobal('window', {
      __TAURI__: {
        core: {
          invoke: async (command: string) => {
            if (command === 'native_voice_status')
              return { state, screen: true, remote_participants: {} };
            if (command === 'native_voice_next_video') return nextFrame();
          }
        }
      }
    });
    const voice = new VoiceSession(() => new FakeVoiceRoom() as unknown as Room);
    await voice.connectNative('2', true, grant(), plaintextChannel);
    try {
      expect(voice.connecting).toBe(true);
      expect(nextFrame).not.toHaveBeenCalled();
      state = 'connected';
      await vi.advanceTimersByTimeAsync(75);
      expect(voice.tiles()[0]).toMatchObject({ local: true, nativeFrame: { width: 1 } });
      state = 'reconnecting';
      await vi.advanceTimersByTimeAsync(75);
      pendingFrame.resolve(packet('KVD2', 2));
      await vi.advanceTimersByTimeAsync(1);
      state = 'connected';
      await vi.advanceTimersByTimeAsync(75);
      expect(voice.tiles().map((tile) => tile.nativeFrame)).toEqual([undefined]);
    } finally {
      await voice.disconnect();
    }
  });

  it('distinguishes camera and share while accepting the previous native frame format', () => {
    expect(decodeNativeVideoFrame(packet('KVD1'))).toMatchObject({
      participant: 'alice',
      source: Track.Source.Camera
    });
    expect(decodeNativeVideoFrame(packet('KVD2', 0))).toMatchObject({
      participant: 'alice',
      source: Track.Source.Camera
    });
    expect(decodeNativeVideoFrame(packet('KVD2', 1))).toMatchObject({
      participant: 'alice',
      source: Track.Source.ScreenShare
    });
    expect(decodeNativeVideoFrame(packet('KVD2', 2))).toMatchObject({
      source: Track.Source.ScreenShare,
      local: true
    });
    expect(decodeNativeVideoFrame(packet('KVD2', 3))).toBeNull();
    expect(decodeNativeVideoFrame(packet('KVD2').slice(0, 15))).toBeNull();
  });

  it('keeps published shares visible between frames and clears ended shares in both views', () => {
    const video = new NativeVideoPresentation();
    const alice = {
      name: 'Alice Maple',
      speaking: false,
      microphone: true,
      camera: true,
      screen: true
    };
    video.applyFrame(decodeNativeVideoFrame(packet('KVD2', 2))!);
    video.setStatus({ state: 'connected', screen: false, remote_participants: {} }, true);
    expect(video.tiles()).toEqual([]);
    video.setLocalScreen(true);
    expect(video.tiles()[0]?.nativeFrame?.width).toBe(1);
    video.clearFrames();
    video.setStatus({ state: 'connected', screen: true, remote_participants: { alice } });
    const waiting = video.tiles();
    expect(waiting.map((tile) => [tile.name, tile.local, tile.nativeFrame])).toEqual([
      ['Alice Maple', false, undefined],
      ['You', true, undefined]
    ]);
    // The remote and local packets deliberately share an identity; only the native
    // discriminator is trusted to mark a frame local.
    for (const source of [0, 1, 2])
      video.applyFrame(decodeNativeVideoFrame(packet('KVD2', source))!);
    expect(video.tiles()).toHaveLength(3);
    for (const tile of waiting) {
      expect(video.tiles().find((item) => item.key === tile.key)?.nativeFrame?.width).toBe(1);
    }
    const removal = packet('KVD2', 1);
    removal[14] = 1;
    video.applyFrame(decodeNativeVideoFrame(removal)!);
    expect(
      video.tiles().find((item) => !item.local && item.source === Track.Source.ScreenShare)
        ?.nativeFrame
    ).toBeUndefined();
    alice.screen = false;
    video.setStatus({ state: 'connected', screen: false, remote_participants: { alice } });
    video.applyFrame(decodeNativeVideoFrame(packet('KVD2', 2))!);
    expect(video.tiles()).toEqual([
      expect.objectContaining({ source: Track.Source.Camera, name: 'Alice Maple' })
    ]);
    video.setStatus({ state: 'disconnected' });
    expect(video.tiles()).toEqual([]);
  });
});

it('keeps a browser share tile while the selected publication and track change', () => {
  const room = new FakeVoiceRoom();
  const voice = new VoiceSession(() => room as unknown as Room);
  const publications = room.localParticipant.trackPublications;
  const share = { kind: Track.Kind.Video, source: Track.Source.ScreenShare, isMuted: false };
  publications.set('baseline', { ...share, trackSid: 'baseline' });
  const [pending] = voice.tiles();
  expect(pending).toMatchObject({ local: true, source: Track.Source.ScreenShare });
  expect(pending.track).toBeUndefined();
  const track = (id: string) => ({
    kind: Track.Kind.Video,
    attach: vi.fn(() => ({ id, remove: vi.fn() })),
    detach: vi.fn()
  });
  const first = track('vp8');
  const second = track('av1');
  vi.stubGlobal('HTMLVideoElement', class {});
  const host = { append: vi.fn() } as unknown as HTMLElement;
  const render = attachVideo(host, pending);
  publications.set('baseline', { ...share, trackSid: 'baseline', track: first });
  publications.set('alternate', { ...share, trackSid: 'alternate', track: second });
  expect(voice.tiles()).toHaveLength(1);
  render.update(voice.tiles()[0]);
  expect(first.attach).toHaveBeenCalledOnce();
  publications.delete('baseline');
  expect(voice.tiles()[0].key).toEqual(pending.key);
  render.update(voice.tiles()[0]);
  expect(first.detach).toHaveBeenCalledWith(first.attach.mock.results[0].value);
  expect(second.attach).toHaveBeenCalledOnce();
  render.destroy();
  expect(second.detach).toHaveBeenCalledOnce();
});

it.each(['deafen', 'revoke'] as const)(
  'closes an unmute that finishes after %s',
  async (action) => {
    const candidate = new FakeVoiceRoom();
    const rooms = [new FakeVoiceRoom(), candidate];
    const voice = new VoiceSession(
      () => rooms.shift() as unknown as Room,
      async () => undefined
    );
    await voice.connect(grant({ expires_at: '2099-07-19T12:15:00Z' }), plaintextChannel);
    await voice.toggleMicrophone();
    const capture = deferred<void>();
    candidate.localParticipant.setMicrophoneEnabled.mockImplementation(
      async (enabled?: boolean) => {
        if (enabled) await capture.promise;
      }
    );
    const pending = voice.toggleMicrophone();
    await vi.waitFor(() =>
      expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(true)
    );
    if (action === 'deafen') await voice.toggleDeafen();
    else
      await voice.reconcileBrowserPermissions({
        canConnect: true,
        canSpeak: false,
        canStream: true,
        canUseVad: false
      });
    capture.resolve();
    await pending;
    expect(candidate.localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(false);
    expect(voice.microphone).toBe(false);
    if (action === 'deafen') expect(voice.deafened).toBe(true);
    else expect(voice.canSpeak).toBe(false);
  }
);
