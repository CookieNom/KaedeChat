import {
  LocalVideoTrack,
  RoomEvent,
  Track,
  VideoQuality,
  type LocalTrackPublication,
  type RemoteParticipant,
  type Room,
  type TrackPublication,
  type TrackPublishOptions,
  type VideoCodec
} from 'livekit-client';
import { isHardwareVideoEncoder, webVideoCodecCapabilities } from './quality';

export const VIDEO_TOPIC = 'kaede.video.v1';
const CODECS = ['av1', 'vp8', 'h264'] as const;
type Codec = (typeof CODECS)[number];
type Health = 'healthy' | 'decode' | 'download';
export interface VideoMessage {
  v: 1;
  codecs: string[];
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
      !value.codecs.every((c: unknown) => CODECS.includes(c as Codec)) ||
      !value.demand ||
      typeof value.demand !== 'object' ||
      Array.isArray(value.demand) ||
      !value.health ||
      typeof value.health !== 'object' ||
      Array.isArray(value.health)
    )
      return;
    if (
      Object.entries(value.demand).some(
        ([k, v]) => k.length > 512 || !CODECS.includes(v as Codec)
      ) ||
      Object.values(value.health).some(
        (v) => !['healthy', 'decode', 'download'].includes(v as string)
      )
    )
      return;
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

type SourceState = {
  capture: LocalVideoTrack;
  options: TrackPublishOptions;
  variants: Map<Codec, LocalVideoTrack>;
  hardware: string[];
  encode: string[];
  bitrate: number;
  fps: number;
  width: number;
  height: number;
  ended: () => void;
  nextProbe: number;
};

