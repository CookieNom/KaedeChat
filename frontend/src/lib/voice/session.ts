import {
  DisconnectReason,
  ExternalE2EEKeyProvider,
  Room,
  RoomEvent,
  Track,
  type Participant
} from 'livekit-client';
import type { LocalTrackPublication, RemoteTrack, RemoteTrackPublication } from 'livekit-client';
import { userErrorMessage } from '$lib/api/client';
import type { Channel } from '$lib/chat/types';
import {
  isNativeDesktop,
  nativeInvoke,
  nativePrioritySpeakerIdentities,
  type NativeVoiceStatus
} from '$lib/platform/native';
import { base64url } from '$lib/e2ee/encoding';
import {
  loadMediaQuality,
  saveMediaQuality,
  webAudioPublishOptions,
  webCameraDefaults,
  webScreenShareOptions,
  type MediaQualityPreferences
} from './quality';

export interface VoiceToken {
  token: string;
  url: string;
  room: string;
  generation: number;
  connection_id: string;
  expires_at: string;
  can_speak: boolean;
  can_stream: boolean;
  can_use_vad: boolean;
  bitrate: number;
  user_limit: number;
  rtc_region: string | null;
  video_quality_mode: 1 | 2;
  move_session_id?: string | null;
  e2ee: boolean;
  channel_id?: string | null;
  channel_domain?: string | null;
  encryption_policy_generation?: string | null;
  encryption_epoch?: string | null;
  media_protocol?: 'livekit-e2ee-v1' | null;
  media_suite?: 'AES-256-GCM' | null;
  media_session_id?: string | null;
  media_epoch?: string | null;
}

export type VoiceChannelPolicy = Pick<
  Channel,
  | 'id'
  | 'origin_domain'
  | 'encryption_mode'
  | 'encryption_state'
  | 'encryption_policy_generation'
  | 'encryption_epoch'
  | 'bitrate'
  | 'user_limit'
  | 'rtc_region'
  | 'video_quality_mode'
>;

export interface VoiceMediaPolicy {
  bitrate: number;
  user_limit: number;
  rtc_region: string | null;
  video_quality_mode: 1 | 2;
}

export interface ExpectedVoicePolicy extends VoiceMediaPolicy {
  e2ee: boolean;
  room: string;
  channel_id: string;
  channel_domain: string;
  encryption_policy_generation: string | null;
  encryption_epoch: string | null;
  media_protocol: 'livekit-e2ee-v1' | null;
  media_suite: 'AES-256-GCM' | null;
  media_session_id: string | null;
  media_epoch: string | null;
}

export interface VoiceTile {
  key: string;
  identity: string;
  name: string;
  source: Track.Source | 'native_camera';
  track?: Track;
  nativeFrame?: NativeVideoFrame;
  local: boolean;
}

interface NativeVideoFrame {
  width: number;
  height: number;
  rgba: Uint8ClampedArray;
}

export interface VoiceParticipant {
  key: string;
  identity: string;
  name: string;
  local: boolean;
  speaking: boolean;
  microphone: boolean;
  camera: boolean;
  screen: boolean;
}

export type VoiceRoomFactory = (options: ConstructorParameters<typeof Room>[0]) => Room;

export interface SelfVoiceState {
  self_mute: boolean;
  self_deaf: boolean;
}

export type VoiceStatePublisher = (state: SelfVoiceState) => Promise<void>;

const VOICE_CONNECT_TIMEOUT_MS = 15_000;
const DEFAULT_VOICE_MEDIA_POLICY: VoiceMediaPolicy = {
  bitrate: 64_000,
  user_limit: 0,
  rtc_region: null,
  video_quality_mode: 1
};

function validRtcRegion(value: unknown): value is string | null {
  if (value === null) return true;
  if (typeof value !== 'string') return false;
  const length = [...value].length;
  return length >= 1 && length <= 64;
}

function parseVoiceMediaPolicy(value: {
  bitrate: unknown;
  user_limit: unknown;
  rtc_region: unknown;
  video_quality_mode: unknown;
}): VoiceMediaPolicy | null {
  if (
    typeof value.bitrate !== 'number' ||
    !Number.isInteger(value.bitrate) ||
    value.bitrate < 8_000 ||
    value.bitrate > 384_000 ||
    typeof value.user_limit !== 'number' ||
    !Number.isInteger(value.user_limit) ||
    value.user_limit < 0 ||
    value.user_limit > 10_000 ||
    !validRtcRegion(value.rtc_region) ||
    (value.video_quality_mode !== 1 && value.video_quality_mode !== 2)
  ) {
    return null;
  }
  return {
    bitrate: value.bitrate,
    user_limit: value.user_limit,
    rtc_region: value.rtc_region,
    video_quality_mode: value.video_quality_mode
  };
}

function voiceMediaPolicyFromGrant(grant: VoiceToken): VoiceMediaPolicy | null {
  return parseVoiceMediaPolicy(grant);
}

