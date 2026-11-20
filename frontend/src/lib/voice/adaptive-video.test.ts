// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { managedCodec, parseVideoMessage, videoBottleneck } from './adaptive-video';

describe('adaptive video wire and sustained stats evidence', () => {
  it('accepts only bounded capability and demand data', () => {
    const encode = (value: unknown) => new TextEncoder().encode(JSON.stringify(value));
    const value = {
      v: 1,
      codecs: ['av1', 'vp8'],
      demand: { 'alice/camera': 'vp8' },
      health: { 'alice/camera': 'decode' }
    };
    expect(parseVideoMessage(encode(value))).toEqual(value);
    expect(parseVideoMessage(encode({ ...value, encryptedH264: 2 }))?.encryptedH264).toBe(2);
    expect(parseVideoMessage(encode({ ...value, encryptedH264: '2' }))).toEqual(value);
    expect(parseVideoMessage(encode({ ...value, codecs: ['unknown'] }))).toBeUndefined();
    expect(parseVideoMessage(encode({ ...value, codecs: ['vp8', 'h265'] }))).toBeUndefined();
    expect(
      parseVideoMessage(encode({ ...value, extendedCodecs: ['h265', 'vp9', 'future'] }))
    ).toEqual({ ...value, extendedCodecs: ['h265', 'vp9'] });
    expect(parseVideoMessage(encode({ ...value, extendedCodecs: [123] }))).toBeUndefined();
    expect(
      parseVideoMessage(encode({ ...value, extendedCodecs: Array(9).fill('h265') }))
    ).toBeUndefined();
    expect(parseVideoMessage(encode({ ...value, demand: { camera: 'h265' } }))).toBeUndefined();
    expect(parseVideoMessage(encode({ ...value, demand: { camera: true } }))).toBeUndefined();
    expect(parseVideoMessage(new Uint8Array(8193))).toBeUndefined();
    expect(managedCodec({ trackName: 'kaede.video.v1:screen_share:av1' })).toBe('av1');
    expect(managedCodec({ trackName: 'camera' })).toBeUndefined();
  });
  it('uses interval processing and loss, ignoring historical cumulative overload', () => {
    const previous = {
      timestamp: 1000,
      frames: 300,
      processing: 100,
      packets: 1000,
      lost: 100,
      dropped: 20
    };
    const healthy = {
      timestamp: 4000,
      frames: 390,
      processing: 100.2,
      packets: 1300,
      lost: 100,
      dropped: 20
    };
    expect(videoBottleneck(undefined, healthy, true)).toBe('unknown');
    expect(videoBottleneck(previous, healthy, true)).toBe('healthy');
    expect(videoBottleneck(previous, { ...healthy, processing: 103 }, true)).toBe('decode');
    expect(videoBottleneck(previous, { ...healthy, lost: 130, processing: 103 }, true)).toBe(
      'download'
    );
    expect(
      videoBottleneck(
        { ...previous, receivedFrames: 300 },
        { ...healthy, frames: 300, receivedFrames: 390 },
        true
      )
    ).toBe('decode');
    expect(videoBottleneck(previous, { ...previous, timestamp: 4000 }, true)).toBe('unknown');
    expect(videoBottleneck(previous, { ...healthy, limitation: 'cpu' }, false)).toBe('encode');
    expect(videoBottleneck(previous, { ...healthy, limitation: 'bandwidth' }, false)).toBe(
      'upload'
    );
  });
});

import { vi } from 'vitest';
import {
  LocalVideoTrack,
  Track,
  type LocalTrackPublication,
  type Room,
  type TrackPublication
} from 'livekit-client';
import { AdaptiveVideoController, VIDEO_POLICY } from './adaptive-video';
import { webVideoCodecCapabilities } from './quality';

function testSender(encodings: RTCRtpEncodingParameters[] = [{ maxBitrate: 2_500_000 }]) {
  let parameters = { encodings: structuredClone(encodings) } as RTCRtpSendParameters;
  return {
    getParameters: () => structuredClone(parameters),
    setParameters: vi.fn(async (next: RTCRtpSendParameters) => {
      parameters = structuredClone(next);
    })
  } as unknown as RTCRtpSender;
}

vi.mock('./quality', async (original) => ({
  ...(await original<typeof import('./quality')>()),
  webVideoCodecCapabilities: vi.fn(async () => ({ decode: ['vp8'], encode: ['vp8'], hardware: [] }))
}));
vi.mock('livekit-client', async (original) => ({
  ...(await original<typeof import('livekit-client')>()),
  LocalVideoTrack: class {
    sid?: string;
    sender = testSender();
    async getRTCStatsReport() {
      return new Map();
    }
    constructor(public mediaStreamTrack: MediaStreamTrack) {}
    stop() {
      this.mediaStreamTrack.stop();
    }
  }
}));

it.each([true, false])(
  'reuses capture and disables backups while preserving encryption=%s',
  async (encrypted) => {
    const clone = { contentHint: '', stop: vi.fn() };
    const media = {
      getSettings: () => ({ width: 1280, height: 720, frameRate: 30 }),
      clone: vi.fn(() => clone),
      contentHint: 'motion',
      stop: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn()
    };
    const capture = new LocalVideoTrack(media as unknown as MediaStreamTrack);
    const publishTrack = vi.fn(async (track: LocalVideoTrack) => {
      track.sid = 'variant';
      return {};
    });
    const unpublishTrack = vi.fn(async () => {});
    const room = {
      localParticipant: { identity: 'alice', publishTrack, unpublishTrack },
      remoteParticipants: new Map(),
      isE2EEEnabled: encrypted
    } as unknown as Room;
    const controller = new AdaptiveVideoController(room);
    await controller.managePublished({
      source: Track.Source.Camera,
      track: capture
    } as unknown as LocalTrackPublication);
    expect(media.clone).toHaveBeenCalledOnce();
    expect(publishTrack).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        backupCodec: false,
        videoCodec: 'vp8',
        name: 'kaede.video.v1:camera:vp8',
        source: Track.Source.Camera
      })
    );
    expect(unpublishTrack).toHaveBeenCalledWith(capture, false);
    expect(media.stop).not.toHaveBeenCalled();
    expect(
      controller.isVisible(
        {
          source: Track.Source.Camera,
          trackSid: 'original',
          trackName: 'camera'
        } as TrackPublication,
        true
      )
    ).toBe(false);
    expect(
      controller.isVisible(
        { source: Track.Source.Camera, trackSid: 'variant' } as TrackPublication,
        true
      )
    ).toBe(true);
    expect(room.isE2EEEnabled).toBe(encrypted);
    await controller.stopSource(Track.Source.Camera);
    expect(clone.stop).toHaveBeenCalledOnce();
    expect(media.stop).toHaveBeenCalledOnce();
  }
);

