import { describe, expect, it } from 'vitest';

import { expandedEncryptedGuildMentionRecipients, mentionsUser } from './mentions';

const ann = {
  id: '75512661369970688',
  origin_domain: 'chat.example',
  username: 'ann',
  handle: 'ann@chat.example'
};

const guildUser = (id: string) => ({
  id,
  origin_domain: 'guild.example',
  username: `u${id}`,
  display_name: null,
  avatar_hash: null,
  handle: `u${id}@guild.example`
});

describe('message mention matching', () => {
  it('matches complete usernames and federated handles case-insensitively', () => {
    expect(mentionsUser('Hello @ANN!', ann)).toBe(true);
    expect(mentionsUser('Hello @ann.', ann)).toBe(true);
    expect(mentionsUser('Hello @ann@chat.example.', ann)).toBe(true);
    expect(mentionsUser('Hello <@75512661369970688@chat.example>', ann)).toBe(true);
    expect(mentionsUser('Hello <@75512661369970688>', ann, 'chat.example')).toBe(true);
  });

  it('does not notify prefix matches, email fragments, or other handles', () => {
    expect(mentionsUser('Hello @anna', ann)).toBe(false);
    expect(mentionsUser('Email me at friend@ann.test', ann)).toBe(false);
    expect(mentionsUser('Hello @ann@other.example', ann)).toBe(false);
    expect(mentionsUser('Hello <@75512661369970689@chat.example>', ann)).toBe(false);
    expect(mentionsUser('Hello <@75512661369970688>', ann, 'other.example')).toBe(false);
  });

  it('expands encrypted role/everyone intent to exact qualified member refs', () => {
    const members = [
      {
        guild_id: '1',
        guild_domain: 'guild.example',
        user: guildUser('10'),
        nickname: null,
        role_ids: ['2']
      },
      {
        guild_id: '1',
        guild_domain: 'guild.example',
        user: guildUser('11'),
        nickname: null,
        role_ids: []
      }
    ];
    const roles = [
      {
        id: '2',
        origin_domain: 'guild.example',
        guild_id: '1',
        guild_domain: 'guild.example',
        name: 'notify',
        color: 0,
        permissions: '0',
        position: 1,
        hoist: false,
        mentionable: true
      }
    ];
    expect(
      expandedEncryptedGuildMentionRecipients(
        {
          userRefs: ['20@remote.example'],
          roleRefs: ['2@guild.example'],
          everyone: false
        },
        members,
        roles,
        '11@guild.example',
        true
      )
    ).toEqual(['10@guild.example', '11@guild.example', '20@remote.example']);
  });

  it('does not expand broad or unmentionable roles without permission', () => {
    const members = [
      {
        guild_id: '1',
        guild_domain: 'guild.example',
        user: guildUser('10'),
        nickname: null,
        role_ids: ['2']
      }
    ];
    const roles = [
      {
        id: '2',
        origin_domain: 'guild.example',
        guild_id: '1',
        guild_domain: 'guild.example',
        name: 'quiet',
        color: 0,
        permissions: '0',
        position: 1,
        hoist: false,
        mentionable: false
      }
    ];

    expect(
      expandedEncryptedGuildMentionRecipients(
        {
          userRefs: [],
          roleRefs: ['2@guild.example'],
          everyone: true
        },
        members,
        roles
      )
    ).toEqual([]);
  });
});
