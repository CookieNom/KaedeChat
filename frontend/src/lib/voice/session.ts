import { ExternalE2EEKeyProvider, Room, RoomEvent, Track, type Participant } from 'livekit-client';
import type { LocalTrackPublication, RemoteTrack, RemoteTrackPublication } from 'livekit-client';
import { userErrorMessage } from '$lib/api/client';
import { isNativeDesktop, nativeInvoke, type NativeVoiceStatus } from '$lib/platform/native';
import { base64url } from '$lib/e2ee/encoding';

export interface VoiceToken {
  token: string;
  url: string;
  room: string;
  generation: number;
  expires_at: string;
  can_speak: boolean;
  can_stream: boolean;
  can_use_vad: boolean;
  move_session_id?: string | null;
  e2ee?: boolean;
  channel_id?: string | null;
  channel_domain?: string | null;
  encryption_policy_generation?: string | null;
  encryption_epoch?: string | null;
  media_protocol?: 'livekit-e2ee-v1' | null;
  media_suite?: 'AES-256-GCM' | null;
  media_session_id?: string | null;
  media_epoch?: string | null;
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

const VOICE_CONNECT_TIMEOUT_MS = 15_000;

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
    (value.move_session_id == null ||
      (typeof value.move_session_id === 'string' &&
        /^[A-Za-z0-9_-]{32,64}$/.test(value.move_session_id))) &&
    (!value.e2ee
      ? value.media_protocol == null &&
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

export class VoiceSession extends EventTarget {
  room: Room;

  connected = false;
  connecting = false;
  microphone = false;
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
  #activeSpeakers = new Set<string>();
  #e2eeConfigured = false;
  #e2eeWorker: Worker | null = null;

  constructor() {
    super();
    this.room = this.#createRoom();
  }

  #createRoom(e2ee?: { keyProvider: ExternalE2EEKeyProvider; worker: Worker }): Room {
    const room = new Room({
      adaptiveStream: true,
      dynacast: true,
      disconnectOnPageLeave: true,
      stopLocalTrackOnUnpublish: true,
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
      this.#activeSpeakers = new Set(speakers.map((speaker) => speaker.identity));
      changed();
    });
    room.on(RoomEvent.Disconnected, () => {
      this.connected = false;
      this.moveSessionId = null;
      this.microphone = false;
      this.camera = false;
      this.screen = false;
      this.#activeSpeakers.clear();
      this.#changed();
    });
    return room;
  }

  async #configureE2EE(key: ArrayBuffer): Promise<void> {
    if (!globalThis.RTCRtpSender || !('transform' in RTCRtpSender.prototype)) {
      throw new Error('This browser does not support encrypted voice and video.');
    }
    await this.room.disconnect();
    this.#e2eeWorker?.terminate();
    const keyProvider = new ExternalE2EEKeyProvider({ ratchetSalt: 'kaede-livekit-v1' });
    await keyProvider.setKey(key);
    const worker = new Worker(new URL('livekit-client/e2ee-worker', import.meta.url), {
      type: 'module'
    });
    this.#e2eeWorker = worker;
    this.room = this.#createRoom({ keyProvider, worker });
    this.#e2eeConfigured = true;
  }

  async #configurePlaintext(): Promise<void> {
    if (!this.#e2eeConfigured) return;
    await this.room.disconnect();
    this.#e2eeWorker?.terminate();
    this.#e2eeWorker = null;
    this.room = this.#createRoom();
    this.#e2eeConfigured = false;
  }