it('explicitly preserves audio and selects exactly one managed video per source', async () => {
  const publication = (name: string, kind: Track.Kind) => ({
    trackName: name,
    trackSid: name,
    source: Track.Source.Camera,
    kind,
    setSubscribed: vi.fn(),
    setVideoFPS: vi.fn()
  });
  const audio = publication('microphone', Track.Kind.Audio);
  const av1 = publication('kaede.video.v1:camera:av1', Track.Kind.Video);
  const vp8 = publication('kaede.video.v1:camera:vp8', Track.Kind.Video);
  const legacy = publication('camera', Track.Kind.Video);
  const room = {
    on: vi.fn(),
    off: vi.fn(),
    remoteParticipants: new Map([
      [
        'bob',
        {
          identity: 'bob',
          trackPublications: new Map([
            ['a', audio],
            ['v1', av1],
            ['v2', vp8],
            ['old', legacy]
          ])
        }
      ]
    ]),
    localParticipant: { publishData: vi.fn(async () => {}) }
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room);
  controller.start();
  expect(audio.setSubscribed).toHaveBeenCalledWith(true);
  expect(av1.setSubscribed).toHaveBeenCalledWith(false);
  expect(legacy.setSubscribed).toHaveBeenCalledWith(false);
  expect(controller.isVisible(legacy as unknown as TrackPublication)).toBe(false);
  expect(vp8.setSubscribed).toHaveBeenCalledWith(true);
  expect(controller.isVisible(vp8 as unknown as TrackPublication)).toBe(true);
  expect(controller.isVisible(av1 as unknown as TrackPublication)).toBe(false);
  await controller.stop();
});

it('does not publish after stopping while the capability probe is in flight', async () => {
  let resolve!: (value: { decode: string[]; encode: string[]; hardware: string[] }) => void;
  vi.mocked(webVideoCodecCapabilities).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      })
  );
  const capture = new LocalVideoTrack({
    getSettings: () => ({ width: 1280, height: 720, frameRate: 30 })
  } as unknown as MediaStreamTrack);
  const publishTrack = vi.fn();
  const room = {
    on: vi.fn(),
    off: vi.fn(),
    remoteParticipants: new Map(),
    localParticipant: { publishTrack }
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room);
  const pending = controller.managePublished({
    source: Track.Source.Camera,
    track: capture
  } as unknown as LocalTrackPublication);
  await Promise.resolve();
  await controller.stop();
  resolve({ decode: ['vp8'], encode: ['vp8'], hardware: [] });
  await pending;
  expect(publishTrack).not.toHaveBeenCalled();
});

it('keeps AV1 for a capable viewer alongside fallback, then honors receiver-local fallback demand', async () => {
  vi.mocked(webVideoCodecCapabilities).mockResolvedValueOnce({
    decode: ['av1', 'vp8'],
    encode: ['av1', 'vp8'],
    hardware: ['av1']
  });
  const capture = new LocalVideoTrack({
    getSettings: () => ({ width: 1280, height: 720, frameRate: 30 }),
    clone: () => ({ stop: vi.fn() }),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    stop: vi.fn()
  } as unknown as MediaStreamTrack);
  const bob = { identity: 'bob', trackPublications: new Map(), videoTrackPublications: new Map() };
  const eve = { identity: 'eve', trackPublications: new Map(), videoTrackPublications: new Map() };
  const events = new Map<string, (...args: unknown[]) => void>();
  let now = 100_000;
  let limitation = 'cpu';
  const clock = vi.spyOn(Date, 'now').mockImplementation(() => now);
  const publishTrack = vi.fn(async (track: LocalVideoTrack, options: { name: string }) => {
    track.sid = options.name;
    track.getRTCStatsReport = async () =>
      new Map([
        [
          'out',
          {
            id: 'out',
            type: 'outbound-rtp',
            kind: 'video',
            timestamp: now,
            framesEncoded: (now / 1000) * 30,
            totalEncodeTime: (now / 1000) * 0.01,
            qualityLimitationReason: limitation,
            powerEfficientEncoder: true,
            encoderImplementation: 'VideoToolbox'
          }
        ]
      ]) as unknown as RTCStatsReport;
  });
  const room = {
    on: vi.fn((event: string, callback: (...args: unknown[]) => void) =>
      events.set(event, callback)
    ),
    off: vi.fn(),
    remoteParticipants: new Map([
      ['bob', bob],
      ['eve', eve]
    ]),
    localParticipant: {
      identity: 'alice',
      publishTrack,
      unpublishTrack: vi.fn(async () => {}),
      publishData: vi.fn(async () => {})
    }
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room);
  controller.start();
  const send = (demand: Record<string, string>) =>
    events.get('dataReceived')!(
      new TextEncoder().encode(
        JSON.stringify({ v: 1, codecs: ['av1', 'vp8'], demand, health: {} })
      ),
      bob,
      undefined,
      'kaede.video.v1'
    );
  send({});
  await controller.managePublished({
    source: Track.Source.Camera,
    track: capture
  } as unknown as LocalTrackPublication);
  expect(publishTrack.mock.calls.map(([, options]) => options.name)).toEqual([
    'kaede.video.v1:camera:vp8',
    'kaede.video.v1:camera:av1'
  ]);
  const tick = async () => {
    now += 3000;
    await (controller as unknown as { tick(): Promise<void> }).tick();
  };
  for (let i = 0; i < 11; i++) await tick();
  expect(room.localParticipant.unpublishTrack).toHaveBeenCalledWith(
    expect.objectContaining({ sid: 'kaede.video.v1:camera:av1' }),
    true
  );
  const av1Publishes = () =>
    publishTrack.mock.calls.filter(([, options]) => options.name.endsWith(':av1')).length;
  expect(av1Publishes()).toBe(1);
  limitation = 'none';
  // A CPU shed is remembered: recovery alone must not re-add AV1 within its backoff.
  for (let i = 0; i < 10; i++) await tick();
  expect(av1Publishes()).toBe(1);
  for (let i = 0; i < 20; i++) await tick();
  expect(av1Publishes()).toBe(2);
  send({ 'alice/camera': 'vp8' });
  await (controller as unknown as { tick(): Promise<void> }).tick();
  expect(room.localParticipant.unpublishTrack).toHaveBeenCalledWith(
    expect.objectContaining({ sid: 'kaede.video.v1:camera:av1' }),
    true
  );
  await controller.stop();
  clock.mockRestore();
});

it('reports picker closure during capability probing without publishing an ended clone', async () => {
  const capture = new LocalVideoTrack({
    getSettings: () => ({ width: 1280, height: 720, frameRate: 30 }),
    readyState: 'ended'
  } as unknown as MediaStreamTrack);
  const publishTrack = vi.fn();
  const ended = vi.fn();
  const room = { localParticipant: { publishTrack } } as unknown as Room;
  const controller = new AdaptiveVideoController(room, undefined, ended);
  await controller.managePublished({
    source: Track.Source.ScreenShare,
    track: capture
  } as unknown as LocalTrackPublication);
  expect(publishTrack).not.toHaveBeenCalled();
  expect(ended).toHaveBeenCalledWith(Track.Source.ScreenShare);
});

