import { Room, RoomEvent, Track, type Participant } from 'livekit-client';
import type { LocalTrackPublication, RemoteTrack, RemoteTrackPublication } from 'livekit-client';
import { isNativeDesktop, nativeInvoke, type NativeVoiceStatus } from '$lib/platform/native';

export interface VoiceToken {
  token: string;
  url: string;
  room: string;
  generation: number;
  expires_at: string;
  can_speak: boolean;
  can_stream: boolean;
  can_use_vad: boolean;
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
    Number.isFinite(expires) &&
    expires > now
  );
}

export class VoiceSession extends EventTarget {
  readonly room = new Room({
    adaptiveStream: true,
    dynacast: true,
    disconnectOnPageLeave: true,
    stopLocalTrackOnUnpublish: true
  });

  connected = false;
  connecting = false;
  microphone = false;
  camera = false;
  screen = false;
  canSpeak = false;
  canStream = false;
  error = '';
  #nativePoll: ReturnType<typeof setInterval> | null = null;
  #nativeVideoGeneration = 0;
  #nativeVideo = new Map<string, NativeVideoFrame>();
  #nativeMuted = false;
  #nativeDeafened = false;

  constructor() {
    super();
    const changed = () => this.#changed();
    this.room.on(RoomEvent.TrackSubscribed, changed);
    this.room.on(RoomEvent.TrackUnsubscribed, changed);
    this.room.on(RoomEvent.LocalTrackPublished, changed);
    this.room.on(RoomEvent.LocalTrackUnpublished, changed);
    this.room.on(RoomEvent.ParticipantConnected, changed);
    this.room.on(RoomEvent.ParticipantDisconnected, changed);
    this.room.on(RoomEvent.ActiveSpeakersChanged, changed);
    this.room.on(RoomEvent.Disconnected, () => {
      this.connected = false;
      this.microphone = false;
      this.camera = false;
      this.screen = false;
      this.#changed();
    });
  }

  async connect(grant: VoiceToken): Promise<void> {
    if (this.connected || this.connecting) return;
    if (!isUsableVoiceToken(grant)) throw new TypeError('Invalid or expired voice grant');
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
      if (grant.can_speak) {
        await this.room.localParticipant.setMicrophoneEnabled(true);
        this.microphone = true;
      }
    } catch (caught) {
      await this.room.disconnect();
      this.error = caught instanceof Error ? caught.message : 'Could not join voice.';
      throw caught;
    } finally {
      this.connecting = false;
      this.#changed();
    }
  }

  async connectNative(reference: string, isCall: boolean): Promise<void> {
    if (!isNativeDesktop()) throw new Error('Native voice is unavailable.');
    if (this.connected || this.connecting) return;
    this.connecting = true;
    this.error = '';
    this.#changed();
    try {
      await nativeInvoke('native_voice_join', { reference, isCall });
      this.#startNativePolling();
      await this.#pollNativeStatus();
      this.#startNativeVideoPolling();
    } catch (caught) {
      this.error = caught instanceof Error ? caught.message : 'Could not join voice.';
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
      this.error = status.message ?? '';
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
      await nativeInvoke('native_voice_leave');
      this.connected = false;
      this.connecting = false;
      this.#changed();
      return;
    }
    await this.room.disconnect(true);
    this.connected = false;
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
              speaking: false,
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
        speaking: participant.isSpeaking,
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
