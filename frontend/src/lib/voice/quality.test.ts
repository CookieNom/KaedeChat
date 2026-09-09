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
  webVideoCodecOptions
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
    saveMediaQuality(preferences, storage);
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
  afterEach(() => vi.unstubAllGlobals());

  it('prefers AV1 with H264 backup, falls back on limited senders, and preserves E2EE', () => {
    const getCapabilities = vi.fn(() => ({
      codecs: [{ mimeType: 'video/AV1' }, { mimeType: 'video/H264' }]
    }));
    vi.stubGlobal('RTCRtpSender', { getCapabilities });
    expect(webVideoCodecOptions(false)).toEqual({
      videoCodec: 'av1',
      backupCodec: { codec: 'h264' }
    });
    expect(webVideoCodecOptions(true)).toEqual({ videoCodec: 'vp8', backupCodec: false });
    getCapabilities.mockReturnValue({ codecs: [{ mimeType: 'video/AV1' }] });
    expect(webVideoCodecOptions(false)).toEqual({
      videoCodec: 'av1',
      backupCodec: { codec: 'vp8' }
    });
    getCapabilities.mockReturnValue({ codecs: [{ mimeType: 'video/H264' }] });
    expect(webVideoCodecOptions(false)).toEqual({ videoCodec: 'h264', backupCodec: false });
    vi.stubGlobal('RTCRtpSender', undefined);
    expect(webVideoCodecOptions(false)).toEqual({ videoCodec: 'vp8', backupCodec: false });
  });
});