it("accepts the publisher's compatible fallback instead of returning a struggling viewer to AV1", async () => {
  const publication = (codec: string) => ({
    trackName: `kaede.video.v1:camera:${codec}`,
    trackSid: codec,
    source: Track.Source.Camera,
    kind: Track.Kind.Video,
    setSubscribed: vi.fn(),
    setVideoFPS: vi.fn()
  });
  const av1 = publication('av1'),
    h264 = publication('h264');
  const room = {
    on: vi.fn(),
    off: vi.fn(),
    remoteParticipants: new Map([
      [
        'bob',
        {
          identity: 'bob',
          trackPublications: new Map([
            ['av1', av1],
            ['h264', h264]
          ])
        }
      ]
    ]),
    localParticipant: { publishData: vi.fn(async () => {}) }
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room);
  controller.start();
  const internals = controller as unknown as {
    message: {
      v: 1;
      codecs: string[];
      demand: Record<string, string>;
      health: Record<string, string>;
    };
    selectSubscriptions(): void;
  };
  internals.message = {
    v: 1,
    codecs: ['av1', 'vp8', 'h264'],
    demand: { 'bob/camera': 'vp8' },
    health: { 'bob/camera': 'decode' }
  };
  internals.selectSubscriptions();
  expect(h264.setSubscribed).toHaveBeenLastCalledWith(true);
  expect(av1.setSubscribed).toHaveBeenLastCalledWith(false);
  await controller.stop();
});

it('unsubscribes hidden video, retains audio, and resumes after visibility changes', async () => {
  const audio = { kind: Track.Kind.Audio, trackSid: 'audio', setSubscribed: vi.fn() };
  const camera = {
    kind: Track.Kind.Video,
    source: Track.Source.Camera,
    trackName: 'camera',
    trackSid: 'camera',
    setSubscribed: vi.fn()
  };
  const screen = {
    ...camera,
    source: Track.Source.ScreenShare,
    trackName: 'screen',
    trackSid: 'screen',
    setSubscribed: vi.fn()
  };
  const publications = new Map([
    ['audio', audio],
    ['camera', camera],
    ['screen', screen]
  ]);
  const room = {
    on: vi.fn(),
    off: vi.fn(),
    localParticipant: { publishData: vi.fn() },
    remoteParticipants: new Map([['alice', { identity: 'alice', trackPublications: publications }]])
  } as unknown as Room;
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
  const controller = new AdaptiveVideoController(room);
  try {
    controller.start();
    expect(audio.setSubscribed).toHaveBeenLastCalledWith(true);
    expect(camera.setSubscribed).toHaveBeenLastCalledWith(false);
    expect(screen.setSubscribed).toHaveBeenLastCalledWith(false);
    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(camera.setSubscribed).toHaveBeenLastCalledWith(true);
    expect(screen.setSubscribed).toHaveBeenLastCalledWith(true);
    visibility.mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(camera.setSubscribed).toHaveBeenLastCalledWith(false);
    expect(screen.setSubscribed).toHaveBeenLastCalledWith(false);
    expect(audio.setSubscribed).toHaveBeenLastCalledWith(true);
  } finally {
    await controller.stop();
    visibility.mockRestore();
  }
});

it('adopts a correctly named VP8 publication instead of republishing it', async () => {
  const media = {
    getSettings: () => ({ width: 1280, height: 720, frameRate: 30 }),
    clone: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    stop: vi.fn()
  };
  const capture = new LocalVideoTrack(media as unknown as MediaStreamTrack);
  capture.sid = 'original';
  const publishTrack = vi.fn();
  const unpublishTrack = vi.fn(async () => {});
  const room = {
    localParticipant: { identity: 'alice', publishTrack, unpublishTrack },
    remoteParticipants: new Map()
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room);
  await controller.managePublished({
    source: Track.Source.Camera,
    trackName: 'kaede.video.v1:camera:vp8',
    track: capture
  } as unknown as LocalTrackPublication);
  expect(media.clone).not.toHaveBeenCalled();
  expect(publishTrack).not.toHaveBeenCalled();
  expect(unpublishTrack).not.toHaveBeenCalled();
  expect(
    controller.isVisible(
      { source: Track.Source.Camera, trackSid: 'original' } as TrackPublication,
      true
    )
  ).toBe(true);
});

it('scales the simulcast ladder proportionally instead of flattening it', async () => {
  const media = {
    getSettings: () => ({ width: 1280, height: 720, frameRate: 30 }),
    clone: () => ({ stop: vi.fn() }),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    stop: vi.fn()
  };
  const capture = new LocalVideoTrack(media as unknown as MediaStreamTrack);
  let parameters = {
    encodings: [
      { maxBitrate: 150_000, maxFramerate: 15, scaleResolutionDownBy: 4 },
      { maxBitrate: 500_000, maxFramerate: 30, scaleResolutionDownBy: 2 },
      { maxBitrate: 2_500_000, maxFramerate: 30, scaleResolutionDownBy: 1 }
    ]
  } as unknown as RTCRtpSendParameters;
  const setParameters = vi.fn(async (next: RTCRtpSendParameters) => {
    parameters = structuredClone(next);
  });
  const room = {
    on: vi.fn(),
    off: vi.fn(),
    remoteParticipants: new Map(),
    localParticipant: {
      identity: 'alice',
      publishTrack: vi.fn(async (track: LocalVideoTrack) => {
        track.sid = 'variant';
        Object.assign(track, {
          sender: { getParameters: () => structuredClone(parameters), setParameters },
          getRTCStatsReport: async () => new Map()
        });
      }),
      unpublishTrack: vi.fn(async () => {}),
      publishData: vi.fn(async () => {})
    }
  } as unknown as Room;
  Object.assign(capture, {
    sender: { getParameters: () => structuredClone(parameters), setParameters },
    getRTCStatsReport: async () => new Map()
  });
  const controller = new AdaptiveVideoController(room);
  await controller.managePublished(
    {
      source: Track.Source.Camera,
      track: capture,
      trackName: 'kaede.video.v1:camera:vp8'
    } as unknown as LocalTrackPublication,
    { videoEncoding: { maxBitrate: 3_150_000, maxFramerate: 30 } }
  );
  const internals = controller as unknown as { tick(): Promise<void>; level: number };
  await internals.tick();
  expect(parameters.encodings.map((e) => e.maxBitrate)).toEqual([150_000, 500_000, 2_500_000]);
  expect(parameters.encodings.map((e) => e.scaleResolutionDownBy)).toEqual([4, 2, 1]);
  internals.level = 2;
  await internals.tick();
  expect(parameters.encodings.map((e) => e.maxBitrate)).toEqual([150_000, 259_200, 1_296_000]);
  expect(parameters.encodings[2].scaleResolutionDownBy).toBeCloseTo(1.5625);
  // Restoring the level returns to the SDK's original ladder, not a compounded one.
  internals.level = 0;
  await internals.tick();
  expect(parameters.encodings.map((e) => e.maxBitrate)).toEqual([150_000, 500_000, 2_500_000]);
  await controller.stop();
});