  async connect(grant: VoiceToken, encryptionKey?: ArrayBuffer): Promise<void> {
    if (this.connected || this.connecting) return;
    if (!isUsableVoiceToken(grant)) throw new TypeError('Invalid or expired voice grant');
    if (grant.e2ee) {
      if (!encryptionKey) throw new Error('This encrypted call has no device media key.');
      await this.#configureE2EE(encryptionKey);
    } else if (encryptionKey) {
      throw new Error('A plaintext voice grant cannot use an encrypted-room key.');
    } else {
      await this.#configurePlaintext();
    }
    this.connecting = true;
    this.error = '';
    this.canSpeak = grant.can_speak;
    this.canStream = grant.can_stream;
    this.#changed();
    try {
      await withVoiceConnectTimeout(
        this.room.connect(grant.url, grant.token, { autoSubscribe: true })
      );
      this.connected = true;
      this.moveSessionId = grant.move_session_id ?? null;
      if (grant.can_speak) {
        await this.room.localParticipant.setMicrophoneEnabled(true);
        this.microphone = true;
      }
    } catch (caught) {
      await this.room.disconnect();
      this.error = userErrorMessage(
        caught,
        'Could not join voice. Check your network and microphone permission, then try again.'
      );
      throw caught;
    } finally {
      this.connecting = false;
      this.#changed();
    }
  }

  async connectNative(
    reference: string,
    isCall: boolean,
    encryptionKey?: ArrayBuffer,
    senderDeviceId?: string
  ): Promise<void> {
    if (!isNativeDesktop()) throw new Error('Native voice is unavailable.');
    if (this.connected || this.connecting) return;
    this.connecting = true;
    this.error = '';
    this.#changed();
    try {
      await nativeInvoke('native_voice_join', {
        reference,
        isCall,
        e2eeKey: encryptionKey ? base64url(new Uint8Array(encryptionKey)) : null,
        senderDeviceId: senderDeviceId ?? null
      });
      this.#startNativePolling();
      await this.#pollNativeStatus();
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
    if (isNativeDesktop()) {
      this.#nativeMuted = !this.#nativeMuted;
      await nativeInvoke('native_voice_control', {
        control: this.#nativeMuted ? 'mute' : 'unmute'
      });
      this.microphone = !this.#nativeMuted;
      this.#changed();
      return;
    }
    const enabled = !this.microphone;
    await this.room.localParticipant.setMicrophoneEnabled(enabled);
    this.microphone = enabled;
    this.#changed();
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
    await this.room.localParticipant.setCameraEnabled(enabled);
    this.camera = enabled;
    this.#changed();
  }

  async toggleScreen(): Promise<void> {
    if (!this.connected || !this.canStream) return;
    const enabled = !this.screen;
    if (isNativeDesktop()) {
      await nativeInvoke('native_voice_control', {
        control: enabled ? 'screen_on' : 'screen_off'
      });
      this.screen = enabled;
      this.#changed();
      return;
    }
    await this.room.localParticipant.setScreenShareEnabled(enabled, { audio: true });
    this.screen = enabled;
    this.#changed();
  }

  async disconnect(): Promise<void> {
    if (isNativeDesktop()) {
      if (this.#nativePoll) clearInterval(this.#nativePoll);
      this.#nativePoll = null;
      this.#nativeVideoGeneration += 1;
      this.#nativeVideo.clear();
      this.#nativeSpeakingUntil = 0;
      await nativeInvoke('native_voice_leave');
      this.connected = false;
      this.moveSessionId = null;
      this.connecting = false;
      this.#changed();
      return;
    }
    await this.room.disconnect(true);
    this.#e2eeWorker?.terminate();
    this.#e2eeWorker = null;
    this.connected = false;
    this.moveSessionId = null;
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

  attachAudio(element: HTMLElement): () => void {
    if (isNativeDesktop()) return () => undefined;
    const attached: HTMLMediaElement[] = [];
    const attachPublication = (
      publication: RemoteTrackPublication | LocalTrackPublication
    ): void => {
      if (!publication.track || publication.track.kind !== Track.Kind.Audio) return;
      const media = publication.track.attach();
      media.autoplay = true;
      element.append(media);
      attached.push(media);
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
      element.append(media);
      attached.push(media);
    };
    this.room.on(RoomEvent.TrackSubscribed, onSubscribed);
    return () => {
      this.room.off(RoomEvent.TrackSubscribed, onSubscribed);
      for (const media of attached) media.remove();
    };
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
