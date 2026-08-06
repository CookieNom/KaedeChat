import { describe, expect, it } from 'vitest';

import type { Channel } from '$lib/chat/types';
import { applyVoiceStateUpdate, type VoiceOccupant, type VoiceStateUpdate } from './occupancy';

const channels = [
  { id: '10', origin_domain: 'chat.test', type: 2 },
  { id: '20', origin_domain: 'chat.test', type: 2 }
] as Channel[];

function occupant(overrides: Partial<VoiceOccupant> = {}): VoiceOccupant {
  return {
    identity: '78@chat.test',
    user_id: '78',
    user_domain: 'chat.test',
    channel_id: '10',
    self_mute: false,
    self_deaf: false,
    server_mute: false,
    server_deaf: false,
    ...overrides
  };
}

describe('voice occupancy reconciliation', () => {
  it('removes a participant by federated user identity on leave', () => {
    const current = { '10@chat.test': [occupant()] };
    const update: VoiceStateUpdate = {
      channel_id: '10',
      user_id: '78',
      user_domain: 'chat.test',
      connected: false
    };

    expect(applyVoiceStateUpdate(current, channels, update)).toEqual({
      '10@chat.test': []
    });
  });

  it('moves a participant without leaving a ghost in the old channel', () => {
    const moved = occupant({ channel_id: '20' });
    const current = { '10@chat.test': [occupant()], '20@chat.test': [] };

    expect(
      applyVoiceStateUpdate(current, channels, {
        ...moved,
        connected: true,
        state: moved
      })
    ).toEqual({ '10@chat.test': [], '20@chat.test': [moved] });
  });

  it('replaces only the room named by an authoritative heartbeat', () => {
    const other = occupant({
      identity: '90@chat.test',
      user_id: '90',
      channel_id: '20'
    });
    const current = { '10@chat.test': [occupant()], '20@chat.test': [other] };

    expect(
      applyVoiceStateUpdate(current, channels, {
        channel_id: '10',
        heartbeat: true,
        participants: []
      })
    ).toEqual({ '10@chat.test': [], '20@chat.test': [other] });
  });

  it('updates moderation flags using the federated user pair', () => {
    const current = { '10@chat.test': [occupant()] };
    const next = applyVoiceStateUpdate(current, channels, {
      user_id: '78',
      user_domain: 'chat.test',
      server_mute: true
    });

    expect(next['10@chat.test'][0].server_mute).toBe(true);
  });
});