it('keeps a struggling viewer on the fallback until the AV1 backoff expires', async () => {
  let now = 500_000;
  const clock = vi.spyOn(Date, 'now').mockImplementation(() => now);
  let decodeTime = 0;
  let decoded = 0;
  const stats = () =>
    new Map([
      [
        'in',
        {
          id: 'in',
          type: 'inbound-rtp',
          kind: 'video',
          timestamp: now,
          framesDecoded: decoded,
          framesReceived: decoded,
          totalDecodeTime: decodeTime,
          packetsReceived: decoded * 10,
          packetsLost: 0
        }
      ]
    ]) as unknown as RTCStatsReport;
  const publication = (codec: string) => ({
    trackName: `kaede.video.v1:camera:${codec}`,
    trackSid: codec,
    source: Track.Source.Camera,
    kind: Track.Kind.Video,
    setSubscribed: vi.fn(),
    setVideoFPS: vi.fn(),
    setVideoQuality: vi.fn(),
    track: { getRTCStatsReport: async () => stats() }
  });
  const av1 = publication('av1'),
    vp8 = publication('vp8');
  const all = new Map([
    ['av1', av1],
    ['vp8', vp8]
  ]);
  const room = {
    on: vi.fn(),
    off: vi.fn(),
    remoteParticipants: new Map([
      ['bob', { identity: 'bob', trackPublications: all, videoTrackPublications: all }]
    ]),
    localParticipant: { identity: 'alice', publishData: vi.fn(async () => {}) }
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room);
  const internals = controller as unknown as {
    tick(): Promise<void>;
    message: { codecs: string[]; demand: Record<string, string> };
    selectSubscriptions(): void;
  };
  controller.start();
  internals.message.codecs = ['av1', 'vp8'];
  internals.selectSubscriptions();
  expect(av1.setSubscribed).toHaveBeenLastCalledWith(true);
  const tick = async (seconds: number, overloaded: boolean) => {
    now += 2000;
    decoded += 60;
    decodeTime += overloaded ? seconds : 0.01;
    await internals.tick();
  };
  for (let i = 0; i < 4; i++) await tick(2, true);
  expect(internals.message.demand['bob/camera']).toBe('vp8');
  // Healthy on the fallback is not evidence that AV1 now plays smoothly.
  for (let i = 0; i < 20; i++) await tick(2, false);
  expect(internals.message.demand['bob/camera']).toBe('vp8');
  for (let i = 0; i < 15; i++) await tick(2, false);
  expect(internals.message.demand['bob/camera']).toBeUndefined();
  await controller.stop();
  clock.mockRestore();
});

it('does not flip the fallback codec while a new viewer has not sent hints', () => {
  const internals = (participants: string[], peers: Record<string, string[]>) => {
    const controller = new AdaptiveVideoController({
      localParticipant: { identity: 'alice' },
      remoteParticipants: new Map(participants.map((id) => [id, { identity: id }]))
    } as unknown as Room);
    const raw = controller as unknown as {
      peers: Map<string, { codecs: string[]; demand: object; health: object }>;
      fallback(state: unknown): string;
    };
    for (const [id, codecs] of Object.entries(peers))
      raw.peers.set(id, { codecs, demand: {}, health: {} });
    return raw;
  };
  const state = (current: string) => ({
    options: { source: Track.Source.Camera },
    variants: new Map([[current, {}]]),
    encode: ['vp8', 'h264'],
    hardware: ['h264']
  });
  expect(internals(['bob'], { bob: ['vp8', 'h264'] }).fallback(state('h264'))).toBe('h264');
  expect(internals(['bob', 'new'], { bob: ['vp8', 'h264'] }).fallback(state('h264'))).toBe('h264');
  expect(internals(['bob', 'new'], { bob: ['vp8', 'h264'] }).fallback(state('vp9'))).toBe('vp8');
  expect(
    internals(['bob', 'old'], { bob: ['vp8', 'h264'], old: ['vp8'] }).fallback(state('h264'))
  ).toBe('vp8');
});

