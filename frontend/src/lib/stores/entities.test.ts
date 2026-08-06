import { describe, expect, it } from 'vitest';

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
});
