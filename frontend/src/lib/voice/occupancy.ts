import { entityKey } from '$lib/chat/refs';
import type { Channel } from '$lib/chat/types';

export interface VoiceOccupant {
  identity: string;
  user_id: string;
  user_domain: string;
  channel_id: string;
  self_mute: boolean;
  self_deaf: boolean;
  server_mute: boolean;
  server_deaf: boolean;
  can_speak?: boolean;
  suppressed?: boolean;
  request_to_speak_timestamp?: string | null;
}

export interface VoiceStateUpdate extends Partial<VoiceOccupant> {
  channel_domain?: string;
  connected?: boolean;
  state?: VoiceOccupant;
  participants?: VoiceOccupant[];
  heartbeat?: boolean;
}

type Occupancy = Record<string, VoiceOccupant[]>;

function isVoiceLike(channel: Channel): boolean {
  return channel.type === 2 || channel.type === 13;
}

function sameUser(
  occupant: VoiceOccupant,
  update: Pick<VoiceStateUpdate, 'user_id' | 'user_domain'>
): boolean {
  return occupant.user_id === update.user_id && occupant.user_domain === update.user_domain;
}

function targetChannel(channels: Channel[], update: VoiceStateUpdate): Channel | undefined {
  return channels.find(
    (channel) =>
      isVoiceLike(channel) &&
      channel.id === update.channel_id &&
      (!update.channel_domain || channel.origin_domain === update.channel_domain)
  );
}

/**
 * Apply an incremental or authoritative voice update.
 *
 * LiveKit identities are deliberately opaque here. User identity in Kaede is
 * the federated `(user_id, user_domain)` pair, which remains stable if the
 * media-server identity encoding changes.
 */
export function applyVoiceStateUpdate(
  occupancy: Occupancy,
  channels: Channel[],
  update: VoiceStateUpdate
): Occupancy {
  if (update.heartbeat && update.participants) {
    const channel = targetChannel(channels, update);
    if (channel) {
      return {
        ...occupancy,
        [entityKey(channel)]: update.participants.filter(
          (occupant) => occupant.channel_id === channel.id
        )
      };
    }

    // Compatibility with older peers that did not include channel_id on a
    // heartbeat. Replace only channels represented in the snapshot; an empty
    // unscoped snapshot must never erase unrelated rooms.
    let next = occupancy;
    for (const represented of new Set(update.participants.map((occupant) => occupant.channel_id))) {
      const representedChannel = channels.find(
        (candidate) => isVoiceLike(candidate) && candidate.id === represented
      );
      if (representedChannel) {
        next = {
          ...next,
          [entityKey(representedChannel)]: update.participants.filter(
            (occupant) => occupant.channel_id === represented
          )
        };
      }
    }
    return next;
  }

  if (typeof update.connected === 'boolean' && update.user_id && update.user_domain) {
    const channel = targetChannel(channels, update);
    const withoutUser = Object.fromEntries(
      Object.entries(occupancy).map(([key, occupants]) => [
        key,
        occupants.filter((occupant) => !sameUser(occupant, update))
      ])
    );
    if (update.connected && update.state && channel) {
      withoutUser[entityKey(channel)] = [...(withoutUser[entityKey(channel)] ?? []), update.state];
    }
    return withoutUser;
  }

  if (update.user_id && update.user_domain) {
    return Object.fromEntries(
      Object.entries(occupancy).map(([key, occupants]) => [
        key,
        occupants.map((occupant) =>
          sameUser(occupant, update) ? { ...occupant, ...update } : occupant
        )
      ])
    );
  }

  return occupancy;
}