it('withdraws encrypted H.264 for legacy joiners and reconnects, and restores it for confirmed peers', async () => {
  const changes: string[] = [];
  let failVp8 = false;
  const received = (codec: string) => ({
    trackName: `kaede.video.v1:camera:${codec}`,
    trackSid: codec,
    source: Track.Source.Camera,
    kind: Track.Kind.Video,
    setSubscribed: vi.fn(),
    setVideoFPS: vi.fn()
  });
  const h264 = received('h264');
  const vp8 = received('vp8');
  const mismatched = { ...received('vp8'), trackSid: 'substituted', mimeType: 'video/H264' };
  const unknown = {
    ...received('vp8'),
    trackSid: 'unknown',
    source: Track.Source.ScreenShare,
    trackName: 'screen'
  };
  const modern = {
    identity: 'modern',
    trackPublications: new Map([
      ['h264', h264],
      ['vp8', vp8],
      ['substituted', mismatched],
      ['unknown', unknown]
    ])
  };
  const legacy = { identity: 'legacy', trackPublications: new Map() };
  const participants = new Map([[modern.identity, modern]]);
  const sent: { codecs: string[]; encryptedH264?: number }[] = [];
  const room = {
    localParticipant: {
      identity: 'local',
      publishData: async (bytes: Uint8Array) =>
        sent.push(JSON.parse(new TextDecoder().decode(bytes))),
      publishTrack: async (track: LocalVideoTrack, options: { videoCodec: string }) => {
        if (failVp8 && options.videoCodec === 'vp8') {
          changes.push('vp8 failed');
          throw new Error('VP8 publication failed');
        }
        track.sid = options.videoCodec;
        changes.push('+' + track.sid);
      },
      unpublishTrack: async (track: LocalVideoTrack) => changes.push('-' + track.sid)
    },
    remoteParticipants: participants
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room, undefined, undefined, true);
  const internal = controller as unknown as {
    message: { codecs: string[] };
    connected(participant: unknown): void;
    disconnected(participant: unknown): void;
    reconnected(): void;
    announce(): Promise<void>;
    data(bytes: Uint8Array, participant: unknown, kind: undefined, topic: string): void;
    pending: Promise<void>;
  };
  internal.message.codecs = ['vp8', 'h264', 'av1'];
  const tell = (peer: unknown, marker?: number) =>
    internal.data(
      new TextEncoder().encode(
        JSON.stringify({
          v: 1,
          codecs: ['vp8', 'h264', 'av1'],
          demand: {},
          health: {},
          encryptedH264: marker
        })
      ),
      peer,
      undefined,
      'kaede.video.v1'
    );
  const capture = new LocalVideoTrack({
    getSettings: () => ({ width: 1280, height: 720, frameRate: 30 }),
    addEventListener: vi.fn(),
    clone: () => ({ stop: vi.fn() })
  } as unknown as MediaStreamTrack);
  capture.sid = 'vp8';
  vi.mocked(webVideoCodecCapabilities).mockResolvedValueOnce({
    decode: ['vp8', 'h264'],
    encode: ['vp8', 'h264'],
    hardware: ['h264']
  });
  await controller.managePublished({
    source: Track.Source.Camera,
    track: capture,
    trackName: 'kaede.video.v1:camera:vp8'
  } as unknown as LocalTrackPublication);

  internal.connected(modern);
  expect(sent.at(-1)).toMatchObject({ codecs: ['vp8', 'av1'], encryptedH264: 2 });
  tell(modern, 2);
  await internal.pending;
  expect(sent.at(-1)?.codecs).toContain('h264');
  expect(changes).toEqual(['+h264', '-vp8']);
  expect(h264.setSubscribed).toHaveBeenLastCalledWith(true);
  const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.now() + VIDEO_POLICY.hintExpiryMs + 1);
  try {
    await internal.announce();
    expect(sent.at(-1)?.codecs).toEqual(['vp8', 'av1']);
  } finally {
    clock.mockRestore();
  }

  participants.set(legacy.identity, legacy);
  internal.connected(legacy);
  // 0.1.104 checks every viewer's legacy codecs list, ignoring the new marker.
  tell(legacy);
  await internal.pending;
  expect(sent.at(-1)).toMatchObject({ codecs: ['vp8', 'av1'], encryptedH264: 2 });
  expect(changes.slice(-2)).toEqual(['+vp8', '-h264']);
  expect(h264.setSubscribed).toHaveBeenLastCalledWith(false);
  expect(mismatched.setSubscribed).toHaveBeenLastCalledWith(false);
  expect(unknown.setSubscribed).toHaveBeenLastCalledWith(false);
  expect(vp8.setSubscribed).toHaveBeenLastCalledWith(true);

  participants.delete(legacy.identity);
  internal.disconnected(legacy);
  await internal.pending;
  expect(changes.slice(-2)).toEqual(['+h264', '-vp8']);
  internal.reconnected();
  await internal.pending;
  expect(sent.at(-1)?.codecs).toEqual(['vp8', 'av1']);
  expect(changes.slice(-2)).toEqual(['+vp8', '-h264']);

  tell(modern, 2);
  await internal.pending;
  failVp8 = true;
  internal.data(new Uint8Array(8193), modern, undefined, 'kaede.video.v1');
  await internal.pending;
  expect(sent.at(-1)?.codecs).toEqual(['vp8', 'av1']);
  expect(changes.slice(-2)).toEqual(['vp8 failed', '-h264']);
  failVp8 = false;
  internal.reconnected();
  await internal.pending;
  expect(changes.at(-1)).toBe('+vp8');
});

it('keeps legacy-readable capability hints when call feedback exceeds the wire limits', async () => {
  const publishData = vi.fn(async (_bytes: Uint8Array) => {});
  const room = {
    localParticipant: { publishData },
    remoteParticipants: new Map()
  } as unknown as Room;
  const internal = new AdaptiveVideoController(room, undefined, undefined, true) as unknown as {
    message: { codecs: string[]; demand: Record<string, string>; health: Record<string, string> };
    announce(): Promise<void>;
  };
  internal.message.codecs = ['vp8', 'h264', 'av1'];
  internal.message.demand = { ['é'.repeat(129)]: 'vp8', 'old/camera': 'h264' };
  for (let n = 0; n < 100; n++) {
    const key = `${n}@${'a'.repeat(80)}.example/camera`;
    internal.message.demand[key] = 'vp8';
    internal.message.health[key] = 'decode';
  }
  await internal.announce();
  const bytes = publishData.mock.calls[0][0];
  expect(bytes.byteLength).toBeLessThanOrEqual(8192);
  const hint = parseVideoMessage(bytes)!;
  expect(hint).toMatchObject({ codecs: ['vp8', 'av1'], encryptedH264: 2 });
  expect(Object.keys(hint.demand)).toHaveLength(64);
  expect(hint.demand).not.toHaveProperty('old/camera');
  expect(hint.demand).not.toHaveProperty('é'.repeat(129));
});

it('withdraws software HEVC immediately during cooldown and requires a fresh hardware proof', async () => {
  let now = 500_000;
  const clock = vi.spyOn(Date, 'now').mockImplementation(() => now);
  const publication = (codec: string) => ({
    trackName: `kaede.video.v1:camera:${codec}`,
    trackSid: codec,
    source: Track.Source.Camera,
    kind: Track.Kind.Video,
    setSubscribed: vi.fn(),
    setVideoFPS: vi.fn(),
    setVideoQuality: vi.fn(),
    track: {
      getRTCStatsReport: async () =>
        new Map([
          [
            'in',
            {
              id: 'in',
              type: 'inbound-rtp',
              timestamp: now,
              framesDecoded: 3,
              decoderImplementation: 'software',
              powerEfficientDecoder: false
            }
          ]
        ])
    }
  });
  const hevc = publication('h265'),
    vp8 = publication('vp8');
  const all = new Map([
    ['h265', hevc],
    ['vp8', vp8]
  ]);
  const controller = new AdaptiveVideoController({
    remoteParticipants: new Map([
      ['bob', { identity: 'bob', trackPublications: all, videoTrackPublications: all }]
    ]),
    localParticipant: { publishData: vi.fn(async () => {}) }
  } as unknown as Room);
  const internals = controller as unknown as {
    tick(): Promise<void>;
    selectSubscriptions(): void;
    probePremiumDecode(): Promise<void>;
    message: { codecs: string[]; extendedCodecs: string[]; demand: Record<string, string> };
    receiverEvidence: Map<string, { bad: number; good: number; changed: number }>;
  };
  internals.message.extendedCodecs = ['h265'];
  internals.receiverEvidence.set('bob/camera', { bad: 0, good: 0, changed: now });
  const probe = vi.spyOn(internals, 'probePremiumDecode').mockResolvedValue();
  try {
    internals.selectSubscriptions();
    expect(hevc.setSubscribed).toHaveBeenLastCalledWith(true);
    await internals.tick();
    expect(internals.message.demand['bob/camera']).toBe('vp8');
    expect(internals.message.extendedCodecs).not.toContain('h265');
    expect(hevc.setSubscribed).toHaveBeenLastCalledWith(false);
    expect(vp8.setSubscribed).toHaveBeenLastCalledWith(true);
    now += 59_000;
    await internals.tick();
    expect(probe).not.toHaveBeenCalled();
    now += 1_000;
    await internals.tick();
    expect(probe).toHaveBeenCalledExactlyOnceWith(['h265']);
    // An unsuccessful fresh probe cannot restore the withdrawn codec hint.
    expect(internals.message.extendedCodecs).not.toContain('h265');
  } finally {
    probe.mockRestore();
    clock.mockRestore();
  }
});

