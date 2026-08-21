import { describe, expect, it } from 'vitest';

import {
  DEFAULT_MEDIA_QUALITY,
  loadMediaQuality,
  saveMediaQuality,
  screenShareProfile,
  webAudioPublishOptions,
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
      shareAudio: false
    };
    saveMediaQuality(preferences, storage);
    expect(loadMediaQuality(storage)).toEqual(preferences);
  });

  it('maps capture and encoder settings independently', () => {
    const preferences = {
      screenProfile: 'smooth' as const,
      audioQuality: 'studio' as const,
      shareAudio: true
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
