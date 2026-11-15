import {
  LocalVideoTrack,
  computeVideoEncodings,
  RoomEvent,
  Track,
  VideoQuality,
  type LocalTrackPublication,
  type RemoteParticipant,
  type Room,
  type TrackPublication,
  type TrackPublishOptions,
  type ScalabilityMode,
  type VideoCodec
} from 'livekit-client';
import {
  isHardwareVideoEncoder,
  isHardwareVideoDecoder,
  cachedVideoCodecProbe,
  recordVideoHardwareResult,
  webVideoCodecCapabilities
} from './quality';

export const VIDEO_TOPIC = 'kaede.video.v1';
import {
  CODECS,
  LEGACY_CODECS,
  EXTENDED_CODECS,
  PREMIUM_CODECS,
  isPremiumCodec,
  type Codec
} from './video-codecs';
type Health = 'healthy' | 'decode' | 'download';

/**
 * Shared adaptation policy. The mobile (adaptive_video.dart) and native desktop
 * (kaede-voice) controllers use the same values; change all three together.
 */
export const VIDEO_POLICY = {
  sampleMs: 2000,
  /** Consecutive bad samples before degrading (6s). */
  degradeSamples: 3,
  /** Consecutive healthy samples before recovering one step (30s). */
  recoverSamples: 15,
  /** Minimum time between two adaptation steps of the same controller. */
  minChangeMs: 20_000,
  /** Bandwidth limitation is expected while the congestion controller ramps up. */
  uploadWarmupMs: 10_000,
  /** A viewer without codec hints is "pending" this long before it counts as VP8-only. */
  hintGraceMs: 8_000,
  /** Re-announce unchanged hints at this interval; changes are sent immediately. */
  announceMs: 4_000,
  /** Retry delays after an premium attempt failed; afterwards the session stops retrying. */
  backoffMs: [60_000, 300_000, 900_000],
  bitrateStep: 0.72,
  motionFpsStep: 0.85,
  detailFpsStep: 0.65,
  scaleStep: 1.25,
  motionFpsFloor: 12,
  detailFpsFloor: 5,
  /** Combined per-source bitrate ceiling across all published sources. */
  totalBitrate: 8_000_000,
  /** Dual-cast only when at least this share of viewers would watch the premium codec. */
  dualCastShare: 1 / 3,
  /** Healthy premium samples (5 min) after which earlier playback failures are forgotten. */
  premiumTrustSamples: 150
} as const;

export interface VideoMessage {
  v: 1;
  codecs: string[];
  extendedCodecs?: string[];
  demand: Record<string, string>;
  health: Record<string, Health>;
}
export function parseVideoMessage(bytes: Uint8Array): VideoMessage | undefined {
  if (bytes.byteLength > 8192) return;
  try {
    const value = JSON.parse(new TextDecoder().decode(bytes));
    if (
      value.v !== 1 ||
      !Array.isArray(value.codecs) ||
      value.codecs.length > 3 ||
      !value.codecs.every((c: unknown) => LEGACY_CODECS.includes(c as Codec)) ||
      !value.demand ||
      typeof value.demand !== 'object' ||
      Array.isArray(value.demand) ||
      !value.health ||
      typeof value.health !== 'object' ||
      Array.isArray(value.health)
    )
      return;
    if (
      value.extendedCodecs !== undefined &&
      (!Array.isArray(value.extendedCodecs) ||
        value.extendedCodecs.length > 8 ||
        !value.extendedCodecs.every(
          (codec: unknown) => typeof codec === 'string' && codec.length <= 16
        ))
    )
      return;
    if (
      Object.entries(value.demand).some(
        ([k, v]) => k.length > 512 || !LEGACY_CODECS.includes(v as Codec)
      ) ||
      Object.values(value.health).some(
        (v) => !['healthy', 'decode', 'download'].includes(v as string)
      )
    )
      return;
    if (value.extendedCodecs)
      value.extendedCodecs = value.extendedCodecs.filter((codec: Codec) =>
        EXTENDED_CODECS.includes(codec)
      );
    return value;
  } catch {
    return;
  }
}
export function managedCodec(
  publication: Pick<TrackPublication, 'trackName'> & Partial<Pick<TrackPublication, 'source'>>
): Codec | undefined {
  const parts = publication.trackName.split(':');
  return parts.length === 3 &&
    parts[0] === VIDEO_TOPIC &&
    ['camera', 'screen_share'].includes(parts[1]) &&
    (!publication.source || parts[1] === publication.source) &&
    CODECS.includes(parts[2] as Codec)
    ? (parts[2] as Codec)
    : undefined;
}

/** Name for the initial SDK publication so the controller can adopt it as the VP8 fallback. */
export function managedTrackName(source: Track.Source, codec: Codec = 'vp8'): string {
  return `${VIDEO_TOPIC}:${source === Track.Source.ScreenShare ? 'screen_share' : 'camera'}:${codec}`;
}

/** Escalating retry delays for an attempt that failed (e.g. premium encode or playback). */
export class VideoBackoff {
  private entries = new Map<string, { failures: number; until: number }>();
  blocked(key: string, now: number): boolean {
    return (this.entries.get(key)?.until ?? 0) > now;
  }
  fail(key: string, now: number): void {
    const entry = this.entries.get(key) ?? { failures: 0, until: 0 };
    entry.until = now + (VIDEO_POLICY.backoffMs[entry.failures] ?? Number.POSITIVE_INFINITY);
    entry.failures++;
    this.entries.set(key, entry);
  }
  clear(prefix: string): void {
    for (const key of this.entries.keys()) if (key.startsWith(prefix)) this.entries.delete(key);
  }
}

// Counter deltas avoid mistaking a past overload for an ongoing problem.
export interface VideoSample {
  timestamp: number;
  frames?: number;
  receivedFrames?: number;
  processing?: number;
  dropped?: number;
  lost?: number;
  packets?: number;
  bytes?: number;
  limitation?: string;
}
export function videoBottleneck(
  previous: VideoSample | undefined,
  next: VideoSample,
  receiver: boolean
): Health | 'encode' | 'upload' | 'unknown' {
  if (!previous || next.timestamp <= previous.timestamp) return 'unknown';
  const frames = (next.frames ?? 0) - (previous.frames ?? 0);
  const seconds = (next.timestamp - previous.timestamp) / 1000;
  const processing = (next.processing ?? 0) - (previous.processing ?? 0);
  const lost = Math.max(0, (next.lost ?? 0) - (previous.lost ?? 0));
  const packets = (next.packets ?? 0) - (previous.packets ?? 0);
  if (!receiver && next.limitation === 'cpu') return 'encode';
  if (!receiver && next.limitation === 'bandwidth') return 'upload';
  if (receiver && packets > 0 && lost / (packets + lost) > 0.05) return 'download';
  if (
    receiver &&
    next.frames !== undefined &&
    previous.frames !== undefined &&
    next.receivedFrames !== undefined &&
    previous.receivedFrames !== undefined &&
    frames === 0 &&
    (next.receivedFrames ?? 0) > (previous.receivedFrames ?? 0)
  )
    return 'decode';
  if (frames > 0 && processing > seconds * 0.8) return receiver ? 'decode' : 'encode';
  const dropped = (next.dropped ?? 0) - (previous.dropped ?? 0);
  if (receiver && frames > 0 && dropped > frames * 0.15 && lost === 0) return 'decode';
  return frames > 0 ? 'healthy' : 'unknown';
}