it('uses the bandwidth estimate to gate adding AV1, never to drop a published track', async () => {
  const hint = (codecs: string[]) => ({ codecs, demand: {}, health: {} });
  const publishTrack = vi.fn(async (track: LocalVideoTrack) => {
    track.sid = 'new';
  });
  const unpublishTrack = vi.fn(async () => {});
  const room = {
    localParticipant: { identity: 'alice', publishTrack, unpublishTrack },
    remoteParticipants: new Map([
      ['bob', { identity: 'bob' }],
      ['eve', { identity: 'eve' }]
    ])
  } as unknown as Room;
  const controller = new AdaptiveVideoController(room);
  const internals = controller as unknown as {
    peers: Map<string, unknown>;
    sources: Map<Track.Source, unknown>;
    availableUpload?: number;
    reconcile(state: unknown): Promise<void>;
  };
  internals.peers.set('bob', hint(['av1', 'vp8']));
  internals.peers.set('eve', hint(['vp8']));
  const state = (variants: string[]) => ({
    capture: { mediaStreamTrack: { readyState: 'live', clone: () => ({}) } },
    options: { source: Track.Source.Camera },
    variants: new Map(variants.map((codec) => [codec, { sid: codec, sender: testSender() }])),
    publishedAt: new Map(),
    audience: new Map(),
    hardware: ['av1'],
    reprobe: new Set(),
    modes: {},
    encode: ['av1', 'vp8'],
    bitrate: 2_500_000,
    fps: 30,
    width: 1280,
    height: 720
  });
  internals.sources.set(Track.Source.Camera, {});
  internals.availableUpload = 1_000_000;
  await internals.reconcile(state(['vp8']));
  expect(publishTrack).not.toHaveBeenCalled();
  await internals.reconcile(state(['vp8', 'av1']));
  expect(unpublishTrack).not.toHaveBeenCalled();
  internals.availableUpload = 5_000_000;
  await internals.reconcile(state(['vp8']));
  expect(publishTrack).toHaveBeenCalledWith(
    expect.anything(),
    expect.objectContaining({ name: 'kaede.video.v1:camera:av1' })
  );
});

it.each([
  [
    ['av1', 'h265'],
    ['av1', 'h265']
  ],
  [['h265'], ['h265']],
  [['av1'], ['h265'], ['h265']],
  [['av1', 'h265'], ['vp8'], ['vp8'], ['vp8']]
])('selects one hardware premium by audience with AV1 winning ties: %j', async (...audiences) => {
  const publishTrack = vi.fn(async (track: LocalVideoTrack) => {
    track.sid = 'new';
  });
  const unpublishTrack = vi.fn(async () => {});
  const participants = new Map(
    audiences.map((_, index) => [String(index), { identity: String(index) }])
  );
  const controller = new AdaptiveVideoController({
    localParticipant: { identity: 'alice', publishTrack, unpublishTrack },
    remoteParticipants: participants
  } as unknown as Room);
  const internal = controller as unknown as {
    peers: Map<string, unknown>;
    sources: Map<Track.Source, unknown>;
    reconcile(state: unknown): Promise<void>;
  };
  audiences.forEach((codecs, index) =>
    internal.peers.set(String(index), {
      codecs: ['vp8', ...codecs.filter((codec) => codec === 'av1')],
      extendedCodecs: codecs.filter((codec) => codec === 'h265'),
      demand: {},
      health: {}
    })
  );
  const state = {
    capture: { mediaStreamTrack: { readyState: 'live', clone: () => ({}) } },
    options: { source: Track.Source.Camera },
    variants: new Map([['vp8', { sid: 'vp8', sender: testSender() }]]),
    publishedAt: new Map(),
    audience: new Map(),
    hardware: ['av1', 'h265'],
    reprobe: new Set(),
    modes: { av1: 'L1T3', h265: 'L1T1' },
    encode: ['av1', 'h265', 'vp8'],
    bitrate: 2_500_000,
    fps: 30,
    width: 1280,
    height: 720
  };
  internal.sources.set(Track.Source.Camera, state);
  await internal.reconcile(state);
  const av1 = audiences.filter((codecs) => codecs.includes('av1')).length;
  const hevc = audiences.filter((codecs) => codecs.includes('h265')).length;
  const enough = Math.max(av1, hevc) >= Math.ceil(audiences.length / 3);
  const premium = av1 >= hevc ? 'av1' : 'h265';
  expect([...state.variants.keys()].filter((codec) => codec !== 'vp8')).toEqual(
    enough ? [premium] : []
  );
  expect(state.variants.size).toBeLessThanOrEqual(2);
  if (enough)
    expect(publishTrack).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        videoCodec: premium,
        scalabilityMode: state.modes[premium as 'av1' | 'h265'],
        backupCodec: false
      })
    );
});

it('admits VP9 only for a fully capable audience with measured spare CPU on a constrained uplink', () => {
  const controller = new AdaptiveVideoController({
    localParticipant: { identity: 'alice' },
    remoteParticipants: new Map([['bob', { identity: 'bob' }]])
  } as unknown as Room);
  const internal = controller as unknown as {
    peers: Map<string, unknown>;
    uploadConstrained: boolean;
    level: number;
    good: number;
    encodeLoad: number;
    encodesMeasured: boolean;
    lastSpareCpuAt: number;
    fallback(state: unknown): string;
  };
  const state = {
    options: { source: Track.Source.Camera },
    variants: new Map([['vp8', {}]]),
    hardware: ['h265'],
    modes: {} as { vp9?: string },
    encode: ['vp8', 'vp9', 'h265']
  };
  internal.peers.set('bob', { codecs: ['vp8'], extendedCodecs: ['vp9', 'h265'], demand: {} });
  internal.good = 15;
  internal.encodeLoad = 0.2;
  internal.encodesMeasured = true;
  expect(internal.fallback(state)).toBe('vp8');
  internal.uploadConstrained = true;
  expect(internal.fallback(state)).toBe('vp8');
  state.modes.vp9 = 'L1T3';
  expect(internal.fallback(state)).toBe('vp9');
  state.variants = new Map([['vp9', {}]]);
  internal.uploadConstrained = false;
  internal.good = 0;
  // Successful bandwidth relief must not undo the codec that provided it.
  expect(internal.fallback(state)).toBe('vp9');
  internal.encodesMeasured = false;
  internal.lastSpareCpuAt = Date.now();
  expect(internal.fallback(state)).toBe('vp9');
  internal.lastSpareCpuAt = Date.now() - VIDEO_POLICY.uploadWarmupMs;
  expect(internal.fallback(state)).toBe('vp8');
  internal.encodesMeasured = true;
  state.variants = new Map([['vp8', {}]]);
  internal.uploadConstrained = true;
  internal.good = 0;
  expect(internal.fallback(state)).toBe('vp8');
  internal.good = 15;
  internal.encodeLoad = 0.7;
  expect(internal.fallback(state)).toBe('vp8');
  internal.encodeLoad = 0.2;
  internal.peers.set('bob', { codecs: ['vp8'], demand: {} });
  expect(internal.fallback(state)).toBe('vp8');
});