/** All publications use the existing Room: its E2EE manager installs transforms and rotates keys. */
export class AdaptiveVideoController {
  private sources = new Map<Track.Source, SourceState>();
  private peers = new Map<string, VideoMessage>();
  private previous = new Map<string, VideoSample>();
  private receiverEvidence = new Map<string, { bad: number; good: number; changed: number }>();
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
  private bad = 0;
  private good = 0;
  private level = 0;
  private bottleneck: 'encode' | 'upload' = 'upload';
  private changed = 0;
  private busy = false;
  private degraded = false;
  private encodeLoad = 0;
  private appliedBudgets = new WeakMap<RTCRtpSender, string>();
  private event = () => {
    this.selectSubscriptions();
    void this.announce();
  };
  private disconnected = (participant: RemoteParticipant) => {
    this.peers.delete(participant.identity);
    const prefix = `${participant.identity}/`;
    for (const key of this.receiverEvidence.keys())
      if (key.startsWith(prefix)) this.receiverEvidence.delete(key);
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
      this.level > 0 || Object.values(this.message.health).some((health) => health !== 'healthy');
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
    this.room.on(RoomEvent.ParticipantConnected, this.event);
    this.room.on(RoomEvent.ParticipantDisconnected, this.disconnected);
    this.room.on(RoomEvent.Reconnected, this.event);
    const capabilities = globalThis.RTCRtpReceiver?.getCapabilities?.('video')?.codecs ?? [];
    this.message.codecs = CODECS.filter((codec) =>
      capabilities.some((c) => c.mimeType.toLowerCase() === `video/${codec}`)
    );
    if (!this.message.codecs.length) this.message.codecs = ['vp8'];
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.visibilityChanged);
      this.videoVisible = document.visibilityState !== 'hidden';
    }
    this.event();
    this.timer = setInterval(() => {
      void this.tick();
    }, 3000);
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
      const settings = capture.mediaStreamTrack.getSettings();
      const encoding =
        publication.source === Track.Source.ScreenShare
          ? options.screenShareEncoding
          : options.videoEncoding;
      const width = settings.width ?? 1280,
        height = settings.height ?? 720;
      const fps = Math.min(settings.frameRate ?? 30, encoding?.maxFramerate ?? 30);
      const bitrate = encoding?.maxBitrate ?? 2_500_000;
      const capabilities = await webVideoCodecCapabilities({
        width,
        height,
        frameRate: fps,
        bitrate
      });
      if (this.stopped) return;
      if (capture.mediaStreamTrack.readyState === 'ended') {
        await this.onSourceEnded(publication.source);
        return;
      }
      const state: SourceState = {
        capture,
        nextProbe: Date.now() + 60_000,
        options: { ...options, source: publication.source },
        variants: new Map(),
        hardware: capabilities.hardware,
        encode: capabilities.encode,
        bitrate,
        fps,
        width,
        height,
        ended: () => {
          void this.stopSource(publication.source)
            .then(() => this.onSourceEnded(publication.source))
            .catch(() => {});
        }
      };
      this.sources.set(publication.source, state);
      capture.mediaStreamTrack.addEventListener('ended', state.ended);
      let migrated = false;
      try {
        // Establish fallback before replacing the SDK's original publication.
        const fallback = this.fallback(state);
        await this.publishVariant(state, fallback);
        await this.room.localParticipant.unpublishTrack(capture, false);
        migrated = true;
        await this.reconcile(state);
      } catch {
        if (!migrated) {
          for (const track of state.variants.values()) {
            await this.room.localParticipant.unpublishTrack(track, true).catch(() => {});
            track.stop();
          }
          this.sources.delete(publication.source);
          capture.mediaStreamTrack.removeEventListener('ended', state.ended);
        }
        // The original or successfully migrated fallback remains usable.
      }
    });
  }

  private fallback(state: SourceState): Codec {
    const viewers = [...this.room.remoteParticipants.values()];
    const supported = (codec: string) =>
      state.encode.includes(codec) &&
      viewers.every((p) => (this.peers.get(p.identity)?.codecs ?? ['vp8']).includes(codec));
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
    try {
      await this.room.localParticipant.publishTrack(track, {
        ...state.options,
        videoCodec: codec as VideoCodec,
        backupCodec: false,
        name: `${VIDEO_TOPIC}:${state.options.source}:${codec}`,
        // Hardware AV1 capability says nothing about spatial SVC. One layer is safe.
        simulcast: codec !== 'av1',
        scalabilityMode: codec === 'av1' ? 'L1T1' : undefined
      });
      if (this.stopped || captureEnded()) {
        await this.room.localParticipant.unpublishTrack(track, true).catch(() => {});
        throw new Error('Capture ended');
      }
      state.variants.set(codec, track);
    } catch (error) {
      track.stop();
      throw error;
    }
  }

  private async reconcile(state: SourceState): Promise<void> {
    const fallback = this.fallback(state);
    const key = `${this.room.localParticipant.identity}/${state.options.source}`;
    const viewers = [...this.room.remoteParticipants.values()];
    const av1 =
      state.hardware.includes('av1') &&
      this.level < 2 &&
      viewers.some((p) => {
        const peer = this.peers.get(p.identity);
        return peer?.codecs.includes('av1') && (!peer.demand[key] || peer.demand[key] === 'av1');
      });
    const needsFallback =
      !av1 ||
      viewers.some((p) => {
        const peer = this.peers.get(p.identity);
        return !peer?.codecs.includes('av1') || (peer.demand[key] && peer.demand[key] !== 'av1');
      });
    const desired: Codec[] = [
      ...(av1 ? ['av1' as const] : []),
      ...(needsFallback ? [fallback] : [])
    ];
    // Publish before retiring the old variant; failures leave working playback intact.
    for (const codec of desired) await this.publishVariant(state, codec);
    for (const [codec, track] of state.variants)
      if (!desired.includes(codec)) {
        await this.room.localParticipant.unpublishTrack(track, true);
        state.variants.delete(codec);
      }
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
        if (!this.receiverEvidence.has(key) && !this.message.demand[key]) {
          const av1 = managed.find((p) => managedCodec(p) === 'av1');
          // Start unproven software playback near 1080p30, then trial higher
          // settings after healthy playback. This is not a permanent decode cap.
          if (
            av1?.dimensions &&
            av1.dimensions.width * av1.dimensions.height > 1920 * 1080 &&
            this.message.codecs.includes('vp8')
          )
            this.message.demand[key] = 'vp8';
          av1?.setVideoFPS(30);
        }
        const wanted = this.message.demand[key];
        const compatible = managed.filter((p) => this.message.codecs.includes(managedCodec(p)!));
        const selected =
          compatible.find((p) => managedCodec(p) === wanted) ??
          (wanted && wanted !== 'av1'
            ? compatible.find((p) => managedCodec(p) !== 'av1')
            : undefined) ??
          compatible.find((p) => managedCodec(p) === 'av1') ??
          compatible[0];
        if (selected) {
          this.selected.add(selected.trackSid);
          if ((this.receiverEvidence.get(key)?.good ?? 0) >= 10) selected.setVideoFPS(60);
        }
        for (const p of publications) p.setSubscribed(p === selected);
      }
    }
    for (const key of this.receiverEvidence.keys()) {
      if (!activeSources.has(key)) {
        this.receiverEvidence.delete(key);
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

  private async announce(): Promise<void> {
    if (this.stopped) return;
    try {
      await this.room.localParticipant.publishData(
        new TextEncoder().encode(JSON.stringify(this.message)),
        { reliable: true, topic: VIDEO_TOPIC }
      );
    } catch {
      /* Reconnect/next sample retries capability and demand exchange. */
    }
  }

  private async sample(
    track: { getRTCStatsReport(): Promise<RTCStatsReport | undefined> },
    id: string,
    receiver: boolean
  ): Promise<ReturnType<typeof videoBottleneck>> {
    const report = await track.getRTCStatsReport();
    let result: ReturnType<typeof videoBottleneck> = 'unknown';
    report?.forEach((stat) => {
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
      // Hardware encode wall time can overlap on independent engines; it is
      // not CPU consumption. Its own deadline/limitation checks still apply.
      if (
        !receiver &&
        !isHardwareVideoEncoder(stat) &&
        previous &&
        next.timestamp > previous.timestamp
      ) {
        this.encodeLoad +=
          Math.max(0, (next.processing ?? 0) - (previous.processing ?? 0)) /
          ((next.timestamp - previous.timestamp) / 1000);
      }
      const limitation = videoBottleneck(previous, next, receiver);
      this.previous.set(key, next);
      if (limitation !== 'unknown' && (result === 'unknown' || limitation !== 'healthy'))
        result = limitation;
    });
    return result;
  }

  private async tick(): Promise<void> {
    if (this.busy || this.stopped) return;
    this.busy = true;
    try {
      let overloaded = false;
      let measured = false;
      this.encodeLoad = 0;
      for (const state of this.sources.values())
        for (const track of state.variants.values()) {
          const limitation = await this.sample(
            track,
            track.sid ?? track.mediaStreamTrack.id,
            false
          );
          if (limitation !== 'unknown') measured = true;
          if (limitation === 'encode' || limitation === 'upload') {
            overloaded = true;
            this.bottleneck = limitation === 'encode' ? 'encode' : 'upload';
          }
          if ([...state.variants].find(([, candidate]) => candidate === track)?.[0] === 'av1') {
            const stats = await track.getRTCStatsReport();
            stats?.forEach((stat) => {
              if (
                stat.type === 'outbound-rtp' &&
                stat.kind === 'video' &&
                stat.framesEncoded > 0 &&
                !isHardwareVideoEncoder(stat)
              ) {
                state.hardware = state.hardware.filter((codec) => codec !== 'av1');
              }
            });
          }
        }
      if (this.encodeLoad > 0.8) {
        overloaded = true;
        this.bottleneck = 'encode';
      }
      const now = Date.now();
      this.bad = overloaded ? this.bad + 1 : 0;
      this.good = overloaded || !measured ? 0 : this.good + 1;
      if (now - this.changed > 15_000 && (this.bad >= 3 || this.good >= 10)) {
        const next = Math.max(0, Math.min(3, this.level + (this.bad >= 3 ? 1 : -1)));
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
          if (sampled === 'unknown') {
            const previous = this.receiverEvidence.get(key);
            if (previous) previous.bad = previous.good = 0;
            continue;
          }
          const health = sampled as Health;
          const evidence = this.receiverEvidence.get(key) ?? { bad: 0, good: 0, changed: 0 };
          evidence.bad = health === 'healthy' ? 0 : evidence.bad + 1;
          evidence.good = health === 'healthy' ? evidence.good + 1 : 0;
          this.receiverEvidence.set(key, evidence);
          if (now - evidence.changed < 15_000) continue;
          if (evidence.bad >= 3) {
            this.message.health[key] = health;
            publication.setVideoQuality(VideoQuality.LOW);
            // AV1 is deliberately single spatial layer; request the scaled fallback.
            if (managedCodec(publication) === 'av1')
              this.message.demand[key] = this.message.codecs.includes('vp8') ? 'vp8' : 'h264';
            evidence.changed = now;
            this.reportNotice();
          } else if (
            evidence.good >= 10 &&
            (this.message.health[key] !== 'healthy' || this.message.demand[key])
          ) {
            this.message.health[key] = 'healthy';
            delete this.message.demand[key];
            publication.setVideoQuality(VideoQuality.HIGH);
            publication.setVideoFPS(60);
            evidence.changed = now;
            this.reportNotice();
          }
        }
      await this.enqueue(async () => {
        if (this.stopped) return;
        for (const state of this.sources.values()) {
          if (
            this.good >= 10 &&
            now >= state.nextProbe &&
            state.encode.includes('av1') &&
            !state.hardware.includes('av1')
          ) {
            state.nextProbe = now + 60_000;
            const capability = await webVideoCodecCapabilities({
              width: state.width,
              height: state.height,
              frameRate: state.fps,
              bitrate: state.bitrate
            });
            if (this.stopped) return;
            state.hardware = capability.hardware;
          }
          await this.reconcile(state);
          const detail = state.options.degradationPreference === 'maintain-resolution';
          for (const [codec, track] of state.variants) {
            const sender = track.sender;
            if (!sender) continue;
            const parameters = sender.getParameters();
            const signature = [
              this.level,
              this.bottleneck,
              this.sources.size,
              state.variants.size,
              parameters.encodings.length
            ].join('/');
            if (this.appliedBudgets.get(sender) === signature) continue;
            const fraction = [1, 0.7, 0.45, 0.25][this.level];
            // A single budget covers every camera/share encoder. Reserve fallback usability.
            const sourceCount = this.sources.size;
            const budget =
              Math.min(state.bitrate, 8_000_000 / sourceCount) *
              (this.bottleneck === 'upload' ? fraction : Math.sqrt(fraction));
            const variantBudget = budget / state.variants.size;
            parameters.degradationPreference = detail
              ? 'maintain-resolution'
              : 'maintain-framerate';
            for (const encoding of parameters.encodings) {
              encoding.maxBitrate = Math.max(
                150_000,
                variantBudget / Math.max(1, parameters.encodings.length)
              );
              encoding.maxFramerate = Math.max(
                detail ? 5 : 12,
                state.fps *
                  (detail || this.bottleneck === 'encode' ? fraction : Math.sqrt(fraction))
              );
              if (parameters.encodings.length === 1)
                encoding.scaleResolutionDownBy = Math.max(
                  1,
                  detail ? (this.level >= 3 ? 2 : 1) : 1 / Math.sqrt(fraction),
                  codec !== 'av1' && state.variants.size > 1 ? state.height / 720 : 1
                );
            }
            await sender
              .setParameters(parameters)
              .then(() => {
                this.appliedBudgets.set(sender, signature);
              })
              .catch(() => {
                /* Unsupported controls retain the working encode. */
              });
          }
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

  async stopSource(source: Track.Source): Promise<void> {
    return this.enqueue(async () => {
      const state = this.sources.get(source);
      if (!state) return;
      this.sources.delete(source);
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
    });
  }

  async stop(): Promise<void> {
    if (typeof document !== 'undefined')
      document.removeEventListener('visibilitychange', this.visibilityChanged);
    this.stopped = true;
    clearInterval(this.timer);
    this.timer = undefined;
    this.room.off(RoomEvent.DataReceived, this.data);
    this.room.off(RoomEvent.TrackPublished, this.event);
    this.room.off(RoomEvent.TrackUnpublished, this.event);
    this.room.off(RoomEvent.TrackSubscribed, this.event);
    this.room.off(RoomEvent.ParticipantConnected, this.event);
    this.room.off(RoomEvent.ParticipantDisconnected, this.disconnected);
    this.room.off(RoomEvent.Reconnected, this.event);
    for (const source of this.sources.keys()) await this.stopSource(source);
    this.peers.clear();
    this.previous.clear();
    this.receiverEvidence.clear();
    this.notice(false);
  }
}