type LayerBaseline = Pick<
  RTCRtpEncodingParameters,
  'maxBitrate' | 'maxFramerate' | 'scaleResolutionDownBy' | 'priority'
>;
type SourceState = {
  capture: LocalVideoTrack;
  options: TrackPublishOptions;
  variants: Map<Codec, LocalVideoTrack>;
  publishedAt: Map<Codec, number>;
  /** Viewers expected to watch each variant; steers the shared bandwidth allocator. */
  audience: Map<Codec, number>;
  hardware: string[];
  /** Set when a premium encoder fell back to software; re-probe once its backoff expires. */
  reprobe: Set<Codec>;
  modes: Partial<Record<Codec, ScalabilityMode>>;
  encode: string[];
  bitrate: number;
  fps: number;
  width: number;
  height: number;
  ended: () => void;
};
type Viewer = {
  identity: string;
  codecs: string[];
  demand: Record<string, string>;
  pending: boolean;
};
type DecodeInfo = { smooth: boolean; powerEfficient: boolean };

const UHD_PIXELS = 1920 * 1088;

/** All publications use the existing Room: its E2EE manager installs transforms and rotates keys. */
export class AdaptiveVideoController {
  private sources = new Map<Track.Source, SourceState>();
  private initialSources = new Map<Track.Source, SourceState>();
  private trackBaselines = new WeakMap<LocalVideoTrack, LayerBaseline[]>();
  private stopSignal = new AbortController();
  private sourceSignals = new Map<Track.Source, AbortController>();
  private peers = new Map<string, VideoMessage>();
  private seen = new Map<string, number>();
  private previous = new Map<string, VideoSample>();
  private receiverEvidence = new Map<
    string,
    { bad: number; good: number; changed: number; lowerLayer?: boolean }
  >();
  /** Receiver demands derived from local decode capability; recovery never clears them. */
  private capabilityDemand = new Set<string>();
  private message: VideoMessage = { v: 1, codecs: ['vp8'], demand: {}, health: {} };
  private selected = new Set<string>();
  private timer?: ReturnType<typeof setInterval>;
  private pending: Promise<unknown> = Promise.resolve();
  private stopped = false;
  private videoVisible = true;
  private visibilityChanged = () => {
    this.videoVisible = document.visibilityState !== 'hidden';
    this.selectSubscriptions();
  };
  /** Encode (CPU) pressure only. Upload pressure is handled structurally, see `uploadConstrained`. */
  private level = 0;
  private bad = 0;
  private good = 0;
  private changed = 0;
  private uploadBad = 0;
  private uploadGood = 0;
  private uploadConstrained = false;
  private availableUpload?: number;
  private busy = false;
  private degraded = false;
  private encodeLoad = 0;
  private encodesMeasured = false;
  private lastSpareCpuAt = 0;
  private backoff = new VideoBackoff();
  private decode = new Map<Codec, { fhd?: DecodeInfo; uhd?: DecodeInfo }>();
  private hevcDecodeReprobe = false;
  private lastAnnounced = '';
  private lastAnnounceAt = 0;
  private baselines = new WeakMap<RTCRtpSender, LayerBaseline[]>();
  private appliedBudgets = new WeakMap<RTCRtpSender, string>();
  private event = () => {
    this.selectSubscriptions();
    void this.announce(true);
  };
  private connected = (participant: RemoteParticipant) => {
    this.seen.set(participant.identity, Date.now());
    this.event();
    void this.enqueue(async () => {
      if (this.stopped) return;
      for (const state of this.sources.values()) await this.reconcile(state);
    }).catch(() => {});
  };
  private disconnected = (participant: RemoteParticipant) => {
    this.peers.delete(participant.identity);
    this.seen.delete(participant.identity);
    const prefix = `${participant.identity}/`;
    this.backoff.clear(prefix);
    for (const key of this.receiverEvidence.keys())
      if (key.startsWith(prefix)) this.receiverEvidence.delete(key);
    for (const key of this.capabilityDemand)
      if (key.startsWith(prefix)) this.capabilityDemand.delete(key);
    for (const key of Object.keys(this.message.demand))
      if (key.startsWith(prefix)) delete this.message.demand[key];
    for (const key of Object.keys(this.message.health))
      if (key.startsWith(prefix)) delete this.message.health[key];
    for (const publication of participant.trackPublications.values()) {
      for (const key of this.previous.keys())
        if (key.startsWith(`${publication.trackSid}/`)) this.previous.delete(key);
    }
    this.event();
  };
  private data = (
    bytes: Uint8Array,
    participant?: RemoteParticipant,
    _kind?: unknown,
    topic?: string
  ) => {
    if (topic !== VIDEO_TOPIC || !participant) return;
    const message = parseVideoMessage(bytes);
    if (!message) return;
    this.peers.set(participant.identity, message);
    this.selectSubscriptions();
  };
  constructor(
    private room: Room,
    private notice: (degraded: boolean) => void = () => {},
    private onSourceEnded: (source: Track.Source) => void | Promise<void> = () => {}
  ) {}

  private reportNotice(): void {
    const degraded =
      this.level > 0 ||
      this.uploadConstrained ||
      Object.values(this.message.health).some((health) => health !== 'healthy');
    if (degraded !== this.degraded) {
      this.degraded = degraded;
      this.notice(degraded);
    }
  }

