import { describe, expect, it } from 'vitest';

import {
  parseGuildNavigation,
  moveGuildGroup,
  placeGuild,
  placeGuildAtTopLevel,
  placeGuildInGroup,
  reconcileGuildNavigation,
  ungroupGuilds
} from './guild-navigation';

const guilds = [
  { id: '1', origin_domain: 'home.example' },
  { id: '1', origin_domain: 'remote.example' },
  { id: '2', origin_domain: 'home.example' }
];

describe('guild navigation', () => {
  it('keeps composite references distinct and appends new guilds', () => {
    expect(
      reconcileGuildNavigation(
        parseGuildNavigation({
          items: [
            {
              kind: 'group',
              id: 'friends',
              name: 'Friends',
              guilds: [{ id: '1', origin_domain: 'remote.example' }, '9@gone.example'],
              collapsed: true
            },
            { kind: 'guild', guild: '1@home.example' }
          ]
        }),
        guilds
      )
    ).toEqual({
      items: [
        { kind: 'guild', guild: '1@remote.example' },
        { kind: 'guild', guild: '1@home.example' },
        { kind: 'guild', guild: '2@home.example' }
      ]
    });
  });

  it('creates folders, moves guilds into them, and ungroups without losing order', () => {
    let navigation = reconcileGuildNavigation({ items: [] }, guilds);
    navigation = placeGuild(navigation, '1@remote.example', '1@home.example', 'inside', 'group_1');
    navigation = placeGuildInGroup(navigation, '2@home.example', 'group_1');
    expect(navigation.items[0]).toMatchObject({
      kind: 'group',
      guilds: ['1@home.example', '1@remote.example', '2@home.example']
    });
    expect(ungroupGuilds(navigation, 'group_1').items).toEqual([
      { kind: 'guild', guild: '1@home.example' },
      { kind: 'guild', guild: '1@remote.example' },
      { kind: 'guild', guild: '2@home.example' }
    ]);
  });

  it('drops malformed and duplicate server data safely', () => {
    expect(
      parseGuildNavigation({
        items: [
          { kind: 'guild', guild: '1@home.example' },
          { kind: 'guild', guild: '1@home.example' },
          { kind: 'group', id: 'bad id', name: '', guilds: [] }
        ]
      })
    ).toEqual({ items: [{ kind: 'guild', guild: '1@home.example' }] });
  });

  it('canonicalizes singleton folders and rejects nested folder-shaped members', () => {
    expect(
      parseGuildNavigation({
        items: [
          {
            kind: 'group',
            id: 'singleton',
            name: 'Singleton',
            guilds: ['1@home.example'],
            collapsed: false
          },
          {
            kind: 'group',
            id: 'outer',
            name: 'Outer',
            guilds: [
              {
                kind: 'group',
                id: 'nested',
                name: 'Nested',
                guilds: ['2@home.example']
              }
            ]
          }
        ]
      })
    ).toEqual({ items: [{ kind: 'guild', guild: '1@home.example' }] });
  });

  it('does not mutate navigation for stale drag sources or targets', () => {
    const navigation = reconcileGuildNavigation({ items: [] }, guilds);
    expect(placeGuild(navigation, '99@home.example', '1@home.example', 'inside', 'group_1')).toBe(
      navigation
    );
    expect(placeGuildInGroup(navigation, '1@home.example', 'missing_group')).toBe(navigation);
  });

  it('moves whole groups without changing their members', () => {
    const navigation = parseGuildNavigation({
      items: [
        {
          kind: 'group',
          id: 'friends',
          name: 'Friends',
          guilds: ['1@home.example', '1@remote.example'],
          collapsed: false
        },
        { kind: 'guild', guild: '2@home.example' }
      ]
    });
    expect(moveGuildGroup(navigation, 'friends', '2@home.example', true).items).toEqual([
      { kind: 'guild', guild: '2@home.example' },
      navigation.items[0]
    ]);
  });

  it('extracts a guild at a top-level drop point and dissolves a singleton folder', () => {
    const navigation = parseGuildNavigation({
      items: [
        {
          kind: 'group',
          id: 'friends',
          name: 'Friends',
          guilds: ['1@home.example', '1@remote.example'],
          collapsed: false
        },
        { kind: 'guild', guild: '2@home.example' }
      ]
    });

    expect(placeGuildAtTopLevel(navigation, '1@home.example', 1).items).toEqual([
      { kind: 'guild', guild: '1@remote.example' },
      { kind: 'guild', guild: '1@home.example' },
      { kind: 'guild', guild: '2@home.example' }
    ]);
  });

  it('keeps the folder when reordering its members', () => {
    const navigation = parseGuildNavigation({
      items: [
        {
          kind: 'group',
          id: 'friends',
          name: 'Friends',
          guilds: ['1@home.example', '1@remote.example'],
          collapsed: false
        },
        { kind: 'guild', guild: '2@home.example' }
      ]
    });

    expect(
      placeGuild(navigation, '1@remote.example', '1@home.example', 'before', 'unused_group')
        .items[0]
    ).toMatchObject({
      kind: 'group',
      id: 'friends',
      guilds: ['1@remote.example', '1@home.example']
    });
  });
});