function voiceMediaPolicyFromChannel(channel: VoiceChannelPolicy): VoiceMediaPolicy | null {
  const policy = {
    bitrate: channel.bitrate ?? DEFAULT_VOICE_MEDIA_POLICY.bitrate,
    user_limit: channel.user_limit ?? DEFAULT_VOICE_MEDIA_POLICY.user_limit,
    rtc_region: channel.rtc_region ?? DEFAULT_VOICE_MEDIA_POLICY.rtc_region,
    video_quality_mode: channel.video_quality_mode ?? DEFAULT_VOICE_MEDIA_POLICY.video_quality_mode
  };
  return parseVoiceMediaPolicy(policy);
}

function sameVoiceMediaPolicy(left: VoiceMediaPolicy, right: VoiceMediaPolicy): boolean {
  return (
    left.bitrate === right.bitrate &&
    left.user_limit === right.user_limit &&
    left.rtc_region === right.rtc_region &&
    left.video_quality_mode === right.video_quality_mode
  );
}

export class VoiceConnectionFence {
  #generation = 0;

  begin(): number {
    this.#generation += 1;
    return this.#generation;
  }

  invalidate(): void {
    this.#generation += 1;
  }

  isCurrent(generation: number): boolean {
    return generation === this.#generation;
  }
}

export async function withVoiceConnectTimeout<T>(
  operation: Promise<T>,
  timeoutMs = VOICE_CONNECT_TIMEOUT_MS
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error('Voice connection timed out. Try again.')),
          timeoutMs
        );
      })
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export function isUsableVoiceToken(value: VoiceToken, now = Date.now()): boolean {
  const expires = Date.parse(value.expires_at);
  return (
    value.token.length > 20 &&
    /^wss?:\/\//.test(value.url) &&
    /^[gd]\.\d+\.\d+$/.test(value.room) &&
    Number.isInteger(value.generation) &&
    value.generation >= 0 &&
    /^[A-Za-z0-9_-]{43}$/.test(value.connection_id) &&
    (value.move_session_id == null ||
      (typeof value.move_session_id === 'string' &&
        /^[A-Za-z0-9_-]{32,64}$/.test(value.move_session_id))) &&
    typeof value.e2ee === 'boolean' &&
    voiceMediaPolicyFromGrant(value) !== null &&
    (!value.e2ee
      ? Boolean(value.channel_id) &&
        Boolean(value.channel_domain) &&
        value.encryption_policy_generation == null &&
        value.encryption_epoch == null &&
        value.media_protocol == null &&
        value.media_suite == null &&
        value.media_session_id == null &&
        value.media_epoch == null
      : Boolean(value.channel_id) &&
        Boolean(value.channel_domain) &&
        /^(0|[1-9][0-9]*)$/.test(value.encryption_policy_generation ?? '') &&
        /^(0|[1-9][0-9]*)$/.test(value.encryption_epoch ?? '') &&
        value.media_protocol === 'livekit-e2ee-v1' &&
        value.media_suite === 'AES-256-GCM' &&
        /^[A-Za-z0-9_-]{43}$/.test(value.media_session_id ?? '') &&
        value.media_epoch === value.encryption_epoch) &&
    Number.isFinite(expires) &&
    expires > now
  );
}

export function voiceGrantMatchesChannelPolicy(
  grant: VoiceToken,
  channel: VoiceChannelPolicy
): boolean {
  const grantMedia = voiceMediaPolicyFromGrant(grant);
  const channelMedia = voiceMediaPolicyFromChannel(channel);
  if (!grantMedia || !channelMedia || !sameVoiceMediaPolicy(grantMedia, channelMedia)) return false;
  if (channel.encryption_mode !== 'plaintext' && channel.encryption_mode !== 'e2ee') return false;
  if (grant.e2ee !== (channel.encryption_mode === 'e2ee')) return false;
  if (`${grant.channel_id}@${grant.channel_domain}` !== `${channel.id}@${channel.origin_domain}`)
    return false;
  if (!grant.e2ee) {
    return (
      grant.encryption_policy_generation == null &&
      grant.encryption_epoch == null &&
      grant.media_protocol == null &&
      grant.media_suite == null &&
      grant.media_session_id == null &&
      grant.media_epoch == null
    );
  }
  return (
    channel.encryption_state === 'active' &&
    grant.encryption_policy_generation === channel.encryption_policy_generation &&
    grant.encryption_epoch === channel.encryption_epoch &&
    grant.media_protocol === 'livekit-e2ee-v1' &&
    grant.media_suite === 'AES-256-GCM' &&
    /^[A-Za-z0-9_-]{43}$/.test(grant.media_session_id ?? '') &&
    grant.media_epoch === grant.encryption_epoch
  );
}

