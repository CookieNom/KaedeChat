import { describe, expect, it } from 'vitest';

import {
  DEFAULT_MEDIA_QUALITY,
  loadMediaQuality,
  saveMediaQuality,
  screenShareProfile,
  webAudioPublishOptions,
  webCameraDefaults,
  webScreenShareOptions
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
    expect(screen.capture.resolution).toEqual({ width: 1280, height: 720, frameRate: 30 });
    expect(screen.publish.screenShareEncoding).toEqual({
      maxBitrate: 2_500_000,
      maxFramerate: 30
    });
    expect(webAudioPublishOptions(preferences)).toMatchObject({
      audioPreset: { maxBitrate: 128_000 },
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

    expect(automatic.capture.resolution).toMatchObject({ width: 640, height: 360 });
    expect(full.capture.resolution).toMatchObject({ width: 1280, height: 720 });
    expect(automatic.publish.videoEncoding?.maxBitrate).toBeLessThan(
      full.publish.videoEncoding?.maxBitrate ?? 0
    );
    expect(webScreenShareOptions(DEFAULT_MEDIA_QUALITY).capture.resolution).toEqual({
      width: 1280,
      height: 720,
      frameRate: 30
    });
  });

  it('keeps source capture bounded to a defensive maximum', () => {
    const options = webScreenShareOptions({
      ...DEFAULT_MEDIA_QUALITY,
      screenProfile: 'source'
    });
    expect(options.capture.resolution).toEqual({ width: 7680, height: 4320, frameRate: 30 });
    expect(screenShareProfile('source').maxBitrate).toBe(8_000_000);
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
