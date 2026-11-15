import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_MEDIA_QUALITY,
  audioQuality,
  loadMediaQuality,
  saveMediaQuality,
  screenShareProfile,
  webAudioPublishOptions,
  webCameraDefaults,
  webScreenShareOptions,
  webVideoCodecOptions,
  webVideoCodecCapabilities,
  isHardwareVideoEncoder,
  clearVideoHardwareCache,
  recordVideoHardwareResult,
  probeVideoCodec,
  cachedVideoCodecProbe
} from './quality';

class MemoryStorage {
  value: string | null = null;
  getItem() {
    return this.value;
  }
  setItem(_key: string, value: string) {
    this.value = value;
  }
}

describe('media quality preferences', () => {
  it('rejects malformed or unknown persisted values', () => {
    const storage = new MemoryStorage();
    storage.value = '{not json';
    expect(loadMediaQuality(storage)).toEqual(DEFAULT_MEDIA_QUALITY);
    storage.value = JSON.stringify({
      screenProfile: 'unbounded',
      audioQuality: 999,
      shareAudio: 'yes'
    });
    expect(loadMediaQuality(storage)).toEqual(DEFAULT_MEDIA_QUALITY);
    const valid = { screenProfile: 'source', audioQuality: 'studio', shareAudio: true, dtx: false };
    for (const [field, invalid] of Object.entries({
      screenProfile: 'unbounded',
      audioQuality: 999,
      shareAudio: 'yes',
      dtx: 'yes'
    })) {
      storage.value = JSON.stringify({ ...valid, [field]: invalid });
      expect(loadMediaQuality(storage)).toEqual({
        ...valid,
        [field]: DEFAULT_MEDIA_QUALITY[field as keyof typeof DEFAULT_MEDIA_QUALITY]
      });
    }
  });
  it('round trips valid preferences', () => {
    const storage = new MemoryStorage();
    const preferences = {
      screenProfile: 'sharp' as const,
      audioQuality: 'high' as const,
      shareAudio: false,
      dtx: false
    };
    saveMediaQuality({ ...preferences, audioProcess: 1234 }, storage);
    expect(storage.value).not.toContain('audioProcess');
    expect(loadMediaQuality(storage)).toEqual(preferences);
    storage.value = JSON.stringify({ ...preferences, audioProcess: 1234 });
    expect(loadMediaQuality(storage)).toEqual(preferences);
  });

  it('enables DTX when loading preferences saved before the setting existed', () => {
    const storage = new MemoryStorage();
    storage.value = JSON.stringify({
      screenProfile: 'smooth',
      audioQuality: 'studio',
      shareAudio: true
    });

    expect(loadMediaQuality(storage).dtx).toBe(true);
  });

  it('maps capture and encoder settings independently', () => {
    const preferences = {
      screenProfile: 'smooth' as const,
      audioQuality: 'studio' as const,
      shareAudio: true,
      dtx: false
    };
    const screen = webScreenShareOptions(preferences);
    const profile = screenShareProfile('smooth');
    expect(screen.capture.resolution).toEqual({
      width: profile.width,
      height: profile.height,
      frameRate: profile.frameRate
    });
    expect(screen.publish.screenShareEncoding).toEqual({
      maxBitrate: profile.maxBitrate,
      maxFramerate: profile.frameRate
    });
    expect(webAudioPublishOptions(preferences)).toMatchObject({
      audioPreset: { maxBitrate: audioQuality('studio').maxBitrate, priority: 'high' },
      forceStereo: true,
      dtx: false
    });
  });

  it('enables DTX by default for mono and stereo audio', () => {
    expect(webAudioPublishOptions(DEFAULT_MEDIA_QUALITY).dtx).toBe(true);
    expect(webAudioPublishOptions({ ...DEFAULT_MEDIA_QUALITY, audioQuality: 'studio' }).dtx).toBe(
      true
    );
    expect(webAudioPublishOptions({ ...DEFAULT_MEDIA_QUALITY, dtx: false }).dtx).toBe(false);
  });

  it('caps microphone audio to the effective channel bitrate', () => {
    expect(
      webAudioPublishOptions({ ...DEFAULT_MEDIA_QUALITY, audioQuality: 'studio' }, 32_000)
        .audioPreset
    ).toEqual({ maxBitrate: 32_000, priority: 'high' });
    expect(
      webAudioPublishOptions({ ...DEFAULT_MEDIA_QUALITY, audioQuality: 'data_saver' }, 96_000)
        .audioPreset
    ).toEqual({ maxBitrate: 24_000, priority: 'high' });
  });

  it('keeps adaptive and full camera defaults separate from screen-share profiles', () => {
    const automatic = webCameraDefaults(1);
    const full = webCameraDefaults(2);

    expect(automatic.capture.resolution!.width).toBeGreaterThan(0);
    expect(automatic.capture.resolution!.height).toBeGreaterThan(0);
    expect(automatic.capture.resolution!.width).toBeLessThan(full.capture.resolution!.width);
    expect(automatic.capture.resolution!.height).toBeLessThan(full.capture.resolution!.height);
    expect(automatic.publish.videoEncoding?.maxBitrate).toBeLessThan(
      full.publish.videoEncoding?.maxBitrate ?? 0
    );
    const screen = screenShareProfile(DEFAULT_MEDIA_QUALITY.screenProfile);
    expect(webScreenShareOptions(DEFAULT_MEDIA_QUALITY).capture.resolution).toEqual({
      width: screen.width,
      height: screen.height,
      frameRate: screen.frameRate
    });
  });

  it('keeps source capture bounded to a defensive maximum', () => {
    const options = webScreenShareOptions({
      ...DEFAULT_MEDIA_QUALITY,
      screenProfile: 'source'
    });
    expect(options.capture.resolution).toEqual({
      width: 7680,
      height: 4320,
      frameRate: screenShareProfile('source').frameRate
    });
    expect(options.publish.screenShareEncoding?.maxBitrate).toBe(
      screenShareProfile('source').maxBitrate
    );
  });

  it('passes the selected source category to the protected browser picker', () => {
    expect(webScreenShareOptions(DEFAULT_MEDIA_QUALITY, 'window').capture.video).toEqual({
      displaySurface: 'window'
    });
    expect(webScreenShareOptions(DEFAULT_MEDIA_QUALITY, 'browser').capture.video).toEqual({
      displaySurface: 'browser'
    });
    expect(webScreenShareOptions(DEFAULT_MEDIA_QUALITY, 'monitor').capture.video).toEqual({
      displaySurface: 'monitor'
    });
  });
});