export function expectedVoicePolicy(
  grant: VoiceToken,
  channel: VoiceChannelPolicy
): ExpectedVoicePolicy {
  if (!isUsableVoiceToken(grant) || !voiceGrantMatchesChannelPolicy(grant, channel)) {
    throw new Error('The voice grant did not match this channel policy. Nothing connected.');
  }
  return {
    e2ee: grant.e2ee,
    room: grant.room,
    channel_id: channel.id,
    channel_domain: channel.origin_domain,
    encryption_policy_generation: grant.encryption_policy_generation ?? null,
    encryption_epoch: grant.encryption_epoch ?? null,
    media_protocol: grant.media_protocol ?? null,
    media_suite: grant.media_suite ?? null,
    media_session_id: grant.media_session_id ?? null,
    media_epoch: grant.media_epoch ?? null,
    bitrate: grant.bitrate,
    user_limit: grant.user_limit,
    rtc_region: grant.rtc_region,
    video_quality_mode: grant.video_quality_mode
  };
}

export function voiceGrantMatchesExpectedPolicy(
  grant: VoiceToken,
  expected: ExpectedVoicePolicy
): boolean {
  return (
    isUsableVoiceToken(grant) &&
    grant.e2ee === expected.e2ee &&
    grant.room === expected.room &&
    grant.channel_id === expected.channel_id &&
    grant.channel_domain === expected.channel_domain &&
    (grant.encryption_policy_generation ?? null) === expected.encryption_policy_generation &&
    (grant.encryption_epoch ?? null) === expected.encryption_epoch &&
    (grant.media_protocol ?? null) === expected.media_protocol &&
    (grant.media_suite ?? null) === expected.media_suite &&
    (grant.media_session_id ?? null) === expected.media_session_id &&
    (grant.media_epoch ?? null) === expected.media_epoch &&
    grant.bitrate === expected.bitrate &&
    grant.user_limit === expected.user_limit &&
    grant.rtc_region === expected.rtc_region &&
    grant.video_quality_mode === expected.video_quality_mode
  );
}

export class VoiceSession extends EventTarget {
  room: Room;

  connected = false;
  connecting = false;
  encrypted: boolean | null = null;
  microphone = false;
  deafened = false;
  camera = false;
  screen = false;
  canSpeak = false;
  canStream = false;
  moveSessionId: string | null = null;
  error = '';
  #nativePoll: ReturnType<typeof setInterval> | null = null;
  #nativeVideoGeneration = 0;
  #nativeVideo = new Map<string, NativeVideoFrame>();
  #nativeMuted = false;
  #nativeDeafened = false;
  #nativeSpeakingUntil = 0;
  #nativePrioritySpeakers = new Set<string>();
  #activeSpeakers = new Set<string>();
  #remoteAudio = new Set<HTMLMediaElement>();
  #connectGeneration = 0;
  #candidateRoom: Room | null = null;
  #candidateWorker: Worker | null = null;
  #e2eeWorker: Worker | null = null;
  readonly #roomFactory: VoiceRoomFactory;
  readonly #publishVoiceState: VoiceStatePublisher;
  #mediaQuality: MediaQualityPreferences;
  #voiceMediaPolicy: VoiceMediaPolicy = { ...DEFAULT_VOICE_MEDIA_POLICY };

  constructor(
    roomFactory: VoiceRoomFactory = (options) => new Room(options),
    publishVoiceState: VoiceStatePublisher = async () => undefined
  ) {
    super();
    this.#roomFactory = roomFactory;
    this.#publishVoiceState = publishVoiceState;
    this.#mediaQuality = loadMediaQuality();
    this.room = this.#createRoom();
  }

