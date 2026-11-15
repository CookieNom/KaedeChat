import { describe, expect, it, vi } from 'vitest';
import { createVideoPublisher } from '@livekit-sdk/room/video/publisher';
import { computeVideoEncodings } from '@livekit-sdk/room/participant/publishUtils';
import { Track } from '@livekit-sdk/room/track/Track';
import { computeVideoEncodings as publicComputeVideoEncodings } from 'livekit-client';
import LocalVideoTrack from '@livekit-sdk/room/track/LocalVideoTrack';
import * as sdkUtils from '@livekit-sdk/room/utils';
import { Mutex } from '@livekit/mutex';

vi.mock('@livekit-sdk/room/utils', async (original) => ({
  ...(await original()),
  supportsAV1: () => true,
  supportsVP9: () => true
}));

describe('probed codec and scalability mode survive publication', () => {
  it.each(['av1', 'vp9', 'h265'])(
    'preserves %s screen-share modes and bitrate',
    async (videoCodec) => {
      const room = {
        localParticipant: { enabledPublishVideoCodecs: [] },
        options: {},
        engine: { serverVersion: '1.13.3' }
      };
      const logger = { debug() {}, info() {}, warn() {}, error() {} };
      const publisher = createVideoPublisher(room, logger);
      const track = {
        source: Track.Source.ScreenShare,
        mediaStreamTrack: { id: 'capture', contentHint: 'detail' },
        waitForDimensions: async () => ({ width: 1280, height: 720 })
      };
      const modes = videoCodec === 'h265' ? ['L1T1', 'L1T3'] : ['L1T1', 'L1T3', 'L3T3_KEY'];
      for (const scalabilityMode of modes) {
        const options = {
          videoCodec,
          scalabilityMode,
          backupCodec: false,
          simulcast: false,
          screenShareEncoding: { maxBitrate: 2500000, maxFramerate: 30 }
        };
        const request = {};
        const encodings = await publisher.prepare(track, options, request);
        expect(options.scalabilityMode).toBe(scalabilityMode);
        expect(encodings).toHaveLength(1);
        expect(encodings[0].scalabilityMode).toBe(scalabilityMode);
        expect(encodings[0].maxBitrate).toBe(2500000);
        expect(track.mediaStreamTrack.contentHint).toBe('detail');
        expect(request.simulcastCodecs.map((codec) => codec.codec)).toEqual([videoCodec]);
      }
    }
  );

  it('rejects HEVC spatial layers or extra encodings instead of silently changing the mode', () => {
    for (const options of [
      { scalabilityMode: 'L3T3_KEY', simulcast: false },
      { scalabilityMode: 'L1T3', simulcast: true }
    ]) {
      expect(() =>
        computeVideoEncodings(false, 1280, 720, { videoCodec: 'h265', ...options })
      ).toThrow('unsupported HEVC scalabilityMode');
    }
  });

  it('uses caller-capped encodings for initial RTP and announced layers without changing the baseline', async () => {
    const room = {
      localParticipant: { enabledPublishVideoCodecs: [] },
      options: {},
      engine: { serverVersion: '1.13.3' }
    };
    const publisher = createVideoPublisher(room, { debug() {}, info() {}, warn() {}, error() {} });
    const track = {
      source: Track.Source.Camera,
      mediaStreamTrack: { id: 'capture' },
      waitForDimensions: async () => ({ width: 1280, height: 720 })
    };
    const options = {
      videoCodec: 'vp8',
      simulcast: true,
      backupCodec: false,
      videoEncoding: { maxBitrate: 2_500_000, maxFramerate: 30 }
    };
    const baseline = publicComputeVideoEncodings(false, 1280, 720, options);
    expect(baseline).toEqual(computeVideoEncodings(false, 1280, 720, options));
    const capped = baseline.map((encoding) => ({
      ...encoding,
      maxBitrate: Math.floor(encoding.maxBitrate / 2)
    }));
    const request = {};
    const actual = await publisher.prepare(
      track,
      { ...options, videoSendEncodings: capped },
      request
    );
    expect(actual).toEqual(capped);
    expect(actual[0]).not.toBe(capped[0]);
    expect(request.layers.reduce((total, layer) => total + layer.bitrate, 0)).toBe(
      capped.reduce((total, encoding) => total + encoding.maxBitrate, 0)
    );
    expect(publicComputeVideoEncodings(false, 1280, 720, options)).toEqual(baseline);
    expect(
      await publisher.serverCodecChanged(
        track,
        { ...options, videoSendEncodings: capped },
        { codecs: [{ mimeType: 'video/h264' }] },
        request
      )
    ).toEqual(capped);
  });

  it('awaits the initial configuration hook and retains its limits after a server codec change', async () => {
    const publisher = createVideoPublisher(
      {
        localParticipant: { enabledPublishVideoCodecs: [] },
        options: {},
        engine: { serverVersion: '1.13.3' }
      },
      { debug() {}, info() {}, warn() {}, error() {} }
    );
    const track = {
      source: Track.Source.Camera,
      mediaStreamTrack: { id: 'capture' },
      waitForDimensions: async () => ({ width: 1280, height: 720 })
    };
    const configured = [];
    const options = {
      videoCodec: 'vp8',
      simulcast: true,
      backupCodec: false,
      videoEncoding: { maxBitrate: 2_500_000, maxFramerate: 30 },
      configureVideoEncodings: async (_track, encodings) => {
        await Promise.resolve();
        configured.push(encodings.map((encoding) => ({ ...encoding })));
        encodings.forEach((encoding) => {
          encoding.maxBitrate = Math.floor(encoding.maxBitrate / 2);
        });
      }
    };
    const request = {};
    const initial = await publisher.prepare(track, options, request);
    expect(options.videoSendEncodings).toEqual(initial);
    expect(request.layers.map((layer) => layer.bitrate)).toEqual(
      initial.map((encoding) => encoding.maxBitrate)
    );
    const changed = await publisher.serverCodecChanged(
      track,
      options,
      { codecs: [{ mimeType: 'video/h264' }] },
      request
    );
    expect(configured).toHaveLength(2);
    expect(changed.map((encoding) => encoding.maxBitrate)).toEqual(
      configured[1].map((encoding) => Math.floor(encoding.maxBitrate / 2))
    );
    expect(options.videoSendEncodings).toEqual(changed);
  });

  it('retains runtime limits across Firefox dynacast and capture restarts, including failed updates', async () => {
    let parameters = {
      encodings: [
        { rid: 'q', active: true, maxBitrate: 100_000, scaleResolutionDownBy: 4 },
        { rid: 'f', active: true, maxBitrate: 1_000_000, scaleResolutionDownBy: 1 }
      ]
    };
    const sender = {
      getParameters: () => structuredClone(parameters),
      setParameters: vi.fn(async (next) => {
        parameters = structuredClone(next);
      })
    };
    const track = {
      sender,
      senderLock: new Mutex(),
      encodings: structuredClone(parameters.encodings),
      publishOptions: {
        videoCodec: 'vp8',
        simulcast: true,
        videoEncoding: { maxBitrate: 2_500_000, maxFramerate: 30 }
      },
      source: Track.Source.Camera,
      simulcastCodecs: new Map(),
      log: { debug() {}, warn() {} },
      logContext: {},
      waitForDimensions: async () => ({ width: 1920, height: 1080 }),
      applyEncodingsToSender: LocalVideoTrack.prototype.applyEncodingsToSender
    };
    const capped = parameters.encodings.map((encoding) => ({
      ...encoding,
      maxBitrate: encoding.maxBitrate / 2
    }));
    await LocalVideoTrack.prototype.applyVideoSendEncodings.call(
      track,
      capped,
      'maintain-resolution'
    );
    expect(parameters.degradationPreference).toBe('maintain-resolution');
    const firefox = vi.spyOn(sdkUtils, 'isFireFox').mockReturnValue(true);
    try {
      const qualities = (enabled) => [0, 2].map((quality) => ({ quality, enabled }));
      await LocalVideoTrack.prototype.setPublishingLayers.call(track, false, qualities(false));
      expect(parameters.encodings.map((encoding) => encoding.maxBitrate)).toEqual([10, 10]);
      await LocalVideoTrack.prototype.setPublishingLayers.call(track, false, qualities(true));
      expect(parameters.encodings.map((encoding) => encoding.maxBitrate)).toEqual([
        50_000, 500_000
      ]);
      await LocalVideoTrack.prototype.refreshSenderEncodings.call(track);
      expect(parameters.encodings.map((encoding) => encoding.maxBitrate)).toEqual([
        50_000, 500_000
      ]);
      sender.setParameters.mockRejectedValueOnce(new Error('encoder busy'));
      await expect(
        LocalVideoTrack.prototype.applyVideoSendEncodings.call(
          track,
          capped.map((encoding) => ({ ...encoding, maxBitrate: 25_000 }))
        )
      ).rejects.toThrow('encoder busy');
      expect(track.publishOptions.videoSendEncodings).toEqual(capped);
    } finally {
      firefox.mockRestore();
    }
  });
});
