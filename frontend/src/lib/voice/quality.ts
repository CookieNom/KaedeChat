import {
  VideoPresets,
  type ScreenShareCaptureOptions,
  type TrackPublishOptions,
  type VideoCaptureOptions
} from 'livekit-client';

export type ScreenShareProfileId = 'data_saver' | 'smooth' | 'sharp' | 'source';
export type AudioQualityId = 'data_saver' | 'standard' | 'high' | 'studio';

export interface ScreenShareProfile {
  id: ScreenShareProfileId;
  label: string;
  description: string;
  width: number | null;
  height: number | null;
  frameRate: number;
  maxBitrate: number;
  contentHint: 'detail' | 'motion';
}

export interface AudioQuality {
  id: AudioQualityId;
  label: string;
  description: string;
  maxBitrate: number;
  stereo: boolean;
}

export interface MediaQualityPreferences {
  screenProfile: ScreenShareProfileId;
  audioQuality: AudioQualityId;
  shareAudio: boolean;
  dtx: boolean;
}

export const SCREEN_SHARE_PROFILES: readonly ScreenShareProfile[] = [
  {
    id: 'data_saver',
    label: 'Data saver',
    description: 'Readable video on slower connections',
    width: 1280,
    height: 720,
    frameRate: 15,
    maxBitrate: 1_200_000,
    contentHint: 'detail'
  },
  {
    id: 'smooth',
    label: 'Smooth',
    description: 'Responsive motion for games and demos',
    width: 1280,
    height: 720,
    frameRate: 30,
    maxBitrate: 2_500_000,
    contentHint: 'motion'
  },
  {
    id: 'sharp',
    label: 'Sharp',
    description: 'Clear text and detailed interfaces',
    width: 1920,
    height: 1080,
    frameRate: 30,
    maxBitrate: 4_500_000,
    contentHint: 'detail'
  },
  {
    id: 'source',
    label: 'Source',
    description: 'Preserve the source resolution',
    width: null,
    height: null,
    frameRate: 30,
    maxBitrate: 8_000_000,
    contentHint: 'detail'
  }
] as const;

export const AUDIO_QUALITIES: readonly AudioQuality[] = [
  {
    id: 'data_saver',
    label: 'Data saver',
    description: '24 kbps · speech',
    maxBitrate: 24_000,
    stereo: false
  },
  {
    id: 'standard',
    label: 'Standard',
    description: '48 kbps · balanced',
    maxBitrate: 48_000,
    stereo: false
  },
  {
    id: 'high',
    label: 'High',
    description: '96 kbps · detailed voice',
    maxBitrate: 96_000,
    stereo: false
  },
  {
    id: 'studio',
    label: 'Studio',
    description: '128 kbps · stereo music',
    maxBitrate: 128_000,
    stereo: true
  }
] as const;

export const DEFAULT_MEDIA_QUALITY: MediaQualityPreferences = {
  screenProfile: 'smooth',
  audioQuality: 'standard',
  shareAudio: true,
  dtx: true
};

const STORAGE_KEY = 'kaede.media-quality.v1';

export function screenShareProfile(id: ScreenShareProfileId): ScreenShareProfile {
  return SCREEN_SHARE_PROFILES.find((profile) => profile.id === id) ?? SCREEN_SHARE_PROFILES[1];
}

export function audioQuality(id: AudioQualityId): AudioQuality {
  return AUDIO_QUALITIES.find((quality) => quality.id === id) ?? AUDIO_QUALITIES[1];
}

export function loadMediaQuality(storage?: Pick<Storage, 'getItem'>): MediaQualityPreferences {
  try {
    const target = storage ?? globalThis.localStorage;
    if (!target) return { ...DEFAULT_MEDIA_QUALITY };
    const parsed: unknown = JSON.parse(target.getItem(STORAGE_KEY) ?? 'null');
    if (!parsed || typeof parsed !== 'object') return { ...DEFAULT_MEDIA_QUALITY };
    const candidate = parsed as Partial<MediaQualityPreferences>;
    return {
      screenProfile: SCREEN_SHARE_PROFILES.some(({ id }) => id === candidate.screenProfile)
        ? (candidate.screenProfile as ScreenShareProfileId)
        : DEFAULT_MEDIA_QUALITY.screenProfile,
      audioQuality: AUDIO_QUALITIES.some(({ id }) => id === candidate.audioQuality)
        ? (candidate.audioQuality as AudioQualityId)
        : DEFAULT_MEDIA_QUALITY.audioQuality,
      shareAudio:
        typeof candidate.shareAudio === 'boolean'
          ? candidate.shareAudio
          : DEFAULT_MEDIA_QUALITY.shareAudio,
      dtx: typeof candidate.dtx === 'boolean' ? candidate.dtx : DEFAULT_MEDIA_QUALITY.dtx
    };
  } catch {
    return { ...DEFAULT_MEDIA_QUALITY };
  }
}

export function saveMediaQuality(
  preferences: MediaQualityPreferences,
  storage?: Pick<Storage, 'setItem'>
): void {
  try {
    const target = storage ?? globalThis.localStorage;
    target?.setItem(STORAGE_KEY, JSON.stringify(preferences));
  } catch {
    // Private browsing and hardened WebViews can reject storage writes. The
    // active session still uses the selected values.
  }
}

export function webScreenShareOptions(
  preferences: MediaQualityPreferences,
  preferredSurface?: 'window' | 'browser' | 'monitor'
): {
  capture: ScreenShareCaptureOptions;
  publish: TrackPublishOptions;
} {
  const profile = screenShareProfile(preferences.screenProfile);
  const resolution =
    profile.width && profile.height
      ? { width: profile.width, height: profile.height, frameRate: profile.frameRate }
      : { width: 7680, height: 4320, frameRate: profile.frameRate };
  return {
    capture: {
      audio: preferences.shareAudio,
      video: preferredSurface ? { displaySurface: preferredSurface } : true,
      resolution,
      contentHint: profile.contentHint,
      systemAudio: preferences.shareAudio ? 'include' : 'exclude',
      surfaceSwitching: 'include',
      selfBrowserSurface: 'exclude'
    },
    publish: {
      screenShareEncoding: {
        maxBitrate: profile.maxBitrate,
        maxFramerate: profile.frameRate
      },
      degradationPreference:
        profile.contentHint === 'motion' ? 'maintain-framerate' : 'maintain-resolution',
      simulcast: true
    }
  };
}

export function webAudioPublishOptions(
  preferences: MediaQualityPreferences,
  channelBitrate = Number.POSITIVE_INFINITY
): TrackPublishOptions {
  const quality = audioQuality(preferences.audioQuality);
  return {
    audioPreset: { maxBitrate: Math.min(quality.maxBitrate, channelBitrate) },
    forceStereo: quality.stereo,
    dtx: preferences.dtx,
    red: true
  };
}

/**
 * Discord's automatic channel mode favors an adaptive 360p working set;
 * full mode raises the camera's primary capture/encoding target to 720p.
 * Screen-share capture and encoding remain governed by the independent
 * screen-share profile above.
 */
export function webCameraDefaults(videoQualityMode: 1 | 2): {
  capture: VideoCaptureOptions;
  publish: TrackPublishOptions;
} {
  const preset = videoQualityMode === 2 ? VideoPresets.h720 : VideoPresets.h360;
  return {
    capture: { resolution: preset.resolution },
    publish: {
      videoEncoding: preset.encoding,
      simulcast: true
    }
  };
}