describe('video codec defaults', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    clearVideoHardwareCache();
  });

  it('keeps both call modes on a single safe codec until hardware is proven', () => {
    vi.stubGlobal('RTCRtpSender', {
      getCapabilities: () => ({
        codecs: [{ mimeType: 'video/AV1' }, { mimeType: 'video/H264' }]
      })
    });
    for (const encrypted of [false, true]) {
      expect(webVideoCodecOptions(encrypted)).toEqual({ videoCodec: 'vp8', backupCodec: false });
    }
  });

  it('admits software AV1 decoding but does not infer hardware encoding from codec support', async () => {
    const getCapabilities = () => ({
      codecs: [{ mimeType: 'video/AV1' }, { mimeType: 'video/VP8' }]
    });
    vi.stubGlobal('RTCRtpSender', { getCapabilities });
    vi.stubGlobal('RTCRtpReceiver', { getCapabilities });
    vi.stubGlobal('navigator', {
      mediaCapabilities: { encodingInfo: vi.fn().mockRejectedValue(new Error('unsupported')) }
    });
    expect(
      await webVideoCodecCapabilities({
        width: 3840,
        height: 2160,
        frameRate: 60,
        bitrate: 8000000
      })
    ).toEqual({ decode: ['av1', 'vp8'], encode: ['av1', 'vp8'], hardware: [] });
  });

  it('reuses cached probe verdicts and bypasses them for a fresh mid-call check', async () => {
    clearVideoHardwareCache();
    vi.stubGlobal('RTCRtpSender', {
      getCapabilities: () => ({ codecs: [{ mimeType: 'video/AV1' }, { mimeType: 'video/VP8' }] })
    });
    const encodingInfo = vi.fn().mockRejectedValue(new Error('unsupported'));
    vi.stubGlobal('navigator', { userAgent: 'test', mediaCapabilities: { encodingInfo } });
    const items = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => items.get(key) ?? null,
      setItem: (key: string, value: string) => items.set(key, value),
      removeItem: (key: string) => items.delete(key)
    });
    const settings = { width: 1920, height: 1080, frameRate: 30, bitrate: 4_500_000 };
    recordVideoHardwareResult('av1', settings, true);
    recordVideoHardwareResult('vp8', settings, false);
    expect((await webVideoCodecCapabilities(settings)).hardware).toEqual(['av1']);
    expect(encodingInfo).not.toHaveBeenCalled();
    expect(
      (await webVideoCodecCapabilities(settings, { fresh: true, codecs: ['av1'] })).hardware
    ).toEqual([]);
    expect(encodingInfo).toHaveBeenCalledOnce();
    // A failed fresh check invalidates the old positive result too.
    await webVideoCodecCapabilities(settings, { codecs: ['av1'] });
    expect(encodingInfo).toHaveBeenCalledTimes(2);
    expect(
      (
        await webVideoCodecCapabilities(
          { ...settings, scalabilityMode: 'L1T3' },
          { codecs: ['av1'] }
        )
      ).hardware
    ).toEqual([]);
    expect(encodingInfo).toHaveBeenCalledTimes(3);
    vi.useFakeTimers();
    recordVideoHardwareResult('av1', settings, false);
    await webVideoCodecCapabilities(settings, { codecs: ['av1'] });
    expect(encodingInfo).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(60_001);
    await webVideoCodecCapabilities(settings, { codecs: ['av1'] });
    expect(encodingInfo).toHaveBeenCalledTimes(4);
    clearVideoHardwareCache();
  });

  it('does not block capture cleanup on an unresponsive capability query', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('RTCRtpSender', {
      getCapabilities: () => ({ codecs: [{ mimeType: 'video/AV1' }] })
    });
    vi.stubGlobal('navigator', {
      mediaCapabilities: { encodingInfo: () => new Promise(() => {}) }
    });
    const result = webVideoCodecCapabilities({
      width: 1920,
      height: 1080,
      frameRate: 30,
      bitrate: 4500000
    });
    await vi.advanceTimersByTimeAsync(801);
    expect((await result).hardware).toEqual([]);
  });

  it.each([
    ['av1', 'av1', 'L1T1', true],
    ['av1', 'av1', 'L1T3', false],
    ['av1', 'h264', 'L1T1', false],
    ['vp8', 'vp8', undefined, true],
    ['h264', 'h264', undefined, true],
    ['av1', 'av1', undefined, false],
    ['h265', 'h265', undefined, false],
    ['vp9', 'vp9', 'L1T1', true],
    ['h265', 'h265', 'L1T1', true]
  ] as const)(
    'verifies requested codec=%s, actual codec=%s and mode=%s, then releases the loopback capture',
    async (requestedCodec, actualCodec, actualMode, proven) => {
      vi.useFakeTimers();
      const stop = vi.fn();
      const captureStream = vi.fn(() => ({
        getTracks: () => [{ stop }],
        getVideoTracks: () => [{}]
      }));
      vi.stubGlobal('document', {
        createElement: () => ({
          getContext: () => ({ fillRect: vi.fn() }),
          captureStream,
          pause: vi.fn()
        })
      });
      vi.stubGlobal('RTCRtpSender', {
        getCapabilities: () => ({ codecs: [{ mimeType: `video/${requestedCodec}` }] })
      });
      vi.stubGlobal('RTCRtpReceiver', {
        getCapabilities: () => ({ codecs: [{ mimeType: `video/${requestedCodec}` }] })
      });
      const encodingInfo = vi.fn(async () => ({
        supported: true,
        smooth: true,
        powerEfficient: true
      }));
      vi.stubGlobal('navigator', { mediaCapabilities: { encodingInfo } });
      const close = vi.fn();
      const setCodecPreferences = vi.fn();
      let activePeers = 0,
        peakPeers = 0;
      vi.stubGlobal(
        'RTCPeerConnection',
        class {
          connectionState = 'connected';
          localDescription = {};
          constructor() {
            peakPeers = Math.max(peakPeers, ++activePeers);
          }
          close() {
            this.connectionState = 'closed';
            activePeers--;
            close();
          }
          async getStats() {
            return new Map<string, unknown>([
              ['codec', { mimeType: `video/${requestedCodec}` }],
              [
                'in',
                {
                  type: 'inbound-rtp',
                  codecId: 'codec',
                  framesDecoded: 3,
                  frameWidth: 1920,
                  frameHeight: 1080,
                  decoderImplementation: 'ExternalDecoder',
                  powerEfficientDecoder: true
                }
              ]
            ]);
          }
          async createOffer() {
            return {};
          }
          async createAnswer() {
            return {};
          }
          async setLocalDescription() {}
          async setRemoteDescription() {}
          addTransceiver() {
            return {
              setCodecPreferences,
              sender: {
                getStats: async () =>
                  new Map<string, unknown>([
                    ['codec', { mimeType: `video/${actualCodec}` }],
                    [
                      'out',
                      {
                        type: 'outbound-rtp',
                        codecId: 'codec',
                        scalabilityMode: actualMode,
                        framesEncoded: 3,
                        frameWidth: 1920,
                        frameHeight: 1080,
                        powerEfficientEncoder: requestedCodec !== 'vp9',
                        encoderImplementation:
                          requestedCodec === 'vp9' ? 'libvpx' : 'ExternalEncoder'
                      }
                    ]
                  ])
              }
            };
          }
        }
      );
      const settings = {
        width: 1920,
        height: 1080,
        frameRate: 30,
        bitrate: 4500000
      };
      const pending = webVideoCodecCapabilities(settings);
      await vi.advanceTimersByTimeAsync(2401);
      const result = await pending;
      expect(result.hardware).toEqual(proven && requestedCodec !== 'vp9' ? [requestedCodec] : []);
      expect(encodingInfo).toHaveBeenCalledWith({
        type: 'webrtc',
        video: {
          contentType: `video/${requestedCodec}`,
          width: 1920,
          height: 1080,
          framerate: 30,
          bitrate: 4500000,
          scalabilityMode: 'L1T1'
        }
      });
      expect(setCodecPreferences).toHaveBeenCalledWith([{ mimeType: `video/${requestedCodec}` }]);
      expect(captureStream).toHaveBeenCalledWith(30);
      expect(close).toHaveBeenCalledTimes(2);
      expect(stop).toHaveBeenCalledOnce();
      if ((requestedCodec === 'vp8' || requestedCodec === 'h264') && actualMode === undefined) {
        const scalable = probeVideoCodec(requestedCodec, { ...settings, scalabilityMode: 'L1T3' });
        await vi.advanceTimersByTimeAsync(2401);
        expect((await scalable).encoded).toBe(false);
      }
      if (requestedCodec === 'vp9') {
        const before = captureStream.mock.calls.length;
        const first = webVideoCodecCapabilities(settings, { scalable: true });
        await vi.advanceTimersByTimeAsync(5000);
        expect((await first).modes?.vp9).toBe('L1T1');
        const after = captureStream.mock.calls.length;
        expect(after).toBe(before + 2); // Higher modes were not actually produced.
        expect((await webVideoCodecCapabilities(settings, { scalable: true })).modes?.vp9).toBe(
          'L1T1'
        );
        expect(captureStream).toHaveBeenCalledTimes(after);
      }
      if (requestedCodec === 'h265' && proven) {
        expect((await cachedVideoCodecProbe('h265', settings)).hardwareDecode).toBe(true);
        expect(captureStream).toHaveBeenCalledOnce();
        const other = { ...settings, bitrate: 6_000_000 };
        const shared = cachedVideoCodecProbe('h265', other);
        expect(cachedVideoCodecProbe('h265', other)).toBe(shared);
        const fresh = probeVideoCodec('h265', settings);
        await vi.advanceTimersByTimeAsync(1);
        await Promise.all([shared, fresh]);
        expect(captureStream).toHaveBeenCalledTimes(3);
        expect(peakPeers).toBe(2);
        expect(activePeers).toBe(0);
      }
    }
  );

  it('releases capture and the first peer if the second peer cannot be created', async () => {
    const stop = vi.fn(),
      close = vi.fn();
    const codecs = { getCapabilities: () => ({ codecs: [{ mimeType: 'video/AV1' }] }) };
    vi.stubGlobal('RTCRtpSender', codecs);
    vi.stubGlobal('RTCRtpReceiver', codecs);
    vi.stubGlobal('document', {
      createElement: () => ({
        getContext: () => ({}),
        captureStream: () => ({ getTracks: () => [{ stop }] }),
        pause: vi.fn()
      })
    });
    let created = 0;
    vi.stubGlobal(
      'RTCPeerConnection',
      class {
        constructor() {
          if (++created === 2) throw new Error('Peer limit reached');
        }
        close = close;
      }
    );
    const result = await probeVideoCodec('av1', {
      width: 1280,
      height: 720,
      frameRate: 30,
      bitrate: 2500000
    });
    expect(result.hardwareEncode).toBe(false);
    expect(result.detail).toBe('Peer limit reached');
    expect(stop).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });

  it('cleans up rejected shared requests and continues the global probe queue', async () => {
    const codecs = { getCapabilities: () => ({ codecs: [{ mimeType: 'video/AV1' }] }) };
    vi.stubGlobal('RTCRtpSender', codecs);
    vi.stubGlobal('RTCRtpReceiver', codecs);
    vi.stubGlobal('RTCPeerConnection', class {});
    const createElement = vi.fn(() => {
      throw new Error('Canvas unavailable');
    });
    vi.stubGlobal('document', { createElement });
    const settings = { width: 1280, height: 720, frameRate: 30, bitrate: 2_500_000 };
    const failed = cachedVideoCodecProbe('av1', settings);
    expect(cachedVideoCodecProbe('av1', settings)).toBe(failed);
    await expect(failed).rejects.toThrow('Canvas unavailable');
    await expect(cachedVideoCodecProbe('av1', settings)).rejects.toThrow('Canvas unavailable');
    expect(createElement).toHaveBeenCalledTimes(2);
  });

  it('requires actual hardware evidence and rejects software and unknown implementations', () => {
    expect(isHardwareVideoEncoder({ powerEfficientEncoder: true })).toBe(false);
    expect(
      isHardwareVideoEncoder({ powerEfficientEncoder: true, encoderImplementation: 'libaom' })
    ).toBe(false);
    expect(
      isHardwareVideoEncoder({
        powerEfficientEncoder: false,
        encoderImplementation: 'ExternalEncoder'
      })
    ).toBe(false);
    expect(
      isHardwareVideoEncoder({
        powerEfficientEncoder: true,
        encoderImplementation: 'ExternalEncoder'
      })
    ).toBe(true);
    expect(
      isHardwareVideoEncoder({
        powerEfficientEncoder: true,
        encoderImplementation: 'NVIDIA AV1 Encoder'
      })
    ).toBe(true);
    expect(
      isHardwareVideoEncoder({
        powerEfficientEncoder: true,
        encoderImplementation: 'ExternalEncoder (fallback software)'
      })
    ).toBe(false);
  });
});