  start(): void {
    if (this.timer) return;
    this.room.on(RoomEvent.DataReceived, this.data);
    this.room.on(RoomEvent.TrackPublished, this.event);
    this.room.on(RoomEvent.TrackUnpublished, this.event);
    this.room.on(RoomEvent.TrackSubscribed, this.event);
    this.room.on(RoomEvent.ParticipantConnected, this.connected);
    this.room.on(RoomEvent.ParticipantDisconnected, this.disconnected);
    this.room.on(RoomEvent.Reconnected, this.event);
    const capabilities = globalThis.RTCRtpReceiver?.getCapabilities?.('video')?.codecs ?? [];
    this.message.codecs = LEGACY_CODECS.filter((codec) =>
      capabilities.some((c) => c.mimeType.toLowerCase() === `video/${codec}`)
    );
    if (!this.message.codecs.length) this.message.codecs = ['vp8'];
    this.message.extendedCodecs = capabilities.some((c) => c.mimeType.toLowerCase() === 'video/vp9')
      ? ['vp9']
      : [];
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.visibilityChanged);
      this.videoVisible = document.visibilityState !== 'hidden';
    }
    void this.probePremiumDecode().catch(() => {});
    this.event();
    this.timer = setInterval(() => {
      void this.tick();
    }, VIDEO_POLICY.sampleMs);
  }

  /** AV1 may decode in software; HEVC requires observed hardware decoding. */
  private async probePremiumDecode(codecs: readonly Codec[] = PREMIUM_CODECS): Promise<void> {
    for (const codec of codecs) {
      const capabilities = globalThis.RTCRtpReceiver?.getCapabilities?.('video')?.codecs ?? [];
      if (!capabilities.some((c) => c.mimeType.toLowerCase() === `video/${codec}`)) continue;
      const query = async (
        width: number,
        height: number,
        bitrate: number
      ): Promise<DecodeInfo | undefined> => {
        if (codec === 'h265') {
          const proof = await cachedVideoCodecProbe(
            codec,
            { width, height, bitrate, frameRate: 30 },
            { fresh: this.hevcDecodeReprobe }
          );
          return { smooth: proof.hardwareDecode, powerEfficient: proof.hardwareDecode };
        }
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          const info = await Promise.race([
            globalThis.navigator?.mediaCapabilities?.decodingInfo({
              type: 'webrtc',
              video: { contentType: `video/${codec}`, width, height, bitrate, framerate: 30 }
            } as MediaDecodingConfiguration),
            new Promise<undefined>((resolve) => {
              timer = setTimeout(() => resolve(undefined), 800);
            })
          ]);
          return info
            ? { smooth: info.supported && info.smooth, powerEfficient: info.powerEfficient }
            : undefined;
        } catch {
          return undefined;
        } finally {
          clearTimeout(timer);
        }
      };
      // Serialize hardware loopbacks; concurrent encoders would distort the proof.
      const fhd = await query(1920, 1080, 4_500_000);
      if (this.stopped) return;
      const uhd = await query(3840, 2160, 12_000_000);
      if (this.stopped) return;
      this.decode.set(codec, { fhd, uhd });
      if (codec === 'h265' && fhd?.powerEfficient) this.message.extendedCodecs!.push(codec);
      if (codec === 'av1' && fhd && !fhd.smooth && this.message.codecs.length > 1)
        this.message.codecs = this.message.codecs.filter((value) => value !== codec);
      this.event();
    }
  }

  private premiumPlayable(codec: Codec, dimensions: { width: number; height: number }): boolean {
    const uhd = dimensions.width * dimensions.height > UHD_PIXELS;
    const info = uhd ? this.decode.get(codec)?.uhd : this.decode.get(codec)?.fhd;
    if (!info) return codec === 'av1' && !uhd;
    return info.smooth && (codec !== 'h265' || info.powerEfficient);
  }

  private receiveCodecs(): string[] {
    return [...this.message.codecs, ...(this.message.extendedCodecs ?? [])];
  }

  private playbackBlocked(key: string, now: number): boolean {
    return PREMIUM_CODECS.some((codec) => this.backoff.blocked(`${key}/${codec}`, now));
  }

  isVisible(publication: TrackPublication, local = false): boolean {
    if (!local) return this.selected.has(publication.trackSid);
    const state = this.sources.get(publication.source);
    // Hide the old publication during publish-before-unpublish transitions too.
    return (
      !state?.variants.size || state.variants.values().next().value?.sid === publication.trackSid
    );
  }

  private enqueue<T>(work: () => Promise<T>): Promise<T> {
    const next = this.pending.then(work);
    this.pending = next.catch(() => {});
    return next;
  }

  /** Remote viewers with their hints. Hint-less viewers are pending during a short grace period. */
  private viewers(now = Date.now()): Viewer[] {
    return [...this.room.remoteParticipants.values()].map((participant) => {
      const peer = this.peers.get(participant.identity);
      if (peer)
        return {
          identity: participant.identity,
          codecs: [...peer.codecs, ...(peer.extendedCodecs ?? [])],
          demand: peer.demand,
          pending: false
        };
      if (!this.seen.has(participant.identity)) this.seen.set(participant.identity, now);
      return {
        identity: participant.identity,
        codecs: ['vp8'],
        demand: {},
        pending: now - this.seen.get(participant.identity)! < VIDEO_POLICY.hintGraceMs
      };
    });
  }

  private budgetStates(): SourceState[] {
    return [...this.sources.values(), ...this.initialSources.values()];
  }

  private async untilStopped<T>(
    task: Promise<T>,
    sourceSignal?: AbortSignal
  ): Promise<T | undefined> {
    if (this.stopped || sourceSignal?.aborted) {
      void task.catch(() => {});
      return undefined;
    }
    let abort!: () => void;
    try {
      return await Promise.race([
        task,
        new Promise<undefined>((resolve) => {
          abort = () => resolve(undefined);
          this.stopSignal.signal.addEventListener('abort', abort, { once: true });
          sourceSignal?.addEventListener('abort', abort, { once: true });
        })
      ]);
    } finally {
      this.stopSignal.signal.removeEventListener('abort', abort);
      sourceSignal?.removeEventListener('abort', abort);
    }
  }

  private sourceState(
    capture: LocalVideoTrack,
    source: Track.Source,
    options: TrackPublishOptions
  ): SourceState {
    const settings = capture.mediaStreamTrack.getSettings();
    const encoding =
      source === Track.Source.ScreenShare ? options.screenShareEncoding : options.videoEncoding;
    return {
      capture,
      options: {
        ...options,
        source,
        configureVideoEncodings: undefined,
        videoSendEncodings: undefined
      },
      variants: new Map(),
      publishedAt: new Map(),
      audience: new Map(),
      hardware: [],
      reprobe: new Set(),
      modes: {},
      encode: ['vp8'],
      bitrate: encoding?.maxBitrate ?? 2_500_000,
      fps: Math.min(settings.frameRate ?? 30, encoding?.maxFramerate ?? 30),
      width: settings.width ?? 1280,
      height: settings.height ?? 720,
      ended: () => {
        void this.stopSource(source)
          .then(() => this.onSourceEnded(source))
          .catch(() => {});
      }
    };
  }

  /** Reserve every active source before the SDK can publish the first video frame. */
  async publishInitial(
    source: Track.Source,
    options: TrackPublishOptions,
    publish: (configured: TrackPublishOptions) => Promise<LocalTrackPublication | undefined>
  ): Promise<LocalTrackPublication | undefined> {
    if (this.sourceSignals.has(source)) throw new Error('Video source is already published');
    const sourceSignal = new AbortController();
    this.sourceSignals.set(source, sourceSignal);
    return this.enqueue(async () => {
      if (this.stopped || sourceSignal.signal.aborted) {
        this.sourceSignals.delete(source);
        return;
      }
      if (this.sources.has(source) || this.initialSources.has(source))
        throw new Error('Video source is already published');
      let initial: SourceState | undefined;
      try {
        const publishing = publish({
          ...options,
          source,
          videoCodec: 'vp8',
          backupCodec: false,
          configureVideoEncodings: async (track, encodings) => {
            if (this.stopped || sourceSignal.signal.aborted) {
              track.stop();
              throw new Error('Video session stopped');
            }
            if (!initial) {
              initial = this.sourceState(track, source, options);
              initial.variants.set('vp8', track);
              this.initialSources.set(source, initial);
              track.mediaStreamTrack.addEventListener('ended', initial.ended);
            }
            if (!this.trackBaselines.has(track))
              this.trackBaselines.set(
                track,
                encodings.map((encoding) => ({ ...encoding }))
              );
            for (const state of this.budgetStates()) {
              if (state === initial) continue;
              for (const [codec, existing] of state.variants)
                await this.applyBudget(state, codec, existing, state.variants.size, true);
            }
            if (
              this.stopped ||
              sourceSignal.signal.aborted ||
              track.mediaStreamTrack.readyState === 'ended'
            )
              throw new Error('Capture ended');
            const capped = this.budgetEncodings(
              initial,
              'vp8',
              this.trackBaselines.get(track)!,
              Math.max(1, initial.variants.size)
            );
            encodings.forEach((encoding, index) => Object.assign(encoding, capped[index]));
          }
        });
        // getUserMedia cannot be cancelled. A late completion after stop must be retired.
        void publishing
          .then(
            async (publication) => {
              if ((this.stopped || sourceSignal.signal.aborted) && publication?.track) {
                await this.room.localParticipant
                  .unpublishTrack(publication.track, true)
                  .catch(() => {});
                publication.track.stop();
              }
            },
            () => {}
          )
          .catch(() => {});
        const publication = await this.untilStopped(publishing, sourceSignal.signal);
        if (!publication || this.stopped || sourceSignal.signal.aborted) {
          if (initial) await this.discardInitial(source, initial);
          return;
        }
        const track = publication.track;
        if (
          !initial ||
          !(track instanceof LocalVideoTrack) ||
          track !== initial.capture ||
          (track.codec && track.codec !== 'vp8') ||
          track.mediaStreamTrack.readyState === 'ended'
        )
          throw new Error('Initial video publication did not use the reserved compatible encoder');
        if (track.sender) this.baselines.set(track.sender, this.trackBaselines.get(track)!);
        initial.publishedAt.set('vp8', Date.now());
        return publication;
      } catch (error) {
        if (initial) await this.discardInitial(source, initial);
        throw error;
      } finally {
        if (!this.initialSources.has(source) && !this.sources.has(source))
          this.sourceSignals.delete(source);
      }
    });
  }

  private async discardInitial(source: Track.Source, state: SourceState): Promise<void> {
    if (this.initialSources.get(source) !== state) return;
    this.initialSources.delete(source);
    state.capture.mediaStreamTrack.removeEventListener('ended', state.ended);
    await this.room.localParticipant.unpublishTrack(state.capture, true).catch(() => {});
    state.capture.stop();
    for (const other of this.budgetStates())
      for (const [codec, track] of other.variants) await this.applyBudget(other, codec, track);
  }

  async managePublished(
    publication: LocalTrackPublication,
    options: TrackPublishOptions = {}
  ): Promise<void> {
    return this.enqueue(async () => {
      if (
        this.stopped ||
        this.sources.has(publication.source) ||
        !(publication.track instanceof LocalVideoTrack)
      )
        return;
      const capture = publication.track;
      const initial = this.initialSources.get(publication.source);
      const state =
        initial?.capture === capture
          ? initial
          : this.sourceState(capture, publication.source, options);
      const capabilities = await this.untilStopped(
        webVideoCodecCapabilities(
          {
            width: state.width,
            height: state.height,
            frameRate: state.fps,
            bitrate: state.bitrate
          },
          { scalable: true }
        ),
        this.sourceSignals.get(publication.source)?.signal
      );
      if (!capabilities || this.stopped) return;
      if (capture.mediaStreamTrack.readyState === 'ended') {
        if (initial) await this.discardInitial(publication.source, initial);
        await this.onSourceEnded(publication.source);
        return;
      }
      state.hardware = capabilities.hardware;
      state.modes = capabilities.modes ?? {};
      state.encode = capabilities.encode;
      this.initialSources.delete(publication.source);
      this.sources.set(publication.source, state);
      if (!initial) capture.mediaStreamTrack.addEventListener('ended', state.ended);
      let migrated = !!initial;
      try {
        const fallback = this.fallback(state);
        if (
          publication.trackName === managedTrackName(publication.source, fallback) &&
          (!capture.codec || capture.codec === fallback)
        ) {
          state.variants.set(fallback, capture);
          state.publishedAt.set(fallback, Date.now());
        } else {
          await this.publishVariant(state, fallback);
          if (state.variants.get('vp8') === capture) await this.retireVariant(state, 'vp8');
          else await this.room.localParticipant.unpublishTrack(capture, false);
        }
        migrated = true;
        await this.reconcile(state);
      } catch {
        if (!migrated) {
          for (const track of state.variants.values()) {
            if (track === capture) continue;
            await this.room.localParticipant.unpublishTrack(track, true).catch(() => {});
            track.stop();
          }
          this.sources.delete(publication.source);
          capture.mediaStreamTrack.removeEventListener('ended', state.ended);
        }
      }
    });
  }

  private fallback(state: SourceState): Codec {
    const viewers = this.viewers();
    const current = [...state.variants.keys()].find((codec) => !isPremiumCodec(codec));
    // Do not flip the fallback for a viewer whose hints have not arrived yet.
    if (current && current !== 'vp9' && viewers.some((viewer) => viewer.pending)) return current;
    const key = `${this.room.localParticipant.identity}/${state.options.source}`;
    const supported = (codec: string) =>
      state.encode.includes(codec) &&
      viewers.every(
        (viewer) =>
          viewer.codecs.includes(codec) &&
          (!viewer.demand[key] ||
            isPremiumCodec(viewer.demand[key]) ||
            viewer.demand[key] === codec)
      );
    if (
      viewers.length &&
      this.level === 0 &&
      (current === 'vp9' || (this.uploadConstrained && this.good >= VIDEO_POLICY.recoverSamples)) &&
      (this.encodesMeasured ||
        (this.lastSpareCpuAt > 0 &&
          Date.now() - this.lastSpareCpuAt < VIDEO_POLICY.uploadWarmupMs)) &&
      this.encodeLoad < 0.5 &&
      state.modes.vp9 &&
      supported('vp9')
    )
      return 'vp9';
    if (supported('h264') && state.hardware.includes('h264') && !state.hardware.includes('vp8'))
      return 'h264';
    if (supported('vp8')) return 'vp8';
    return supported('h264') ? 'h264' : 'vp8';
  }

  private async publishVariant(state: SourceState, codec: Codec): Promise<void> {
    if (state.variants.has(codec)) return;
    const captureEnded = () => state.capture.mediaStreamTrack.readyState === 'ended';
    if (this.stopped || captureEnded()) throw new Error('Capture ended');
    const media = state.capture.mediaStreamTrack.clone();
    media.contentHint = state.capture.mediaStreamTrack.contentHint;
    const track = new LocalVideoTrack(media, undefined, true);
    let published = false;
    try {
      // Reserve the new track's share before it can send capture frames.
      for (const [current, existing] of state.variants)
        await this.applyBudget(state, current, existing, state.variants.size + 1, true);
      const options: TrackPublishOptions = {
        ...state.options,
        videoCodec: codec as VideoCodec,
        backupCodec: false,
        name: managedTrackName(state.options.source!, codec),
        // Publish exactly the mode proven on this encoder; the SDK must not override it.
        simulcast: !isPremiumCodec(codec) && codec !== 'vp9',
        scalabilityMode:
          state.modes[codec] ?? (isPremiumCodec(codec) || codec === 'vp9' ? 'L1T1' : undefined)
      };
      const initial = computeVideoEncodings(
        state.options.source === Track.Source.ScreenShare,
        state.width,
        state.height,
        options
      );
      const capped = this.budgetEncodings(state, codec, initial, state.variants.size + 1);
      options.videoSendEncodings = initial.map((encoding, index) => ({
        ...encoding,
        ...capped[index]
      }));
      this.trackBaselines.set(track, initial);
      await this.room.localParticipant.publishTrack(track, options);
      if (track.sender) this.baselines.set(track.sender, initial);
      published = true;
      if (track.codec && track.codec !== codec)
        throw new Error('Server substituted an unproven video codec');
      if (this.stopped || captureEnded()) {
        throw new Error('Capture ended');
      }
      state.variants.set(codec, track);
      state.publishedAt.set(codec, Date.now());
      await this.applyBudget(state, codec, track, state.variants.size, true);
    } catch (error) {
      if (published) await this.room.localParticipant.unpublishTrack(track, true).catch(() => {});
      state.variants.delete(codec);
      state.publishedAt.delete(codec);
      track.stop();
      for (const [current, existing] of state.variants)
        await this.applyBudget(state, current, existing);
      throw error;
    }
  }

  private premiumKey(state: SourceState, codec: Codec): string {
    return `publish/${state.options.source}/${codec}/${state.width}x${state.height}/${state.fps}/${state.modes[codec] ?? 'L1T1'}`;
  }

  private async retireVariant(state: SourceState, codec: Codec): Promise<void> {
    const track = state.variants.get(codec);
    if (!track) return;
    await this.room.localParticipant.unpublishTrack(track, track !== state.capture);
    state.variants.delete(codec);
    state.publishedAt.delete(codec);
  }

  private async reconcile(state: SourceState): Promise<void> {
    const now = Date.now();
    const fallback = this.fallback(state);
    const key = `${this.room.localParticipant.identity}/${state.options.source}`;
    const viewers = this.viewers(now);
    const known = viewers.filter((viewer) => !viewer.pending);
    const audience = (codec: Codec) =>
      known.filter(
        (viewer) =>
          viewer.codecs.includes(codec) && (!viewer.demand[key] || viewer.demand[key] === codec)
      ).length;
    const ranked = PREMIUM_CODECS.filter(
      (codec) =>
        state.hardware.includes(codec) && !this.backoff.blocked(this.premiumKey(state, codec), now)
    ).sort((a, b) => audience(b) - audience(a));
    const candidate = ranked[0];
    const count = candidate ? audience(candidate) : 0;
    const others = known.length - count;
    const oldPremium = [...state.variants.keys()].find(isPremiumCodec);
    const fallbackBudget = Math.min(
      state.bitrate,
      VIDEO_POLICY.totalBitrate / Math.max(1, this.budgetStates().length)
    );
    const dualAllowed =
      this.level === 0 &&
      !this.uploadConstrained &&
      count >= Math.ceil(known.length * VIDEO_POLICY.dualCastShare) &&
      (oldPremium || this.availableUpload === undefined || this.availableUpload >= fallbackBudget);
    const premium =
      candidate && count > 0 && (others === 0 ? this.level < 3 : dualAllowed)
        ? candidate
        : undefined;
    const pendingNeedsFallback = viewers.some((viewer) => viewer.pending);
    const needsFallback = !premium || others > 0 || pendingNeedsFallback;
    if (
      oldPremium &&
      oldPremium === candidate &&
      !premium &&
      count > 0 &&
      (others === 0 || count >= Math.ceil(known.length * VIDEO_POLICY.dualCastShare)) &&
      (this.level > 0 || this.uploadConstrained) &&
      !this.backoff.blocked(this.premiumKey(state, oldPremium), now)
    )
      this.backoff.fail(this.premiumKey(state, oldPremium), now);
    state.audience = new Map([
      [
        fallback,
        needsFallback ? (premium ? others + viewers.length - known.length : viewers.length) : 0
      ]
    ]);
    if (premium) state.audience.set(premium, count);
    // Establish compatibility before a premium swap, then retire the old premium.
    // Never transiently publish both premium codecs or three tracks for a source.
    if (oldPremium && oldPremium !== premium) {
      if (![...state.variants.keys()].some((codec) => !isPremiumCodec(codec)))
        await this.publishVariant(state, fallback);
      await this.retireVariant(state, oldPremium);
    }
    if (needsFallback && !state.variants.has(fallback)) {
      if (state.variants.size >= 2 && oldPremium) await this.retireVariant(state, oldPremium);
      await this.publishVariant(state, fallback);
    }
    // A newly published compatible track can replace the old one immediately.
    // Otherwise keep the old track until premium publication has succeeded.
    if (state.variants.has(fallback))
      for (const codec of [...state.variants.keys()])
        if (!isPremiumCodec(codec) && codec !== fallback) await this.retireVariant(state, codec);
    if (premium) {
      try {
        await this.publishVariant(state, premium);
      } catch {
        this.backoff.fail(this.premiumKey(state, premium), now);
        if (![...state.variants.keys()].some((codec) => !isPremiumCodec(codec)))
          await this.publishVariant(state, fallback);
        for (const [codec, track] of state.variants) await this.applyBudget(state, codec, track);
        return;
      }
    }
    if (!needsFallback && premium)
      for (const codec of [...state.variants.keys()])
        if (!isPremiumCodec(codec)) await this.retireVariant(state, codec);
    for (const [codec, track] of state.variants) await this.applyBudget(state, codec, track);
  }

  private selectSubscriptions(): void {
    this.selected.clear();
    const activeSources = new Set<string>();
    for (const participant of this.room.remoteParticipants.values()) {
      const groups = new Map<
        Track.Source,
        typeof participant.videoTrackPublications extends Map<string, infer P> ? P[] : never
      >();
      for (const publication of participant.trackPublications.values()) {
        if (publication.kind === Track.Kind.Audio) {
          publication.setSubscribed(true);
          continue;
        }
        if (publication.kind !== Track.Kind.Video) continue;
        if (!this.videoVisible) {
          publication.setSubscribed(false);
          continue;
        }
        const group = groups.get(publication.source) ?? [];
        group.push(publication);
        groups.set(publication.source, group);
      }
      for (const [source, publications] of groups) {
        const key = `${participant.identity}/${source}`;
        activeSources.add(key);
        const managed = publications.filter((p) => managedCodec(p));
        if (!managed.length) {
          for (const p of publications) {
            p.setSubscribed(true);
            this.selected.add(p.trackSid);
          }
          continue;
        }
        const codecs = this.receiveCodecs();
        const premiums = managed.filter((p) => isPremiumCodec(managedCodec(p)));
        const premium = premiums.find((p) => codecs.includes(managedCodec(p)!));
        const lowerSpatialLayer =
          !!premium?.dimensions &&
          premium.simulcasted &&
          !this.premiumPlayable(managedCodec(premium)!, premium.dimensions) &&
          this.premiumPlayable(managedCodec(premium)!, { width: 1280, height: 720 });
        if (lowerSpatialLayer) premium!.setVideoQuality(VideoQuality.LOW);
        if (
          premium?.dimensions &&
          !lowerSpatialLayer &&
          !this.message.demand[key] &&
          !this.capabilityDemand.has(key) &&
          !this.premiumPlayable(managedCodec(premium)!, premium.dimensions)
        ) {
          this.message.demand[key] = this.message.codecs.includes('vp8') ? 'vp8' : 'h264';
          this.capabilityDemand.add(key);
        }
        if (premium && !this.receiverEvidence.has(key)) premium.setVideoFPS(30);
        // Backoff applies to the failing codec; another proven premium can still play.
        const compatible = managed.filter(
          (p) =>
            codecs.includes(managedCodec(p)!) &&
            (!isPremiumCodec(managedCodec(p)) ||
              !this.backoff.blocked(`${key}/${managedCodec(p)}`, Date.now()))
        );
        const availablePremium = PREMIUM_CODECS.map((codec) =>
          compatible.find((p) => managedCodec(p) === codec)
        ).find(Boolean);
        if (!this.message.demand[key] && !availablePremium && this.playbackBlocked(key, Date.now()))
          this.message.demand[key] = this.message.codecs.includes('vp8') ? 'vp8' : 'h264';
        const wanted = this.message.demand[key];
        const selected =
          compatible.find((p) => managedCodec(p) === wanted) ??
          (wanted && !isPremiumCodec(wanted)
            ? compatible.find((p) => !isPremiumCodec(managedCodec(p)))
            : undefined) ??
          availablePremium ??
          compatible[0];
        if (selected) {
          this.selected.add(selected.trackSid);
          if ((this.receiverEvidence.get(key)?.good ?? 0) >= VIDEO_POLICY.recoverSamples)
            selected.setVideoFPS(60);
        }
        for (const p of publications) p.setSubscribed(p === selected);
      }
    }
    for (const key of [...this.receiverEvidence.keys(), ...Object.keys(this.message.demand)]) {
      if (!activeSources.has(key)) {
        this.receiverEvidence.delete(key);
        this.capabilityDemand.delete(key);
        delete this.message.demand[key];
        delete this.message.health[key];
      }
    }
    const activeTracks = new Set(this.selected);
    for (const state of this.sources.values()) {
      for (const track of state.variants.values())
        activeTracks.add(track.sid ?? track.mediaStreamTrack.id);
    }
    for (const key of this.previous.keys()) {
      if (!activeTracks.has(key.split('/')[0])) this.previous.delete(key);
    }
    this.reportNotice();
  }

  /** Sends hints when they change, on membership events, and otherwise every `announceMs`. */
  private async announce(force = false): Promise<void> {
    if (this.stopped) return;
    const payload = JSON.stringify(this.message);
    const now = Date.now();
    if (
      !force &&
      payload === this.lastAnnounced &&
      now - this.lastAnnounceAt < VIDEO_POLICY.announceMs
    )
      return;
    this.lastAnnounced = payload;
    this.lastAnnounceAt = now;
    try {
      await this.room.localParticipant.publishData(new TextEncoder().encode(payload), {
        reliable: true,
        topic: VIDEO_TOPIC
      });
    } catch {
      this.lastAnnounced = ''; // Reconnect/next sample retries capability and demand exchange.
    }
  }

  private async sample(
    track: { getRTCStatsReport(): Promise<RTCStatsReport | undefined>; sender?: RTCRtpSender },
    id: string,
    receiver: boolean
  ): Promise<{
    limitation: ReturnType<typeof videoBottleneck>;
    software: boolean;
    cpuMeasured: boolean;
    available?: number;
  }> {
    const report = await track.getRTCStatsReport();
    let result: ReturnType<typeof videoBottleneck> = 'unknown';
    let software = false;
    let available: number | undefined;
    const encodings = receiver ? [] : (track.sender?.getParameters().encodings ?? []);
    let cpuReports = 0;
    let cpuMeasured = true;
    report?.forEach((stat) => {
      if (
        !receiver &&
        stat.type === 'candidate-pair' &&
        stat.nominated &&
        typeof stat.availableOutgoingBitrate === 'number'
      )
        available = stat.availableOutgoingBitrate;
      if (
        stat.type !== (receiver ? 'inbound-rtp' : 'outbound-rtp') ||
        (stat.kind && stat.kind !== 'video')
      )
        return;
      const next: VideoSample = {
        timestamp: stat.timestamp,
        frames: receiver ? stat.framesDecoded : stat.framesEncoded,
        receivedFrames: stat.framesReceived,
        processing: receiver ? stat.totalDecodeTime : stat.totalEncodeTime,
        dropped: stat.framesDropped,
        lost: stat.packetsLost,
        packets: stat.packetsReceived,
        bytes: receiver ? stat.bytesReceived : stat.bytesSent,
        limitation: stat.qualityLimitationReason
      };
      const key = `${id}/${stat.id}`;
      const previous = this.previous.get(key);
      const hardware = receiver ? isHardwareVideoDecoder(stat) : isHardwareVideoEncoder(stat);
      if (!receiver) {
        const encoding = encodings.find((encoding) => encoding.rid === stat.rid);
        const paused = stat.active === false || encoding?.active === false;
        if (!paused) {
          cpuReports++;
          const advancing =
            previous &&
            next.timestamp > previous.timestamp &&
            next.frames !== undefined &&
            previous.frames !== undefined &&
            next.frames > previous.frames;
          const processingKnown =
            next.processing !== undefined &&
            previous?.processing !== undefined &&
            Number.isFinite(next.processing) &&
            next.processing >= previous.processing;
          cpuMeasured &&= !!advancing && (hardware || processingKnown);
        }
      }
      if (receiver && !hardware && stat.framesDecoded > (previous?.frames ?? 0)) software = true;
      if (!receiver && !hardware && stat.framesEncoded > (previous?.frames ?? 0)) software = true;
      // Hardware encode wall time can overlap on independent engines; it is
      // not CPU consumption. Its own deadline/limitation checks still apply.
      if (!receiver && !hardware && previous && next.timestamp > previous.timestamp) {
        this.encodeLoad +=
          Math.max(0, (next.processing ?? 0) - (previous.processing ?? 0)) /
          ((next.timestamp - previous.timestamp) / 1000);
      }
      const limitation = videoBottleneck(previous, next, receiver);
      this.previous.set(key, next);
      if (limitation !== 'unknown' && (result === 'unknown' || limitation !== 'healthy'))
        result = limitation;
    });
    return {
      limitation: result,
      software,
      available,
      cpuMeasured:
        cpuReports > 0
          ? cpuMeasured
          : encodings.length > 0 && encodings.every((encoding) => encoding.active === false)
    };
  }

  private async tick(): Promise<void> {
    if (this.busy || this.stopped) return;
    this.busy = true;
    try {
      const now = Date.now();
      let encodePressure = false;
      let uploadPressure = false;
      let measured = false;
      let available: number | undefined;
      this.encodeLoad = 0;
      let allEncodesMeasured = true;
      let sampledEncodes = 0;
      for (const state of this.budgetStates())
        for (const [codec, track] of [...state.variants]) {
          const sampled = await this.sample(track, track.sid ?? track.mediaStreamTrack.id, false);
          sampledEncodes++;
          allEncodesMeasured &&= sampled.cpuMeasured;
          if (sampled.available !== undefined)
            available = Math.max(available ?? 0, sampled.available);
          if (sampled.limitation !== 'unknown') measured = true;
          if (sampled.limitation === 'encode') encodePressure = true;
          // Ramp-up reports "bandwidth" while the estimate grows; it is not pressure.
          if (
            sampled.limitation === 'upload' &&
            now - (state.publishedAt.get(codec) ?? 0) >= VIDEO_POLICY.uploadWarmupMs
          )
            uploadPressure = true;
          if (isPremiumCodec(codec) && sampled.software && state.hardware.includes(codec)) {
            // The proven premium encoder fell back to software at runtime.
            state.hardware = state.hardware.filter((value) => value !== codec);
            state.reprobe.add(codec);
            this.backoff.fail(this.premiumKey(state, codec), now);
            recordVideoHardwareResult(
              codec,
              {
                width: state.width,
                height: state.height,
                frameRate: state.fps,
                bitrate: state.bitrate,
                scalabilityMode: state.modes[codec] ?? 'L1T1'
              },
              false
            );
          }
        }
      this.availableUpload = available;
      if (this.encodeLoad > 0.8) encodePressure = true;
      this.encodesMeasured = sampledEncodes > 0 && allEncodesMeasured;
      if (encodePressure || this.encodeLoad >= 0.5) this.lastSpareCpuAt = 0;
      else if (this.encodesMeasured) this.lastSpareCpuAt = now;
      if (measured) {
        this.bad = encodePressure ? this.bad + 1 : 0;
        this.good = encodePressure ? 0 : this.good + 1;
        this.uploadBad = uploadPressure ? this.uploadBad + 1 : 0;
        this.uploadGood = uploadPressure ? 0 : this.uploadGood + 1;
      } else {
        this.good = this.uploadGood = 0;
      }
      if (!this.uploadConstrained && this.uploadBad >= VIDEO_POLICY.degradeSamples) {
        this.uploadConstrained = true;
        this.reportNotice();
      } else if (this.uploadConstrained && this.uploadGood >= VIDEO_POLICY.recoverSamples) {
        this.uploadConstrained = false;
        this.reportNotice();
      }
      if (
        now - this.changed >= VIDEO_POLICY.minChangeMs &&
        (this.bad >= VIDEO_POLICY.degradeSamples || this.good >= VIDEO_POLICY.recoverSamples)
      ) {
        const next = Math.max(
          0,
          Math.min(3, this.level + (this.bad >= VIDEO_POLICY.degradeSamples ? 1 : -1))
        );
        if (next !== this.level) {
          this.level = next;
          this.changed = now;
          this.bad = this.good = 0;
          this.reportNotice();
        }
      }
      for (const participant of this.room.remoteParticipants.values())
        for (const publication of participant.videoTrackPublications.values()) {
          if (!publication.track || !this.selected.has(publication.trackSid)) continue;
          const key = `${participant.identity}/${publication.source}`;
          const sampled = await this.sample(publication.track, publication.trackSid, true);
          const codec = managedCodec(publication);
          const lostHevcHardware = codec === 'h265' && sampled.software;
          if (lostHevcHardware) {
            this.message.extendedCodecs = this.message.extendedCodecs?.filter(
              (value) => value !== 'h265'
            );
            this.decode.delete('h265');
            if (!this.hevcDecodeReprobe) this.backoff.fail('decode/h265', now);
            this.hevcDecodeReprobe = true;
          }
          const limitation = lostHevcHardware ? 'decode' : sampled.limitation;
          if (limitation === 'unknown') {
            const previous = this.receiverEvidence.get(key);
            if (previous) previous.bad = previous.good = 0;
            continue;
          }
          const health = limitation as Health;
          const evidence = this.receiverEvidence.get(key) ?? { bad: 0, good: 0, changed: 0 };
          evidence.bad = lostHevcHardware
            ? VIDEO_POLICY.degradeSamples
            : health === 'healthy'
              ? 0
              : evidence.bad + 1;
          evidence.good = health === 'healthy' ? evidence.good + 1 : 0;
          this.receiverEvidence.set(key, evidence);
          // Long healthy premium playback forgets earlier failures, so one bad
          // spell early in a long call does not ratchet to permanent fallback.
          if (isPremiumCodec(codec) && evidence.good >= VIDEO_POLICY.premiumTrustSamples) {
            this.backoff.clear(`${key}/${codec}`);
            if (codec === 'h265') this.backoff.clear('decode/h265');
          }
          if (!lostHevcHardware && now - evidence.changed < VIDEO_POLICY.minChangeMs) continue;
          if (evidence.bad >= VIDEO_POLICY.degradeSamples) {
            this.message.health[key] = health;
            publication.setVideoQuality(VideoQuality.LOW);
            // Give spatial SVC one adaptation interval at its lower layer before
            // requesting another encode. A software HEVC decoder is never retained.
            const lowerLayer = publication.simulcasted && !evidence.lowerLayer && !lostHevcHardware;
            if (isPremiumCodec(codec) && !lowerLayer) {
              this.message.demand[key] = this.message.codecs.includes('vp8') ? 'vp8' : 'h264';
              this.backoff.fail(`${key}/${codec}`, now);
            }
            evidence.lowerLayer = true;
            evidence.bad = 0;
            evidence.changed = now;
            this.reportNotice();
          } else if (
            evidence.good >= VIDEO_POLICY.recoverSamples &&
            (this.message.health[key] !== 'healthy' ||
              (this.message.demand[key] &&
                !this.capabilityDemand.has(key) &&
                !this.playbackBlocked(key, now)))
          ) {
            this.message.health[key] = 'healthy';
            if (!this.capabilityDemand.has(key) && !this.playbackBlocked(key, now))
              delete this.message.demand[key];
            evidence.lowerLayer = false;
            const needsLowerLayer =
              publication.simulcasted &&
              isPremiumCodec(codec) &&
              publication.dimensions &&
              !this.premiumPlayable(codec, publication.dimensions);
            publication.setVideoQuality(needsLowerLayer ? VideoQuality.LOW : VideoQuality.HIGH);
            publication.setVideoFPS(60);
            evidence.changed = now;
            this.reportNotice();
          }
        }
      if (this.hevcDecodeReprobe && !this.backoff.blocked('decode/h265', now)) {
        await this.probePremiumDecode(['h265']);
        this.hevcDecodeReprobe = false;
        if (this.stopped) return;
        if (!this.receiveCodecs().includes('h265')) {
          this.hevcDecodeReprobe = true;
          this.backoff.fail('decode/h265', now);
        }
      }
      await this.enqueue(async () => {
        if (this.stopped) return;
        for (const state of this.sources.values()) {
          // Re-probe only the failed codec/mode after its own backoff on a healthy encoder.
          for (const codec of state.reprobe) {
            if (
              this.good < VIDEO_POLICY.recoverSamples ||
              this.backoff.blocked(this.premiumKey(state, codec), now)
            )
              continue;
            state.reprobe.delete(codec);
            const capability = await webVideoCodecCapabilities(
              {
                width: state.width,
                height: state.height,
                frameRate: state.fps,
                bitrate: state.bitrate,
                scalabilityMode: state.modes[codec] ?? 'L1T1'
              },
              { fresh: true, codecs: [codec] }
            );
            if (this.stopped) return;
            if (capability.hardware.includes(codec)) state.hardware.push(codec);
          }
          await this.reconcile(state);
          for (const [codec, track] of state.variants) await this.applyBudget(state, codec, track);
        }
      });
      this.selectSubscriptions();
      await this.announce();
    } catch {
      /* Failed replacement or missing stats must not interrupt a working call. */
    } finally {
      this.busy = false;
    }
  }

  /** Preserve the SDK ladder while bounding all encoders of a source together. */
  private budgetEncodings(
    state: SourceState,
    codec: Codec,
    baseline: LayerBaseline[],
    variantCount: number
  ): RTCRtpEncodingParameters[] {
    const audience = state.audience.get(codec) ?? 0;
    const largest = Math.max(...state.audience.values());
    const priority: RTCPriorityType | undefined =
      variantCount > 1 ? (audience >= largest ? 'high' : 'low') : undefined;
    const ceiling =
      Math.min(state.bitrate, VIDEO_POLICY.totalBitrate / Math.max(1, this.budgetStates().length)) /
      Math.max(1, variantCount);
    const compatibleScale =
      variantCount > 1 && !isPremiumCodec(codec)
        ? Math.max(
            1,
            Math.max(state.width, state.height) / 1280,
            Math.min(state.width, state.height) / 720
          )
        : 1;
    const detail = state.options.degradationPreference === 'maintain-resolution';
    const rates = baseline.map((layer) => layer.maxBitrate ?? state.bitrate / baseline.length);
    // Legacy SVC exposes inclusive layers; simulcast sends independent encodes.
    const total =
      isPremiumCodec(codec) || codec === 'vp9'
        ? Math.max(...rates)
        : rates.reduce((sum, rate) => sum + rate, 0);
    const share = total > ceiling ? ceiling / total : 1;
    return baseline.map((layer, index) => {
      const allocated = rates[index] * share;
      const scaled = allocated * VIDEO_POLICY.bitrateStep ** this.level;
      const fps = layer.maxFramerate ?? state.fps;
      const floor = detail ? VIDEO_POLICY.detailFpsFloor : VIDEO_POLICY.motionFpsFloor;
      const step = detail ? VIDEO_POLICY.detailFpsStep : VIDEO_POLICY.motionFpsStep;
      const scale = layer.scaleResolutionDownBy ?? 1;
      return {
        maxBitrate: Math.floor(Math.max(Math.min(allocated, 150_000), scaled)),
        maxFramerate: Math.max(Math.min(fps, floor), fps * step ** this.level),
        scaleResolutionDownBy:
          (codec === 'av1' || codec === 'vp9') &&
          baseline.length > 1 &&
          layer.scaleResolutionDownBy === undefined
            ? undefined
            : Math.max(
                compatibleScale,
                detail
                  ? scale * (this.level >= 3 ? 2 : 1)
                  : scale * VIDEO_POLICY.scaleStep ** this.level
              ),
        priority: priority ?? layer.priority
      };
    });
  }

  private async applyBudget(
    state: SourceState,
    codec: Codec,
    track: LocalVideoTrack,
    variantCount = state.variants.size,
    required = false
  ): Promise<void> {
    const sender = track.sender;
    if (!sender) {
      if (required) throw new Error('Cannot reserve an unavailable video sender');
      return;
    }
    const parameters = sender.getParameters();
    if (!parameters.encodings?.length) {
      if (required) throw new Error('Cannot reserve unavailable video encodings');
      return;
    }
    let baseline = this.baselines.get(sender);
    if (!baseline || baseline.length !== parameters.encodings.length) {
      const pristine = this.trackBaselines.get(track);
      baseline =
        pristine?.length === parameters.encodings.length
          ? pristine
          : parameters.encodings.map((encoding) => ({ ...encoding }));
      this.baselines.set(sender, baseline);
    }
    const budget = this.budgetEncodings(state, codec, baseline, variantCount);
    const detail = state.options.degradationPreference === 'maintain-resolution';
    const signature = JSON.stringify([detail, budget]);
    const preference = detail ? 'maintain-resolution' : 'maintain-framerate';
    const matches =
      parameters.degradationPreference === preference &&
      parameters.encodings.every(
        (encoding, index) =>
          encoding.active === false ||
          Object.entries(budget[index]).every(
            ([key, value]) =>
              value === undefined || encoding[key as keyof RTCRtpEncodingParameters] === value
          )
      );
    if (this.appliedBudgets.get(sender) === signature && matches) return;
    parameters.degradationPreference = preference;
    parameters.encodings.forEach((encoding, index) => Object.assign(encoding, budget[index]));
    try {
      if (typeof track.applyVideoSendEncodings === 'function')
        await track.applyVideoSendEncodings(budget, preference);
      else await sender.setParameters(parameters);
      this.appliedBudgets.set(sender, signature);
    } catch (error) {
      // Do not add another encoder if its reserved share cannot be enforced.
      if (required) throw error;
    }
  }

  async stopSource(source: Track.Source): Promise<void> {
    this.sourceSignals.get(source)?.abort();
    return this.enqueue(async () => {
      const state = this.sources.get(source) ?? this.initialSources.get(source);
      if (!state) return;
      this.sources.delete(source);
      this.initialSources.delete(source);
      this.sourceSignals.delete(source);
      state.capture.mediaStreamTrack.removeEventListener('ended', state.ended);
      try {
        for (const track of state.variants.values()) {
          try {
            await this.room.localParticipant.unpublishTrack(track, true);
          } catch {
            // Continue stopping every capture clone even while disconnected.
          } finally {
            track.stop();
          }
        }
      } finally {
        state.capture.stop();
        if (source === Track.Source.ScreenShare) {
          const audio = this.room.localParticipant.getTrackPublication?.(
            Track.Source.ScreenShareAudio
          )?.track;
          if (audio) {
            await this.room.localParticipant.unpublishTrack(audio, true).catch(() => {});
            audio.stop();
          }
        }
      }
      if (!this.stopped)
        for (const other of this.budgetStates())
          for (const [codec, track] of other.variants) await this.applyBudget(other, codec, track);
    });
  }

  async stop(): Promise<void> {
    if (typeof document !== 'undefined')
      document.removeEventListener('visibilitychange', this.visibilityChanged);
    this.stopped = true;
    this.stopSignal.abort();
    clearInterval(this.timer);
    this.timer = undefined;
    this.room.off(RoomEvent.DataReceived, this.data);
    this.room.off(RoomEvent.TrackPublished, this.event);
    this.room.off(RoomEvent.TrackUnpublished, this.event);
    this.room.off(RoomEvent.TrackSubscribed, this.event);
    this.room.off(RoomEvent.ParticipantConnected, this.connected);
    this.room.off(RoomEvent.ParticipantDisconnected, this.disconnected);
    this.room.off(RoomEvent.Reconnected, this.event);
    for (const source of [...this.sources.keys(), ...this.initialSources.keys()])
      await this.stopSource(source);
    this.peers.clear();
    this.seen.clear();
    this.previous.clear();
    this.receiverEvidence.clear();
    this.capabilityDemand.clear();
    this.notice(false);
  }
}
