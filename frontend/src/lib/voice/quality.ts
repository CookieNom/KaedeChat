import {
  VideoPresets,
  type ScreenShareCaptureOptions,
  type TrackPublishOptions,
  type ScalabilityMode,
  type VideoCaptureOptions
} from 'livekit-client';
import { CODECS, VIDEO_CODECS, type Codec } from './video-codecs';

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
  /** Current share only; process IDs are never restored from preferences. */
  audioProcess?: number;
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
    const { screenProfile, audioQuality, shareAudio, dtx } = preferences;
    target?.setItem(STORAGE_KEY, JSON.stringify({ screenProfile, audioQuality, shareAudio, dtx }));
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
    audioPreset: {
      maxBitrate: Math.min(quality.maxBitrate, channelBitrate),
      // Request voice DSCP marking where the browser and network support it.
      priority: 'high'
    },
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

/** Safe initial publication until the asynchronous, settings-specific probe finishes. */
export function webVideoCodecOptions(encrypted: boolean): TrackPublishOptions {
  // Encryption belongs to the room and never selects a different codec policy.
  void encrypted;
  return { videoCodec: 'vp8', backupCodec: false };
}

export interface VideoCapabilitySettings {
  width: number;
  height: number;
  frameRate: number;
  bitrate: number;
  scalabilityMode?: ScalabilityMode;
}

/** Power efficiency alone may also describe software; require an actual hardware implementation. */
export function isHardwareVideoEncoder(stats: {
  powerEfficientEncoder?: boolean;
  encoderImplementation?: string;
}): boolean {
  return (
    stats.powerEfficientEncoder === true &&
    /(?:ExternalEncoder|VideoToolbox|MediaCodec|NVENC|NVIDIA|VAAPI|VA-API|QuickSync|QSV|AMF|D3D11|MediaFoundation)/i.test(
      stats.encoderImplementation ?? ''
    ) &&
    !/(?:libaom|libvpx|openh264|software|fallback)/i.test(stats.encoderImplementation ?? '')
  );
}

/*
 * Probe results persist per browser build and capture settings, so repeated
 * shares start on the right codec without another loopback encode. Negative
 * results expire sooner because drivers and power state change. Storage
 * failures (private mode, blocked site data) only disable persistence.
 */
const PROBE_CACHE_KEY = 'kaede.video.hardware.v3';
const PROBE_POSITIVE_TTL = 7 * 24 * 60 * 60 * 1000;
// Encoder-session contention, power state and permission changes are transient.
const PROBE_NEGATIVE_TTL = 60_000;
type ProbeEntry = { hardware: boolean; at: number; proof?: VideoCodecProbe };
type ProbeCache = { ua: string; entries: Record<string, ProbeEntry> };
const sessionProofs = new Map<string, ProbeEntry>();
const pendingProofs = new Map<string, Promise<VideoCodecProbe>>();
let probeQueue: Promise<void> = Promise.resolve();
let cacheGeneration = 0;

function probeKey(codec: string, settings: VideoCapabilitySettings): string {
  return [
    codec,
    settings.scalabilityMode ?? 'L1T1',
    Math.round(settings.width),
    Math.round(settings.height),
    Math.round(settings.frameRate),
    Math.round(settings.bitrate / 100_000)
  ].join('/');
}

function readProbeCache(): ProbeCache {
  const ua = globalThis.navigator?.userAgent ?? '';
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(PROBE_CACHE_KEY) ?? 'null');
    if (value && value.ua === ua && value.entries && typeof value.entries === 'object')
      return value as ProbeCache;
  } catch {
    /* Unavailable or malformed storage behaves as an empty cache. */
  }
  return { ua, entries: {} };
}

