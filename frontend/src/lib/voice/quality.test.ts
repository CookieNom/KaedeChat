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
  isHardwareVideoEncoder
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
      audioPreset: { maxBitrate: audioQuality('studio').maxBitrate },
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
    ).toEqual({ maxBitrate: 32_000 });
    expect(
      webAudioPublishOptions({ ...DEFAULT_MEDIA_QUALITY, audioQuality: 'data_saver' }, 96_000)
        .audioPreset
    ).toEqual({ maxBitrate: 24_000 });
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

  it('checks the actual encoder at the requested dimensions and releases the loopback capture', async () => {
    const stop = vi.fn();
    const captureStream = vi.fn(() => ({
      getTracks: () => [{ stop }],
      getVideoTracks: () => [{}]
    }));
    vi.stubGlobal('document', {
      createElement: () => ({ getContext: () => ({ fillRect: vi.fn() }), captureStream })
    });
    vi.stubGlobal('RTCRtpSender', {
      getCapabilities: () => ({ codecs: [{ mimeType: 'video/AV1' }] })
    });
    const encodingInfo = vi.fn(async () => ({
      supported: true,
      smooth: true,
      powerEfficient: true
    }));
    vi.stubGlobal('navigator', { mediaCapabilities: { encodingInfo } });
    const close = vi.fn();
    const setCodecPreferences = vi.fn();
    vi.stubGlobal(
      'RTCPeerConnection',
      class {
        connectionState = 'connected';
        localDescription = {};
        close() {
          this.connectionState = 'closed';
          close();
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
                new Map([
                  [
                    'out',
                    {
                      type: 'outbound-rtp',
                      framesEncoded: 3,
                      frameWidth: 1920,
                      frameHeight: 1080,
                      powerEfficientEncoder: true,
                      encoderImplementation: 'ExternalEncoder'
                    }
                  ]
                ])
            }
          };
        }
      }
    );
    const result = await webVideoCodecCapabilities({
      width: 1920,
      height: 1080,
      frameRate: 30,
      bitrate: 4500000
    });
    expect(result.hardware).toEqual(['av1']);
    expect(encodingInfo).toHaveBeenCalledWith({
      type: 'webrtc',
      video: {
        contentType: 'video/av1',
        width: 1920,
        height: 1080,
        framerate: 30,
        bitrate: 4500000,
        scalabilityMode: 'L1T1'
      }
    });
    expect(setCodecPreferences).toHaveBeenCalledWith([{ mimeType: 'video/AV1' }]);
    expect(captureStream).toHaveBeenCalledWith(30);
    expect(close).toHaveBeenCalledTimes(2);
    expect(stop).toHaveBeenCalledOnce();
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
