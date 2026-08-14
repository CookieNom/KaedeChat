import { describe, expect, it } from 'vitest';

import type {
  Channel,
  GuildMemberSummary,
  Message,
  Relationship,
  UserSummary
} from '$lib/chat/types';
import { ChatEntityStore, NormalizedCollection } from './entities.svelte';

describe('normalized entity collections', () => {
  it('reconciles REST and gateway copies without duplicating order', () => {
    const collection = new NormalizedCollection<{ id: string; value: string }>((item) => item.id);
    collection.replace([{ id: '1', value: 'rest' }]);
    collection.upsert({ id: '1', value: 'gateway' });
    expect(collection.values).toEqual([{ id: '1', value: 'gateway' }]);
    expect(collection.order).toEqual(['1']);
  });

  it('normalizes nested guild channels', () => {
    const store = new ChatEntityStore();
    store.ingestGuilds([
      {
        id: '1',
        origin_domain: 'alpha.test',
        name: 'Lanterns',
        description: null,
        icon_hash: null,
        owner_id: '2',
        permission_generation: '1',
        unavailable: false,
        channels: [
          {
            id: '3',
            origin_domain: 'alpha.test',
            guild_id: '1',
            guild_domain: 'alpha.test',
            type: 0,
            name: 'general',
            topic: null,
            position: 0,
            parent_id: null,
            parent_domain: null,
            rate_limit_per_user: 0,
            last_message_id: null,
            last_message_domain: null
          }
        ]
      }
    ]);
    expect(store.channels.get('3@alpha.test')?.name).toBe('general');
  });

  it('ingests initial member presence and applies live updates', () => {
    const store = new ChatEntityStore();
    const user = {
      id: '7',
      origin_domain: 'alpha.test',
      username: 'mio',
      display_name: 'Mio',
      avatar_hash: null,
      handle: 'mio@alpha.test'
    };
    store.ingestMembers([
      {
        guild_id: '1',
        guild_domain: 'alpha.test',
        user,
        nickname: null,
        role_ids: [],
        presence: 'idle'
      }
    ]);

    expect(store.presenceFor(user)).toBe('idle');
    store.setPresence(user, 'online');
    expect(store.presenceFor(user)).toBe('online');
    expect(store.users.get('7@alpha.test')).toEqual(user);
  });

  it('preserves a same-account roster across a gateway re-identify when requested', () => {
    const store = new ChatEntityStore();
    const user: UserSummary = {
      id: '7',
      origin_domain: 'alpha.test',
      username: 'mio',
      display_name: 'Mio',
      avatar_hash: null,
      handle: 'mio@alpha.test'
    };
    store.ingestCurrentUser(user);
    store.ingestMembers([
      {
        guild_id: '1',
        guild_domain: 'alpha.test',
        user,
        nickname: null,
        role_ids: []
      }
    ]);

    store.beginGatewaySession(user);

    expect(store.currentUser).toEqual(user);
    expect(store.members.values).toHaveLength(1);
    store.beginGatewaySession({ ...user, id: '8' });
    expect(store.members.values).toEqual([]);
  });

  it('replaces visible denormalized placeholders when a profile resolves live', () => {
    const store = new ChatEntityStore();
    const placeholder: UserSummary = {
      id: '7',
      origin_domain: 'remote.test',
      username: 'history_deadbeef',
      display_name: null,
      avatar_hash: null,
      handle: 'history_deadbeef@remote.test',
      profile_resolved: false
    };
    const resolved: UserSummary = {
      ...placeholder,
      username: 'mio',
      display_name: 'Mio',
      handle: 'mio@remote.test',
      profile_resolved: true
    };
    store.messages.replace([
      {
        id: '9',
        origin_domain: 'guild.test',
        channel_id: '3',
        channel_domain: 'guild.test',
        author_id: '7',
        author_domain: 'remote.test',
        author: placeholder,
        content: 'hello',
        message_type: 0,
        flags: 0,
        client_nonce: null,
        referenced_message_id: null,
        referenced_message_domain: null,
        mention_user_refs: [],
        edited_at: null,
        deleted_at: null,
        created_at: '2026-08-12T00:00:00Z'
      } satisfies Message
    ]);
    store.members.replace([
      {
        guild_id: '2',
        guild_domain: 'guild.test',
        user: placeholder,
        nickname: null,
        role_ids: []
      } satisfies GuildMemberSummary
    ]);
    store.channels.replace([
      {
        id: '3',
        origin_domain: 'guild.test',
        guild_id: null,
        guild_domain: null,
        type: 1,
        name: null,
        topic: null,
        position: 0,
        parent_id: null,
        parent_domain: null,
        rate_limit_per_user: 0,
        last_message_id: null,
        last_message_domain: null,
        recipients: [placeholder]
      } satisfies Channel
    ]);
    store.relationships.replace([
      {
        type: 'friend',
        user: placeholder,
        created_at: '2026-08-12T00:00:00Z',
        updated_at: '2026-08-12T00:00:00Z'
      } satisfies Relationship
    ]);

    store.applyUserProfile(resolved);

    expect(store.messages.values[0].author?.username).toBe('mio');
    expect(store.members.values[0].user.username).toBe('mio');
    expect(store.channels.values[0].recipients?.[0].username).toBe('mio');
    expect(store.relationships.values[0].user.username).toBe('mio');
  });
});
