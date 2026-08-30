import { describe, expect, it } from 'vitest';
import {
  botE2eeHistoryNotice,
  botE2eeParticipationPath,
  dmBotE2eeParticipationPath
} from './bot-participation';

describe('bot E2EE participation', () => {
  it('keeps every federated authority reference in one encoded segment', () => {
    expect(botE2eeParticipationPath('1@guild.example', '2@guild.example', '3@apps.example')).toBe(
      '/guilds/1%40guild.example/channels/2%40guild.example/e2ee/bots/3%40apps.example'
    );
  });

  it('states the immutable history floor', () => {
    expect(
      botE2eeHistoryNotice({
        device_id: 'kbe_device',
        status: 'active',
        consent_generation: '2',
        joined_epoch: '7',
        history_floor_message_ref: '9@guild.example'
      })
    ).toContain('9@guild.example');
  });

  it('uses the conversation authority path for personal consent', () => {
    expect(dmBotE2eeParticipationPath('2@dm.example', '3@apps.example')).toBe(
      '/channels/2%40dm.example/e2ee/bots/3%40apps.example'
    );
  });
});
