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
    expect(parseVideoMessage(encode({ ...value, codecs: ['unknown'] }))).toBeUndefined();
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
import { AdaptiveVideoController } from './adaptive-video';
import { webVideoCodecCapabilities } from './quality';

vi.mock('./quality', async (original) => ({
  ...(await original<typeof import('./quality')>()),
  webVideoCodecCapabilities: vi.fn(async () => ({ decode: ['vp8'], encode: ['vp8'], hardware: [] }))
}));
vi.mock('livekit-client', async (original) => ({
  ...(await original<typeof import('livekit-client')>()),
  LocalVideoTrack: class {
    sid?: string;
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
  for (let i = 0; i < 10; i++) await tick();
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