it.each([false, true])(
  'keeps compatible playback while premium publication fails=%s',
  async (fails) => {
    const live = new Set(['vp8']);
    let parameters = { encodings: [{ maxBitrate: 2_500_000 }] } as RTCRtpSendParameters;
    const sender = {
      getParameters: () => structuredClone(parameters),
      setParameters: vi.fn(async (next: RTCRtpSendParameters) => {
        parameters = structuredClone(next);
      })
    };
    const publishTrack = vi.fn(
      async (
        track: LocalVideoTrack,
        options: { videoCodec: string; videoSendEncodings: RTCRtpEncodingParameters[] }
      ) => {
        expect(parameters.encodings[0].maxBitrate).toBeLessThanOrEqual(1_250_000);
        expect(
          options.videoSendEncodings.reduce((sum, layer) => sum + layer.maxBitrate!, 0)
        ).toBeLessThanOrEqual(1_250_000);
        expect(live.size).toBeGreaterThan(0);
        if (fails) throw new Error('encoder busy');
        track.sid = options.videoCodec;
        live.add(options.videoCodec);
        expect(live.size).toBeLessThanOrEqual(2);
      }
    );
    const controller = new AdaptiveVideoController({
      localParticipant: {
        identity: 'alice',
        publishTrack,
        unpublishTrack: vi.fn(async (track: LocalVideoTrack) => {
          live.delete(track.sid!);
        })
      },
      remoteParticipants: new Map([['bob', { identity: 'bob' }]])
    } as unknown as Room);
    const internal = controller as unknown as {
      peers: Map<string, unknown>;
      sources: Map<Track.Source, unknown>;
      reconcile(state: unknown): Promise<void>;
    };
    internal.peers.set('bob', { codecs: ['vp8', 'h264', 'av1'], demand: {} });
    const state = {
      capture: { mediaStreamTrack: { readyState: 'live', clone: () => ({ stop: vi.fn() }) } },
      options: { source: Track.Source.Camera },
      variants: new Map([['vp8', { sid: 'vp8', sender }]]),
      publishedAt: new Map(),
      audience: new Map(),
      hardware: ['h264', 'av1'],
      modes: { av1: 'L1T1' },
      encode: ['vp8', 'h264', 'av1'],
      bitrate: 2_500_000,
      fps: 30,
      width: 1280,
      height: 720
    };
    internal.sources.set(Track.Source.Camera, state);
    await internal.reconcile(state);
    expect([...live]).toEqual(fails ? ['vp8'] : ['av1']);
    expect([...state.variants.keys()]).toEqual([...live]);
    if (fails) expect(parameters.encodings[0].maxBitrate).toBe(2_500_000);
  }
);

it('restores compatible video for a pending joiner without retiring premium during grace', async () => {
  const publishTrack = vi.fn(async (track: LocalVideoTrack, options: { videoCodec: string }) => {
    track.sid = options.videoCodec;
  });
  const unpublishTrack = vi.fn(async () => {});
  const controller = new AdaptiveVideoController({
    localParticipant: {
      identity: 'alice',
      publishTrack,
      unpublishTrack,
      publishData: vi.fn(async () => {})
    },
    remoteParticipants: new Map([
      ['bob', { identity: 'bob', trackPublications: new Map() }],
      ['new', { identity: 'new', trackPublications: new Map() }]
    ])
  } as unknown as Room);
  const internal = controller as unknown as {
    peers: Map<string, unknown>;
    sources: Map<Track.Source, unknown>;
    connected(participant: { identity: string }): void;
    pending: Promise<unknown>;
  };
  internal.peers.set('bob', { codecs: ['vp8', 'av1'], demand: {} });
  const state = {
    capture: { mediaStreamTrack: { readyState: 'live', clone: () => ({ stop: vi.fn() }) } },
    options: { source: Track.Source.Camera },
    variants: new Map([['av1', { sid: 'av1', sender: testSender() }]]),
    publishedAt: new Map(),
    audience: new Map(),
    hardware: ['av1'],
    modes: { av1: 'L1T1' },
    encode: ['vp8', 'av1'],
    bitrate: 2_500_000,
    fps: 30,
    width: 1280,
    height: 720
  };
  internal.sources.set(Track.Source.Camera, state);
  internal.connected({ identity: 'new' });
  await internal.pending;
  expect([...state.variants.keys()]).toEqual(['av1', 'vp8']);
  expect(unpublishTrack).not.toHaveBeenCalled();
  expect(publishTrack).toHaveBeenCalledOnce();
});

it('shares the source ceiling across codecs, caps compatible pixels and restores the original ladder', () => {
  const controller = new AdaptiveVideoController({} as Room);
  const internal = controller as unknown as {
    sources: Map<Track.Source, unknown>;
    level: number;
    budgetEncodings(
      state: unknown,
      codec: string,
      layers: RTCRtpEncodingParameters[],
      count: number
    ): RTCRtpEncodingParameters[];
  };
  const state = {
    audience: new Map([
      ['vp8', 1],
      ['h265', 1]
    ]),
    options: {},
    bitrate: 8_000_000,
    fps: 60,
    width: 1920,
    height: 1080
  };
  const baseline = [
    { maxBitrate: 150_000, scaleResolutionDownBy: 4 },
    { maxBitrate: 500_000, scaleResolutionDownBy: 2 },
    { maxBitrate: 7_350_000, scaleResolutionDownBy: 1 }
  ];
  internal.sources.set(Track.Source.ScreenShare, state);
  const fallback = internal.budgetEncodings(state, 'vp8', baseline, 2);
  const premium = internal.budgetEncodings(state, 'h265', [{ maxBitrate: 8_000_000 }], 2);
  expect([...fallback, ...premium].reduce((sum, layer) => sum + layer.maxBitrate!, 0)).toBe(
    8_000_000
  );
  expect(fallback.map((layer) => layer.scaleResolutionDownBy)).toEqual([4, 2, 1.5]);
  expect(
    internal.budgetEncodings(state, 'vp8', baseline, 1).map((layer) => layer.maxBitrate)
  ).toEqual(baseline.map((layer) => layer.maxBitrate));
  expect(internal.budgetEncodings(state, 'vp8', baseline, 1)[2].scaleResolutionDownBy).toBe(1);
  internal.sources.set(Track.Source.Camera, {});
  expect(
    internal.budgetEncodings(state, 'h265', [{ maxBitrate: 8_000_000 }], 2)[0].maxBitrate
  ).toBe(2_000_000);
  state.bitrate = 300_000;
  internal.level = 3;
  expect(
    internal
      .budgetEncodings(state, 'vp8', baseline, 2)
      .reduce((sum, layer) => sum + layer.maxBitrate!, 0)
  ).toBeLessThanOrEqual(150_000);
  // Multiple legacy SVC encodings describe inclusive layers, not separate streams.
  state.bitrate = 8_000_000;
  internal.level = 0;
  const svc = internal.budgetEncodings(
    state,
    'av1',
    [{ maxBitrate: 8_000_000 }, { maxBitrate: 2_000_000 }],
    2
  );
  expect(svc.map((layer) => layer.maxBitrate)).toEqual([2_000_000, 500_000]);
  expect(svc.map((layer) => layer.scaleResolutionDownBy)).toEqual([undefined, undefined]);
});

