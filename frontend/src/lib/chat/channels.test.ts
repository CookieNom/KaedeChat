import { describe, expect, it } from 'vitest';
import { entityKey } from './refs';
import { firstNavigableChannel, groupChannels, moveChannel } from './channels';
import type { Channel } from './types';

function channel(id: string, position: number, type = 0, parent: Channel | null = null): Channel {
  return {
    id,
    origin_domain: 'chat.example',
    guild_id: '1',
    guild_domain: 'chat.example',
    type,
    name: `channel-${id}`,
    topic: null,
    position,
    parent_id: parent?.id ?? null,
    parent_domain: parent?.origin_domain ?? null,
    rate_limit_per_user: 0,
    last_message_id: null,
    last_message_domain: null
  };
}

describe('channel grouping and reordering', () => {
  it('groups category children and preserves ungrouped channels', () => {
    const category = channel('20', 1, 4);
    const groups = groupChannels([channel('30', 2, 0, category), category, channel('10', 0)]);

    expect(groups.map((group) => group.category?.id ?? null)).toEqual([null, '20']);
    expect(groups[0].channels.map((item) => item.id)).toEqual(['10']);
    expect(groups[1].channels.map((item) => item.id)).toEqual(['30']);
  });

  it('moves an ungrouped channel into a category', () => {
    const category = channel('20', 1, 4);
    const moved = moveChannel(
      [channel('10', 0), category, channel('30', 2, 0, category)],
      '10@chat.example',
      entityKey(category),
      'inside'
    );

    expect(moved.map((item) => item.id)).toEqual(['20', '30', '10']);
    expect(moved.at(-1)).toMatchObject({
      parent_id: '20',
      parent_domain: 'chat.example',
      position: 2
    });
  });

  it('reorders category children without changing their parent', () => {
    const category = channel('20', 0, 4);
    const first = channel('30', 1, 0, category);
    const second = channel('40', 2, 0, category);
    const moved = moveChannel(
      [category, first, second],
      entityKey(second),
      entityKey(first),
      'before'
    );

    expect(moved.map((item) => item.id)).toEqual(['20', '40', '30']);
    expect(moved[1].parent_id).toBe('20');
    expect(moved.map((item) => item.position)).toEqual([0, 1, 2]);
  });

  it('moves a category together with its children', () => {
    const firstCategory = channel('10', 0, 4);
    const firstChild = channel('11', 1, 0, firstCategory);
    const secondCategory = channel('20', 2, 4);
    const secondChild = channel('21', 3, 0, secondCategory);
    const moved = moveChannel(
      [firstCategory, firstChild, secondCategory, secondChild],
      entityKey(firstCategory),
      entityKey(secondCategory),
      'after'
    );

    expect(moved.map((item) => item.id)).toEqual(['20', '21', '10', '11']);
    expect(moved.map((item) => item.position)).toEqual([0, 1, 2, 3]);
  });

  it('preserves categories above uncategorized channels', () => {
    const category = channel('10', 0, 4);
    const child = channel('11', 1, 0, category);
    const groups = groupChannels([category, child, channel('20', 2)]);

    expect(groups.map((group) => group.category?.id ?? null)).toEqual(['10', null]);
    expect(flattenIds(groups)).toEqual(['10', '11', '20']);
  });

  it('moves a category before an uncategorized channel', () => {
    const ungrouped = channel('10', 0);
    const category = channel('20', 1, 4);
    const child = channel('21', 2, 0, category);
    const moved = moveChannel(
      [ungrouped, category, child],
      entityKey(category),
      entityKey(ungrouped),
      'before'
    );

    expect(moved.map((item) => item.id)).toEqual(['20', '21', '10']);
    expect(groupChannels(moved).map((group) => group.category?.id ?? null)).toEqual(['20', null]);
  });

  it('never selects a category as the guild landing channel', () => {
    const category = channel('10', 0, 4);
    const child = channel('11', 1, 0, category);
    expect(firstNavigableChannel([category, child])).toBe(child);
  });
});

function flattenIds(groups: ReturnType<typeof groupChannels>): string[] {
  return groups.flatMap((group) =>
    group.category
      ? [group.category.id, ...group.channels.map((item) => item.id)]
      : group.channels.map((item) => item.id)
  );
}