function cachedProbe(codec: string, settings: VideoCapabilitySettings): boolean | undefined {
  const entry = readProbeCache().entries[probeKey(codec, settings)];
  if (!entry || typeof entry.hardware !== 'boolean' || typeof entry.at !== 'number') return;
  const ttl = entry.hardware ? PROBE_POSITIVE_TTL : PROBE_NEGATIVE_TTL;
  const age = Date.now() - entry.at;
  return age >= 0 && age < ttl ? entry.hardware : undefined;
}

function sessionProbeKey(codec: Codec, settings: VideoCapabilitySettings): string {
  return `${globalThis.navigator?.userAgent ?? ''}/${probeKey(codec, settings)}`;
}

function forgetProbe(codec: Codec, settings: VideoCapabilitySettings): void {
  sessionProofs.delete(sessionProbeKey(codec, settings));
  try {
    const cache = readProbeCache();
    delete cache.entries[probeKey(codec, settings)];
    globalThis.localStorage?.setItem(PROBE_CACHE_KEY, JSON.stringify(cache));
  } catch {
    /* Missing storage cannot retain a stale in-memory proof. */
  }
}

function cachedCodecProof(
  codec: Codec,
  settings: VideoCapabilitySettings
): VideoCodecProbe | undefined {
  const entry =
    sessionProofs.get(sessionProbeKey(codec, settings)) ??
    readProbeCache().entries[probeKey(codec, settings)];
  const proof = entry?.proof;
  if (!proof || proof.codec !== codec || proof.mode !== (settings.scalabilityMode ?? 'L1T1'))
    return;
  const ttl =
    proof.encoded && (proof.hardwareEncode || codec === 'vp9')
      ? PROBE_POSITIVE_TTL
      : PROBE_NEGATIVE_TTL;
  const age = Date.now() - entry.at;
  if (age >= 0 && age < ttl) return { ...proof };
}

/** Shared exact-settings proof for controllers. All loopbacks use one encoder at a time. */
export function cachedVideoCodecProbe(
  codec: Codec,
  settings: VideoCapabilitySettings,
  options: { fresh?: boolean } = {}
): Promise<VideoCodecProbe> {
  const key = sessionProbeKey(codec, settings);
  if (options.fresh) forgetProbe(codec, settings);
  const pending = pendingProofs.get(key);
  if (pending) return pending;
  const cached = options.fresh ? undefined : cachedCodecProof(codec, settings);
  if (cached) return Promise.resolve(cached);
  const generation = cacheGeneration;
  const task = probeVideoCodec(codec, settings).then((proof) => {
    // Unknown, aborted and privacy-redacted results do not poison future probes.
    if (
      generation === cacheGeneration &&
      (proof.encoded || proof.decoded) &&
      (proof.encoder || proof.decoder)
    ) {
      const entry = { hardware: proof.hardwareEncode, at: Date.now(), proof: { ...proof } };
      sessionProofs.set(key, entry);
      try {
        const cache = readProbeCache();
        cache.entries[probeKey(codec, settings)] = entry;
        globalThis.localStorage?.setItem(PROBE_CACHE_KEY, JSON.stringify(cache));
      } catch {
        /* The in-memory result remains useful when storage is unavailable. */
      }
    }
    return proof;
  });
  pendingProofs.set(key, task);
  // Both handlers settle successfully: an ignored finally() would create a rejected promise.
  void task.then(
    () => pendingProofs.delete(key),
    () => pendingProofs.delete(key)
  );
  return task;
}

/** Records probe or live-call evidence, e.g. a "hardware" AV1 encoder that fell back to software. */
export function recordVideoHardwareResult(
  codec: string,
  settings: VideoCapabilitySettings,
  hardware: boolean
): void {
  sessionProofs.delete(sessionProbeKey(codec as Codec, settings));
  try {
    const cache = readProbeCache();
    cache.entries[probeKey(codec, settings)] = { hardware, at: Date.now() };
    globalThis.localStorage?.setItem(PROBE_CACHE_KEY, JSON.stringify(cache));
  } catch {
    /* Storage is a convenience only. */
  }
}