function initialPublicationFixture() {
  const live = new Map<Track.Source, LocalVideoTrack>();
  const unpublishTrack = vi.fn(async (track: LocalVideoTrack) => {
    for (const [source, value] of live) if (value === track) live.delete(source);
  });
  const controller = new AdaptiveVideoController({
    on: vi.fn(),
    off: vi.fn(),
    remoteParticipants: new Map(),
    localParticipant: { identity: 'alice', unpublishTrack, publishData: vi.fn(async () => {}) }
  } as unknown as Room);
  const options = {
    videoEncoding: { maxBitrate: 8_000_000, maxFramerate: 30 },
    screenShareEncoding: { maxBitrate: 8_000_000, maxFramerate: 30 }
  };
  const bitrate = (track: LocalVideoTrack) =>
    track
      .sender!.getParameters()
      .encodings.reduce((total, encoding) => total + encoding.maxBitrate!, 0);
  const publish = (source: Track.Source, fails = false) =>
    controller.publishInitial(source, options, async (configured) => {
      const track = new LocalVideoTrack({
        readyState: 'live',
        getSettings: () => ({ width: 1920, height: 1080, frameRate: 30 }),
        stop: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn()
      } as unknown as MediaStreamTrack);
      Object.assign(track, { sender: undefined, codec: 'vp8' });
      const encodings = [{ maxBitrate: 8_000_000, maxFramerate: 30, scaleResolutionDownBy: 1 }];
      await configured.configureVideoEncodings!(track, encodings);
      expect(
        [...live.values()].reduce((total, value) => total + bitrate(value), 0) +
          encodings[0].maxBitrate
      ).toBeLessThanOrEqual(VIDEO_POLICY.totalBitrate);
      if (fails) throw new Error('Publication failed');
      Object.assign(track, { sender: testSender(encodings) });
      live.set(source, track);
      return {
        source,
        track,
        trackName: `kaede.video.v1:${source}:vp8`
      } as unknown as LocalTrackPublication;
    });
  return { controller, options, live, bitrate, publish, unpublishTrack };
}

it('reserves concurrent initial sources before RTP and retains pristine baselines through adoption', async () => {
  const { controller, options, live, bitrate, publish } = initialPublicationFixture();
  const [camera] = await Promise.all([
    publish(Track.Source.Camera),
    publish(Track.Source.ScreenShare)
  ]);
  expect([...live.values()].map(bitrate)).toEqual([4_000_000, 4_000_000]);
  await controller.managePublished(camera!, options);
  // Reconnection can replace the sender while preserving the published capture.
  const capture = live.get(Track.Source.Camera)!;
  Object.assign(capture, { sender: testSender(capture.sender!.getParameters().encodings) });
  await controller.stopSource(Track.Source.ScreenShare);
  expect(bitrate(live.get(Track.Source.Camera)!)).toBe(8_000_000);
  await controller.stop();
});

it('restores existing source allocation when a configured initial publication fails', async () => {
  const { controller, live, bitrate, publish } = initialPublicationFixture();
  await publish(Track.Source.Camera);
  await expect(publish(Track.Source.ScreenShare, true)).rejects.toThrow('Publication failed');
  expect([...live.keys()]).toEqual([Track.Source.Camera]);
  expect(bitrate(live.get(Track.Source.Camera)!)).toBe(8_000_000);
  await controller.stop();
});

it.each(['source', 'session'] as const)(
  'cancels %s while an initial capture or capability probe is pending',
  async (scope) => {
    const { controller, options, live, publish, unpublishTrack } = initialPublicationFixture();
    if (scope === 'source') {
      const camera = await publish(Track.Source.Camera);
      vi.mocked(webVideoCodecCapabilities).mockImplementationOnce(() => new Promise(() => {}));
      const managing = controller.managePublished(camera!, options);
      await Promise.resolve();
      await Promise.all([managing, controller.stopSource(Track.Source.Camera)]);
      expect(live.size).toBe(0);
      expect(unpublishTrack).toHaveBeenCalled();
      await controller.stop();
    } else {
      let publishStarted!: () => void;
      let configured!: Parameters<Parameters<AdaptiveVideoController['publishInitial']>[2]>[0];
      const started = new Promise<void>((resolve) => {
        publishStarted = resolve;
      });
      const pending = controller.publishInitial(Track.Source.Camera, options, async (value) => {
        configured = value;
        publishStarted();
        return new Promise(() => {});
      });
      await started;
      await Promise.all([pending, controller.stop()]);
      expect(live.size).toBe(0);
      const stop = vi.fn();
      const late = new LocalVideoTrack({ stop } as unknown as MediaStreamTrack);
      await expect(
        configured.configureVideoEncodings!(late, [{ maxBitrate: 8_000_000 }])
      ).rejects.toThrow('Video session stopped');
      expect(stop).toHaveBeenCalledOnce();
    }
  }
);

it('retries unchanged hints before a lost join announcement exhausts the grace period', async () => {
  let now = 100_000;
  const clock = vi.spyOn(Date, 'now').mockImplementation(() => now);
  const publishData = vi.fn(async () => {});
  const controller = new AdaptiveVideoController({
    localParticipant: { publishData }
  } as unknown as Room);
  const internal = controller as unknown as { announce(force?: boolean): Promise<void> };
  try {
    await internal.announce(true);
    now += VIDEO_POLICY.announceMs;
    await internal.announce();
    expect(publishData).toHaveBeenCalledTimes(2);
    expect(VIDEO_POLICY.announceMs).toBeLessThan(VIDEO_POLICY.hintGraceMs);
  } finally {
    clock.mockRestore();
  }
});