  get voiceMediaPolicy(): VoiceMediaPolicy {
    return { ...this.#voiceMediaPolicy };
  }

  #createRoom(
    policy: VoiceMediaPolicy = DEFAULT_VOICE_MEDIA_POLICY,
    e2ee?: { keyProvider: ExternalE2EEKeyProvider; worker: Worker }
  ): Room {
    const camera = webCameraDefaults(policy.video_quality_mode);
    const room = this.#roomFactory({
      adaptiveStream: true,
      dynacast: true,
      disconnectOnPageLeave: true,
      stopLocalTrackOnUnpublish: true,
      videoCaptureDefaults: camera.capture,
      publishDefaults: {
        ...webAudioPublishOptions(this.#mediaQuality, policy.bitrate),
        ...camera.publish
      },
      ...(e2ee ? { e2ee } : {})
    });
    const changed = () => this.#changed();
    room.on(RoomEvent.TrackSubscribed, changed);
    room.on(RoomEvent.TrackUnsubscribed, changed);
    room.on(RoomEvent.LocalTrackPublished, changed);
    room.on(RoomEvent.LocalTrackUnpublished, changed);
    room.on(RoomEvent.ParticipantConnected, changed);
    room.on(RoomEvent.ParticipantDisconnected, changed);
    room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
      if (this.room !== room) return;
      this.#activeSpeakers = new Set(speakers.map((speaker) => speaker.identity));
      changed();
    });
    room.on(RoomEvent.Disconnected, (reason) => {
      if (this.room !== room) return;
      this.connected = false;
      this.encrypted = null;
      this.moveSessionId = null;
      this.#voiceMediaPolicy = { ...DEFAULT_VOICE_MEDIA_POLICY };
      this.microphone = false;
      this.deafened = false;
      this.camera = false;
      this.screen = false;
      if (
        reason === DisconnectReason.DUPLICATE_IDENTITY ||
        reason === DisconnectReason.PARTICIPANT_REMOVED
      ) {
        this.error =
          'This voice connection was ended from another device or by a moderator. It will not reconnect automatically.';
      }
      this.#activeSpeakers.clear();
      this.#changed();
    });
    return room;
  }

  async connect(
    grant: VoiceToken,
    channel: VoiceChannelPolicy,
    encryptionKey?: ArrayBuffer
  ): Promise<void> {
    this.#mediaQuality = loadMediaQuality();
    const generation = ++this.#connectGeneration;
    const expected = expectedVoicePolicy(grant, channel);
    if (grant.e2ee && !encryptionKey) {
      throw new Error('This encrypted call has no device media key.');
    }
    if (!grant.e2ee && encryptionKey) {
      throw new Error('A plaintext voice grant cannot use an encrypted-room key.');
    }

    const previousRoom = this.room;
    const previousCandidate = this.#candidateRoom;
    const previousWorker = this.#e2eeWorker;
    const previousCandidateWorker = this.#candidateWorker;
    this.#candidateRoom = null;
    this.#candidateWorker = null;
    this.#e2eeWorker = null;
    previousWorker?.terminate();
    if (previousCandidateWorker !== previousWorker) previousCandidateWorker?.terminate();

    this.connecting = true;
    this.connected = false;
    this.encrypted = null;
    this.moveSessionId = null;
    this.microphone = false;
    this.deafened = false;
    this.camera = false;
    this.screen = false;
    this.error = '';
    this.canSpeak = grant.can_speak;
    this.canStream = grant.can_stream;
    this.#changed();

    let candidate: Room | null = null;
    let candidateWorker: Worker | null = null;
    let promoted = false;
    try {
      await Promise.allSettled(
        [
          ...new Set(
            [previousRoom, previousCandidate].filter((room): room is Room => room !== null)
          )
        ].map((room) => room.disconnect(true))
      );
      if (generation !== this.#connectGeneration) return;

      if (grant.e2ee) {
        if (!globalThis.RTCRtpSender || !('transform' in RTCRtpSender.prototype)) {
          throw new Error('This browser does not support encrypted voice and video.');
        }
        const keyProvider = new ExternalE2EEKeyProvider({ ratchetSalt: 'kaede-livekit-v1' });
        await keyProvider.setKey(encryptionKey as ArrayBuffer);
        if (generation !== this.#connectGeneration) return;
        candidateWorker = new Worker(new URL('livekit-client/e2ee-worker', import.meta.url), {
          type: 'module'
        });
        candidate = this.#createRoom(expected, { keyProvider, worker: candidateWorker });
      } else {
        candidate = this.#createRoom(expected);
      }
      this.#candidateRoom = candidate;
      this.#candidateWorker = candidateWorker;

      await withVoiceConnectTimeout(
        candidate.connect(grant.url, grant.token, { autoSubscribe: true })
      );
      if (generation !== this.#connectGeneration) return;

      if (grant.can_speak) {
        if (generation !== this.#connectGeneration) return;
        await candidate.localParticipant.setMicrophoneEnabled(true);
        if (generation !== this.#connectGeneration) {
          await candidate.localParticipant.setMicrophoneEnabled(false);
          return;
        }
      }
      if (generation !== this.#connectGeneration) return;

      this.room = candidate;
      this.#candidateRoom = null;
      this.#candidateWorker = null;
      this.#e2eeWorker = candidateWorker;
      promoted = true;
      this.connected = true;
      this.encrypted = expected.e2ee;
      this.#voiceMediaPolicy = {
        bitrate: expected.bitrate,
        user_limit: expected.user_limit,
        rtc_region: expected.rtc_region,
        video_quality_mode: expected.video_quality_mode
      };
      this.moveSessionId = grant.move_session_id ?? null;
      this.microphone = grant.can_speak;
    } catch (caught) {
      if (generation !== this.#connectGeneration) return;
      this.error = userErrorMessage(
        caught,
        'Could not join voice. Check your network and microphone permission, then try again.'
      );
      throw caught;
    } finally {
      if (!promoted || generation !== this.#connectGeneration) {
        if (this.#candidateRoom === candidate) this.#candidateRoom = null;
        if (this.#candidateWorker === candidateWorker) this.#candidateWorker = null;
        if (this.#e2eeWorker === candidateWorker) this.#e2eeWorker = null;
        candidateWorker?.terminate();
        await candidate?.disconnect(true);
      }
      if (generation === this.#connectGeneration) {
        this.connecting = false;
        this.#changed();
      }
    }
  }

  async connectNative(
    reference: string,
    isCall: boolean,
    grant: VoiceToken,
    channel: VoiceChannelPolicy,
    encryptionKey?: ArrayBuffer,
    senderDeviceId?: string,
    takeover = false
  ): Promise<void> {
    if (!isNativeDesktop()) throw new Error('Native voice is unavailable.');
    if (this.connected || this.connecting) return;
    this.connecting = true;
    this.error = '';
    this.#changed();
    try {
      const expectedPolicy = expectedVoicePolicy(grant, channel);
      await nativeInvoke('native_voice_join', {
        reference,
        isCall,
        expectedPolicy,
        e2eeKey: encryptionKey ? base64url(new Uint8Array(encryptionKey)) : null,
        senderDeviceId: senderDeviceId ?? null,
        connectionId: grant.connection_id,
        takeover
      });
      this.#startNativePolling();
      await this.#pollNativeStatus();
      this.encrypted = expectedPolicy.e2ee;
      this.#voiceMediaPolicy = {
        bitrate: expectedPolicy.bitrate,
        user_limit: expectedPolicy.user_limit,
        rtc_region: expectedPolicy.rtc_region,
        video_quality_mode: expectedPolicy.video_quality_mode
      };
      this.moveSessionId = grant.move_session_id ?? null;
      this.#startNativeVideoPolling();
    } catch (caught) {
      this.error = userErrorMessage(
        caught,
        'Could not join voice. Check your microphone permission and try again.'
      );
      throw caught;
    } finally {
      this.connecting = false;
      this.#changed();
    }
  }

  #startNativePolling(): void {
    if (this.#nativePoll) clearInterval(this.#nativePoll);
    this.#nativePoll = setInterval(() => void this.#pollNativeStatus(), 250);
  }

  async #pollNativeStatus(): Promise<void> {
    try {
      const status = await nativeInvoke<NativeVoiceStatus>('native_voice_status');
      this.connected = status.state === 'connected' || status.state === 'media_error';
      this.connecting = status.state === 'connecting' || status.state === 'reconnecting';
      this.canSpeak = status.can_speak ?? false;
      this.canStream = status.can_stream ?? false;
      this.camera = status.camera ?? false;
      this.screen = status.screen ?? false;
      this.#nativeMuted = status.muted ?? this.#nativeMuted;
      this.#nativeDeafened = status.deafened ?? this.#nativeDeafened;
      this.#nativePrioritySpeakers = nativePrioritySpeakerIdentities(status.priority_speakers);
      this.deafened = this.#nativeDeafened;
      this.microphone = this.connected && this.canSpeak && !this.#nativeMuted;
      const inputLevel = Math.max(0, status.input_level ?? 0);
      if (this.microphone && inputLevel >= 0.015) {
        // Keep the outline stable between native meter polls and across short
        // syllable gaps. Audio remains entirely in Rust; only the scalar meter
        // crosses the desktop bridge.
        this.#nativeSpeakingUntil = Date.now() + 350;
      } else if (!this.microphone) {
        this.#nativeSpeakingUntil = 0;
      }
      const statusFallback =
        status.state === 'media_error'
          ? 'Voice connected, but a media device failed. Check your microphone and output device.'
          : status.state === 'failed'
            ? 'The voice connection failed. Check your network and try joining again.'
            : '';
      this.error = status.message
        ? userErrorMessage(
            { message: status.message },
            statusFallback || 'Voice encountered an error.'
          )
        : statusFallback;
      if (status.state === 'disconnected' || status.state === 'failed') {
        this.encrypted = null;
        this.moveSessionId = null;
        this.#voiceMediaPolicy = { ...DEFAULT_VOICE_MEDIA_POLICY };
        if (this.#nativePoll) clearInterval(this.#nativePoll);
        this.#nativePoll = null;
      }
      this.#changed();
    } catch {
      // A transient bridge failure must not tear down a still-live native room.
    }
  }

  #startNativeVideoPolling(): void {
    const generation = ++this.#nativeVideoGeneration;
    void (async () => {
      while (generation === this.#nativeVideoGeneration && this.connected) {
        try {
          const response = await nativeInvoke<ArrayBuffer | Uint8Array>('native_voice_next_video');
          const bytes = response instanceof Uint8Array ? response : new Uint8Array(response);
          if (bytes.byteLength === 0) continue;
          const frame = decodeNativeVideoFrame(bytes);
          if (!frame) continue;
          if (frame.removed) this.#nativeVideo.delete(frame.participant);
          else this.#nativeVideo.set(frame.participant, frame.image);
          this.#changed();
        } catch {
          if (generation === this.#nativeVideoGeneration) {
            await new Promise((resolve) => setTimeout(resolve, 250));
          }
        }
      }
    })();
  }

  async toggleMicrophone(): Promise<void> {
    if (!this.connected || !this.canSpeak) return;
    const wasDeafened = this.deafened;
    const nextMuted = this.microphone;
    const nextDeafened = nextMuted ? wasDeafened : false;
    if (!nextMuted) {
      // Publish an unmute before opening capture. If realtime state is down,
      // the local microphone stays closed instead of surprising the user.
      await this.#publishVoiceState({ self_mute: false, self_deaf: false });
    }
    if (isNativeDesktop()) {
      try {
        if (!nextMuted && wasDeafened) {
          await nativeInvoke('native_voice_control', { control: 'undeafen' });
        }
        await nativeInvoke('native_voice_control', {
          control: nextMuted ? 'mute' : 'unmute'
        });
      } catch (caught) {
        if (!nextMuted) {
          if (wasDeafened) {
            await nativeInvoke('native_voice_control', { control: 'deafen' }).catch(
              () => undefined
            );
          }
          await this.#publishVoiceState({
            self_mute: true,
            self_deaf: wasDeafened
          }).catch(() => undefined);
        }
        throw caught;
      }
      this.#nativeMuted = nextMuted;
      this.#nativeDeafened = nextDeafened;
      this.microphone = !this.#nativeMuted;
      this.deafened = this.#nativeDeafened;
      this.#changed();
      if (nextMuted) {
        await this.#publishVoiceState({ self_mute: true, self_deaf: nextDeafened });
      }
      return;
    }
    try {
      await this.room.localParticipant.setMicrophoneEnabled(!nextMuted);
    } catch (caught) {
      if (!nextMuted) {
        await this.#publishVoiceState({
          self_mute: true,
          self_deaf: this.deafened
        }).catch(() => undefined);
      }
      throw caught;
    }
    this.microphone = !nextMuted;
    if (!nextMuted && wasDeafened) this.#setBrowserDeafened(false);
    this.deafened = nextDeafened;
    this.#changed();
    if (nextMuted) {
      await this.#publishVoiceState({ self_mute: true, self_deaf: nextDeafened });
    }
  }

  async toggleDeafen(): Promise<void> {
    if (!this.connected) return;
    const nextDeafened = !this.deafened;
    if (!nextDeafened) {
      await this.#publishVoiceState({ self_mute: true, self_deaf: false });
    }
    if (isNativeDesktop()) {
      try {
        await nativeInvoke('native_voice_control', {
          control: nextDeafened ? 'deafen' : 'undeafen'
        });
      } catch (caught) {
        if (!nextDeafened) {
          await this.#publishVoiceState({ self_mute: true, self_deaf: true }).catch(
            () => undefined
          );
        }
        throw caught;
      }
      this.#nativeDeafened = nextDeafened;
      if (nextDeafened) this.#nativeMuted = true;
      this.microphone = !this.#nativeMuted;
    } else {
      try {
        if (nextDeafened && this.microphone) {
          await this.room.localParticipant.setMicrophoneEnabled(false);
          this.microphone = false;
        }
        this.#setBrowserDeafened(nextDeafened);
      } catch (caught) {
        if (!nextDeafened) {
          await this.#publishVoiceState({ self_mute: true, self_deaf: true }).catch(
            () => undefined
          );
        }
        throw caught;
      }
    }
    this.deafened = nextDeafened;
    this.#changed();
    if (nextDeafened) {
      await this.#publishVoiceState({ self_mute: true, self_deaf: true });
    }
  }

  async toggleCamera(): Promise<void> {
    if (!this.connected || !this.canStream) return;
    const enabled = !this.camera;
    if (isNativeDesktop()) {
      await nativeInvoke('native_voice_control', {
        control: enabled ? 'camera_on' : 'camera_off'
      });
      this.camera = enabled;
      this.#changed();
      return;
    }
    const camera = webCameraDefaults(this.#voiceMediaPolicy.video_quality_mode);
    await this.room.localParticipant.setCameraEnabled(enabled, camera.capture, camera.publish);
    this.camera = enabled;
    this.#changed();
  }

  async startScreenShare(
    preferences: MediaQualityPreferences,
    sourceId: string | null = null
  ): Promise<void> {
    if (!this.connected || !this.canStream) return;
    if (this.screen) return;
    const previous = this.#mediaQuality;
    this.#mediaQuality = { ...preferences };
    saveMediaQuality(this.#mediaQuality);
    if (isNativeDesktop()) {
      await nativeInvoke('native_media_quality_set', {
        screenProfile: preferences.screenProfile,
        audioQuality: preferences.audioQuality,
        shareSystemAudio: preferences.shareAudio,
        sourceId
      });
      await nativeInvoke('native_voice_control', {
        control: 'screen_on'
      });
      this.screen = true;
      this.#changed();
      return;
    }
    if (previous.audioQuality !== preferences.audioQuality || previous.dtx !== preferences.dtx) {
      try {
        await this.#republishMicrophoneWithQuality();
      } catch (caught) {
        this.#mediaQuality = previous;
        saveMediaQuality(previous);
        throw caught;
      }
    }
    const preferredSurface =
      sourceId === 'browser:window'
        ? 'window'
        : sourceId === 'browser:browser'
          ? 'browser'
          : sourceId === 'browser:monitor'
            ? 'monitor'
            : undefined;
    const { capture, publish } = webScreenShareOptions(preferences, preferredSurface);
    await this.room.localParticipant.setScreenShareEnabled(true, capture, {
      ...publish,
      ...webAudioPublishOptions(preferences)
    });
    this.screen = true;
    this.#changed();
  }

  async stopScreenShare(): Promise<void> {
    if (!this.connected || !this.screen) return;
    if (isNativeDesktop()) {
      await nativeInvoke('native_voice_control', { control: 'screen_off' });
    } else {
      await this.room.localParticipant.setScreenShareEnabled(false);
    }
    this.screen = false;
    this.#changed();
  }

  async toggleScreen(): Promise<void> {
    if (this.screen) return this.stopScreenShare();
    return this.startScreenShare(this.#mediaQuality);
  }

  async #republishMicrophoneWithQuality(): Promise<void> {
    const publication = this.room.localParticipant.getTrackPublication(Track.Source.Microphone);
    const track = publication?.track;
    if (!track) return;
    const wasMuted = track.isMuted;
    const previousOptions = publication.options;
    await this.room.localParticipant.unpublishTrack(track, false);
    try {
      await this.room.localParticipant.publishTrack(
        track,
        webAudioPublishOptions(this.#mediaQuality, this.#voiceMediaPolicy.bitrate)
      );
      if (wasMuted) await track.mute();
    } catch (caught) {
      // Do not leave a connected user without a microphone if applying a
      // preference fails on an older browser/encoder.
      await this.room.localParticipant.publishTrack(track, previousOptions);
      if (wasMuted) await track.mute();
      throw caught;
    }
  }

  async disconnect(): Promise<void> {
    if (isNativeDesktop()) {
      if (this.#nativePoll) clearInterval(this.#nativePoll);
      this.#nativePoll = null;
      this.#nativeVideoGeneration += 1;
      this.#nativeVideo.clear();
      this.#nativeSpeakingUntil = 0;
      this.#nativePrioritySpeakers.clear();
      await nativeInvoke('native_voice_leave');
      this.connected = false;
      this.encrypted = null;
      this.moveSessionId = null;
      this.#voiceMediaPolicy = { ...DEFAULT_VOICE_MEDIA_POLICY };
      this.connecting = false;
      this.microphone = false;
      this.deafened = false;
      this.camera = false;
      this.screen = false;
      this.#nativeMuted = false;
      this.#nativeDeafened = false;
      this.#changed();
      return;
    }
    const generation = ++this.#connectGeneration;
    const activeRoom = this.room;
    const candidateRoom = this.#candidateRoom;
    const activeWorker = this.#e2eeWorker;
    const candidateWorker = this.#candidateWorker;
    this.#candidateRoom = null;
    this.#candidateWorker = null;
    this.#e2eeWorker = null;
    activeWorker?.terminate();
    if (candidateWorker !== activeWorker) candidateWorker?.terminate();
    await Promise.allSettled(
      [...new Set([activeRoom, candidateRoom].filter((room): room is Room => room !== null))].map(
        (room) => room.disconnect(true)
      )
    );
    if (generation !== this.#connectGeneration) return;
    this.connected = false;
    this.encrypted = null;
    this.moveSessionId = null;
    this.#voiceMediaPolicy = { ...DEFAULT_VOICE_MEDIA_POLICY };
    this.connecting = false;
    this.microphone = false;
    this.deafened = false;
    this.camera = false;
    this.screen = false;
    this.#activeSpeakers.clear();
    this.#changed();
  }

  tiles(): VoiceTile[] {
    if (isNativeDesktop()) {
      return [...this.#nativeVideo.entries()].map(([identity, nativeFrame]) => ({
        key: `native:${identity}`,
        identity,
        name: identity.split('@', 1)[0] || identity,
        source: 'native_camera',
        nativeFrame,
        local: false
      }));
    }
    const tiles: VoiceTile[] = [];
    const addParticipant = (participant: Participant, local: boolean) => {
      for (const publication of participant.trackPublications.values()) {
        const track = publication.track;
        if (!track || track.kind !== Track.Kind.Video) continue;
        tiles.push({
          key: `${participant.identity}:${publication.trackSid}`,
          identity: participant.identity,
          name: participant.name || participant.identity,
          source: publication.source,
          track,
          local
        });
      }
    };
    addParticipant(this.room.localParticipant, true);
    for (const participant of this.room.remoteParticipants.values()) {
      addParticipant(participant, false);
    }
    return tiles.sort((left, right) => {
      const leftScreen = left.source === Track.Source.ScreenShare ? 0 : 1;
      const rightScreen = right.source === Track.Source.ScreenShare ? 0 : 1;
      return leftScreen - rightScreen || left.identity.localeCompare(right.identity);
    });
  }

  participants(): VoiceParticipant[] {
    if (isNativeDesktop()) {
      return this.connected
        ? [
            {
              key: 'native-local',
              identity: 'native-local',
              name: 'You',
              local: true,
              speaking: this.microphone && Date.now() < this.#nativeSpeakingUntil,
              microphone: this.microphone,
              camera: this.camera,
              screen: this.screen
            }
          ]
        : [];
    }
    const participants: VoiceParticipant[] = [];
    const addParticipant = (participant: Participant, local: boolean) => {
      const publications = [...participant.trackPublications.values()];
      const liveSource = (source: Track.Source) =>
        publications.some((publication) => publication.source === source && !publication.isMuted);
      participants.push({
        key: participant.identity,
        identity: participant.identity,
        name: participant.name || participant.identity,
        local,
        speaking:
          participant.isSpeaking ||
          this.#activeSpeakers.has(participant.identity) ||
          (local && participant.audioLevel >= 0.015),
        microphone: liveSource(Track.Source.Microphone),
        camera: liveSource(Track.Source.Camera),
        screen: liveSource(Track.Source.ScreenShare)
      });
    };
    addParticipant(this.room.localParticipant, true);
    for (const participant of this.room.remoteParticipants.values()) {
      addParticipant(participant, false);
    }
    return participants.sort(
      (left, right) =>
        Number(right.local) - Number(left.local) || left.name.localeCompare(right.name)
    );
  }

  prioritySpeakers(): ReadonlySet<string> {
    return this.#nativePrioritySpeakers;
  }

  attachAudio(element: HTMLElement): () => void {
    if (isNativeDesktop()) return () => undefined;
    const attached: HTMLMediaElement[] = [];
    const attachPublication = (
      publication: RemoteTrackPublication | LocalTrackPublication
    ): void => {
      if (!publication.track || publication.track.kind !== Track.Kind.Audio) return;
      const media = publication.track.attach();
      media.autoplay = true;
      media.muted = this.deafened;
      element.append(media);
      attached.push(media);
      this.#remoteAudio.add(media);
    };
    for (const participant of this.room.remoteParticipants.values()) {
      for (const publication of participant.audioTrackPublications.values()) {
        attachPublication(publication);
      }
    }
    const onSubscribed = (track: RemoteTrack) => {
      if (track.kind !== Track.Kind.Audio) return;
      const media = track.attach();
      media.autoplay = true;
      media.muted = this.deafened;
      element.append(media);
      attached.push(media);
      this.#remoteAudio.add(media);
    };
    this.room.on(RoomEvent.TrackSubscribed, onSubscribed);
    return () => {
      this.room.off(RoomEvent.TrackSubscribed, onSubscribed);
      for (const media of attached) {
        this.#remoteAudio.delete(media);
        media.remove();
      }
    };
  }

  #setBrowserDeafened(deafened: boolean): void {
    for (const media of this.#remoteAudio) media.muted = deafened;
  }

  #changed(): void {
    this.dispatchEvent(new Event('change'));
  }
}

export function attachVideo(
  node: HTMLElement,
  tile: VoiceTile
): { update: (next: VoiceTile) => void; destroy: () => void } {
  if (tile.nativeFrame) {
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    node.append(canvas);
    const draw = (next: VoiceTile) => {
      const frame = next.nativeFrame;
      if (!frame || !context) return;
      canvas.width = frame.width;
      canvas.height = frame.height;
      // Tauri may deserialize the frame onto a SharedArrayBuffer-backed view.
      // ImageData deliberately accepts only an owned ArrayBuffer, so copy at
      // this UI boundary instead of weakening the native frame type.
      const pixels = new Uint8ClampedArray(frame.rgba.length);
      pixels.set(frame.rgba);
      context.putImageData(new ImageData(pixels, frame.width, frame.height), 0, 0);
    };
    draw(tile);
    return { update: draw, destroy: () => canvas.remove() };
  }
  if (!tile.track) return { update: () => undefined, destroy: () => undefined };
  const track = tile.track;
  const media = track.attach();
  media.autoplay = true;
  if (media instanceof HTMLVideoElement) media.playsInline = true;
  media.muted = tile.local;
  node.append(media);
  return {
    destroy: () => {
      track.detach(media);
      media.remove();
    },
    update: () => undefined
  };
}

function decodeNativeVideoFrame(
  bytes: Uint8Array
):
  | { participant: string; removed: true; image: NativeVideoFrame }
  | { participant: string; removed: false; image: NativeVideoFrame }
  | null {
  if (bytes.byteLength < 15 || new TextDecoder().decode(bytes.subarray(0, 4)) !== 'KVD1') {
    return null;
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const width = view.getUint32(4, true);
  const height = view.getUint32(8, true);
  const identityLength = view.getUint16(12, true);
  const removed = view.getUint8(14) === 1;
  const imageOffset = 15 + identityLength;
  if (imageOffset > bytes.byteLength) return null;
  const participant = new TextDecoder().decode(bytes.subarray(15, imageOffset));
  const expected = width * height * 4;
  if (!removed && (expected === 0 || bytes.byteLength - imageOffset !== expected)) return null;
  return {
    participant,
    removed,
    image: {
      width,
      height,
      rgba: new Uint8ClampedArray(bytes.slice(imageOffset).buffer)
    }
  };
}