export function clearVideoHardwareCache(): void {
  cacheGeneration++;
  sessionProofs.clear();
  try {
    globalThis.localStorage?.removeItem(PROBE_CACHE_KEY);
  } catch {
    /* Storage is a convenience only. */
  }
}

export interface VideoCodecProbe {
  codec: Codec;
  mode: ScalabilityMode;
  advertisedEncode: boolean;
  advertisedDecode: boolean;
  encoded: boolean;
  decoded: boolean;
  hardwareEncode: boolean;
  hardwareDecode: boolean;
  encoder: string;
  decoder: string;
  actualMode?: string;
  detail: string;
}

export function isHardwareVideoDecoder(stats: {
  powerEfficientDecoder?: boolean;
  decoderImplementation?: string;
}): boolean {
  return isHardwareVideoEncoder({
    powerEfficientEncoder: stats.powerEfficientDecoder,
    encoderImplementation: stats.decoderImplementation?.replace(
      'ExternalDecoder',
      'ExternalEncoder'
    )
  });
}

export async function webVideoCodecCapabilities(
  settings: VideoCapabilitySettings,
  options: { fresh?: boolean; codecs?: string[]; scalable?: boolean } = {}
): Promise<{
  decode: string[];
  encode: string[];
  hardware: string[];
  modes?: Partial<Record<Codec, ScalabilityMode>>;
}> {
  const supported = (codecs: RTCRtpCodec[]) =>
    [...new Set(codecs.map((codec) => codec.mimeType.toLowerCase().replace('video/', '')))].filter(
      (codec) => CODECS.includes(codec as Codec)
    );
  const encode = supported(globalThis.RTCRtpSender?.getCapabilities?.('video')?.codecs ?? []);
  const decode = supported(globalThis.RTCRtpReceiver?.getCapabilities?.('video')?.codecs ?? [])
    // HEVC decoding is admitted separately after a hardware loopback proof.
    .filter((codec) => codec !== 'h265');
  const hardware: string[] = [];
  const modes: Partial<Record<Codec, ScalabilityMode>> = {};
  const result = () => ({ decode, encode, hardware, ...(options.scalable ? { modes } : {}) });
  if (
    ![settings.width, settings.height, settings.frameRate, settings.bitrate].every(
      (value) => Number.isFinite(value) && value > 0
    )
  )
    return result();
  for (const name of encode) {
    const codec = name as Codec;
    if (options.codecs && !options.codecs.includes(codec)) continue;
    const candidates =
      options.scalable && ['av1', 'vp9', 'h265'].includes(codec)
        ? VIDEO_CODECS[codec].modes
        : [settings.scalabilityMode ?? 'L1T1'];
    for (const mode of candidates) {
      const exact = { ...settings, scalabilityMode: mode };
      if (options.fresh) forgetProbe(codec, exact);
      const previousProof = options.fresh ? undefined : cachedCodecProof(codec, exact);
      const cached = options.fresh ? undefined : cachedProbe(codec, exact);
      if (codec === 'h265' && previousProof?.hardwareDecode && !decode.includes(codec))
        decode.push(codec);
      if (previousProof?.encoded && (previousProof.hardwareEncode || codec === 'vp9')) {
        if (previousProof.hardwareEncode) hardware.push(codec);
        modes[codec] = mode;
        break;
      }
      if (cached !== undefined && (codec !== 'vp9' || previousProof)) {
        if (cached) {
          hardware.push(codec);
          modes[codec] = mode;
          break;
        }
        continue;
      }
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const info = await Promise.race([
          globalThis.navigator?.mediaCapabilities?.encodingInfo({
            type: 'webrtc',
            video: {
              contentType: `video/${codec}`,
              width: settings.width,
              height: settings.height,
              bitrate: settings.bitrate,
              framerate: settings.frameRate,
              scalabilityMode: mode
            }
          }),
          new Promise<undefined>((resolve) => {
            timer = setTimeout(() => resolve(undefined), 800);
          })
        ]);
        // Power-efficient capability is only a prefilter; actual RTP proves hardware and mode.
        if (!info) continue;
        if (!info.supported || !info.smooth || (!info.powerEfficient && codec !== 'vp9')) {
          continue;
        }
        const proof = await cachedVideoCodecProbe(codec, exact, { fresh: options.fresh });
        if (codec === 'h265' && proof.decoded) {
          if (proof.hardwareDecode && !decode.includes(codec)) decode.push(codec);
        }
        if (!proof.encoded) continue; // Failure/timeout remains unknown.
        if (proof.hardwareEncode) hardware.push(codec);
        if (proof.hardwareEncode || codec === 'vp9') {
          modes[codec] = mode;
          break;
        }
      } catch {
        // Missing or privacy-redacted information never establishes hardware support.
      } finally {
        clearTimeout(timer);
      }
    }
  }
  return result();
}

