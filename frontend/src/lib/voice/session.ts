import { Room, RoomEvent, Track, type Participant } from 'livekit-client';
import type { LocalTrackPublication, RemoteTrack, RemoteTrackPublication } from 'livekit-client';

export interface VoiceToken {
  token: string;
  url: string;
  room: string;
  generation: number;
  expires_at: string;
  can_speak: boolean;
  can_stream: boolean;
}

export interface VoiceTile {
  key: string;
  identity: string;
  name: string;
  source: Track.Source;
  track: Track;
  local: boolean;
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

  async toggleMicrophone(): Promise<void> {
    if (!this.connected || !this.canSpeak) return;
    const enabled = !this.microphone;
    await this.room.localParticipant.setMicrophoneEnabled(enabled);
    this.microphone = enabled;
    this.#changed();
  }

  async toggleCamera(): Promise<void> {
    if (!this.connected || !this.canStream) return;
    const enabled = !this.camera;
    await this.room.localParticipant.setCameraEnabled(enabled);
    this.camera = enabled;
    this.#changed();
  }

  async toggleScreen(): Promise<void> {
    if (!this.connected || !this.canStream) return;
    const enabled = !this.screen;
    await this.room.localParticipant.setScreenShareEnabled(enabled, { audio: true });
    this.screen = enabled;
    this.#changed();
  }

  async disconnect(): Promise<void> {
    await this.room.disconnect(true);
    this.connected = false;
    this.#changed();
  }

  tiles(): VoiceTile[] {
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

export function attachVideo(node: HTMLElement, tile: VoiceTile): { destroy: () => void } {
  const media = tile.track.attach();
  media.autoplay = true;
  if (media instanceof HTMLVideoElement) media.playsInline = true;
  media.muted = tile.local;
  node.append(media);
  return {
    destroy: () => {
      tile.track.detach(media);
      media.remove();
    }
  };
}