/** Fresh local RTP loopback; queued with cached controller probes to avoid hardware contention. */
export function probeVideoCodec(
  codec: Codec,
  settings: VideoCapabilitySettings,
  signal?: AbortSignal
): Promise<VideoCodecProbe> {
  const task = probeQueue.then(() => runVideoCodecProbe(codec, settings, signal));
  probeQueue = task.then(
    () => {},
    () => {}
  );
  return task;
}

/** Synthetic pixels only; a decode-only device cannot establish hardware proof through loopback. */
async function runVideoCodecProbe(
  codec: Codec,
  settings: VideoCapabilitySettings,
  signal?: AbortSignal
): Promise<VideoCodecProbe> {
  const mode = settings.scalabilityMode ?? 'L1T1';
  const senderCodecs = globalThis.RTCRtpSender?.getCapabilities?.('video')?.codecs ?? [];
  const receiverCodecs = globalThis.RTCRtpReceiver?.getCapabilities?.('video')?.codecs ?? [];
  const matches = (value: RTCRtpCodec) => value.mimeType.toLowerCase() === `video/${codec}`;
  const result: VideoCodecProbe = {
    codec,
    mode,
    advertisedEncode: senderCodecs.some(matches),
    advertisedDecode: receiverCodecs.some(matches),
    encoded: false,
    decoded: false,
    hardwareEncode: false,
    hardwareDecode: false,
    encoder: '',
    decoder: '',
    detail: ''
  };
  if (!result.advertisedEncode || !result.advertisedDecode) {
    result.detail = 'A local loopback needs both an advertised encoder and decoder.';
    return result;
  }
  if (signal?.aborted) {
    result.detail = 'Stopped';
    return result;
  }
  if (typeof document === 'undefined' || typeof RTCPeerConnection === 'undefined') return result;
  const canvas = document.createElement('canvas');
  canvas.width = settings.width;
  canvas.height = settings.height;
  const context = canvas.getContext('2d');
  if (!context || !canvas.captureStream) {
    result.detail = 'Canvas capture unavailable';
    return result;
  }
  const peers: RTCPeerConnection[] = [];
  let stream: MediaStream | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let draw: ReturnType<typeof setInterval> | undefined;
  let frame = 0;
  let done = false;
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  const abort = () => {
    done = true;
    peers.forEach((peer) => peer.close());
  };
  try {
    stream = canvas.captureStream(settings.frameRate);
    const sender = new RTCPeerConnection({ iceServers: [] });
    peers.push(sender);
    const receiver = new RTCPeerConnection({ iceServers: [] });
    peers.push(receiver);
    draw = setInterval(() => {
      context.fillStyle = `rgb(${frame++ % 255},64,128)`;
      context.fillRect(0, 0, canvas.width, canvas.height);
    }, 1000 / settings.frameRate);
    signal?.addEventListener('abort', abort, { once: true });
    receiver.ontrack = (event) => {
      video.srcObject = event.streams[0] ?? new MediaStream([event.track]);
      void video.play().catch(() => {});
    };
    const transceiver = sender.addTransceiver(stream.getVideoTracks()[0], {
      direction: 'sendonly',
      streams: [stream],
      sendEncodings: [
        {
          maxBitrate: settings.bitrate,
          maxFramerate: settings.frameRate,
          ...{ scalabilityMode: mode }
        }
      ]
    });
    transceiver.setCodecPreferences(senderCodecs.filter(matches));
    // Candidates can arrive before the other description; apply after negotiation.
    const toReceiver: RTCIceCandidate[] = [],
      toSender: RTCIceCandidate[] = [];
    sender.onicecandidate = (event) => {
      if (!event.candidate) return;
      if (receiver.remoteDescription)
        void receiver.addIceCandidate(event.candidate).catch(() => {});
      else toReceiver.push(event.candidate);
    };
    receiver.onicecandidate = (event) => {
      if (!event.candidate) return;
      if (sender.remoteDescription) void sender.addIceCandidate(event.candidate).catch(() => {});
      else toSender.push(event.candidate);
    };
    const probe = async () => {
      await sender.setLocalDescription(await sender.createOffer());
      await receiver.setRemoteDescription(sender.localDescription!);
      await receiver.setLocalDescription(await receiver.createAnswer());
      await sender.setRemoteDescription(receiver.localDescription!);
      await Promise.all(toReceiver.map((candidate) => receiver.addIceCandidate(candidate)));
      await Promise.all(toSender.map((candidate) => sender.addIceCandidate(candidate)));
      while (!done && sender.connectionState !== 'closed') {
        const outbound = await transceiver.sender.getStats();
        if (done) return;
        outbound.forEach((report) => {
          if (report.type !== 'outbound-rtp') return;
          result.encoder = report.encoderImplementation ?? '';
          result.actualMode = report.scalabilityMode;
          const actualCodec = outbound.get(report.codecId)?.mimeType?.toLowerCase();
          result.encoded =
            report.framesEncoded >= 3 &&
            report.frameWidth === settings.width &&
            report.frameHeight === settings.height &&
            actualCodec === `video/${codec}` &&
            (report.scalabilityMode === mode ||
              (report.scalabilityMode === undefined &&
                mode === 'L1T1' &&
                (codec === 'vp8' || codec === 'h264')));
          result.hardwareEncode = result.encoded && isHardwareVideoEncoder(report);
        });
        const inbound = await receiver.getStats();
        if (done) return;
        inbound.forEach((report) => {
          if (report.type !== 'inbound-rtp') return;
          result.decoder = report.decoderImplementation ?? '';
          result.decoded =
            report.framesDecoded >= 3 &&
            report.frameWidth === settings.width &&
            report.frameHeight === settings.height &&
            inbound.get(report.codecId)?.mimeType?.toLowerCase() === `video/${codec}`;
          result.hardwareDecode = result.decoded && isHardwareVideoDecoder(report);
        });
        if (result.encoded && result.decoded) return;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    };
    await Promise.race([
      probe(),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, 2400);
      })
    ]);
    result.detail = signal?.aborted
      ? 'Stopped'
      : result.encoded && result.decoded
        ? 'Verified with local video frames'
        : result.actualMode && result.actualMode !== mode
          ? `Browser used ${result.actualMode} instead of ${mode}`
          : 'Not verified before timeout; support may be unavailable or hidden';
  } catch (error) {
    result.detail = error instanceof Error ? error.message : 'Probe failed';
  } finally {
    done = true;
    clearTimeout(timer);
    clearInterval(draw);
    signal?.removeEventListener('abort', abort);
    peers.forEach((peer) => peer.close());
    video.pause();
    video.srcObject = null;
    stream?.getTracks().forEach((track) => track.stop());
  }
  return result;
}
