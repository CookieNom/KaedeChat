import { compareEntityRefs, entityKey } from './refs';
import type { Channel } from './types';

export interface ChannelGroup {
  category: Channel | null;
  channels: Channel[];
}

export type ChannelDropPlacement = 'before' | 'after' | 'inside' | 'ungrouped';

function compareChannels(left: Channel, right: Channel): number {
  return left.position - right.position || compareEntityRefs(left, right);
}

function belongsTo(channel: Channel, category: Channel): boolean {
  return channel.parent_id === category.id && channel.parent_domain === category.origin_domain;
}

export function groupChannels(channels: Channel[]): ChannelGroup[] {
  const ordered = [...channels].sort(compareChannels);
  const categories = ordered.filter((channel) => channel.type === 4);
  const categoryKeys = new Set(categories.map(entityKey));
  const ungrouped = ordered.filter(
    (channel) =>
      channel.type !== 4 &&
      (channel.parent_id === null ||
        channel.parent_domain === null ||
        !categoryKeys.has(`${channel.parent_id}@${channel.parent_domain}`))
  );
  return [
    { category: null, channels: ungrouped },
    ...categories.map((category) => ({
      category,
      channels: ordered.filter((channel) => channel.type !== 4 && belongsTo(channel, category))
    }))
  ];
}

export function flattenChannelGroups(groups: ChannelGroup[]): Channel[] {
  return groups.flatMap((group) =>
    group.category ? [group.category, ...group.channels] : group.channels
  );
}

export function firstNavigableChannel(channels: Channel[] | undefined): Channel | null {
  if (!channels) return null;
  return (
    flattenChannelGroups(groupChannels(channels)).find((channel) => channel.type !== 4) ?? null
  );
}

function categoryBlock(ordered: Channel[], category: Channel): Channel[] {
  return ordered.filter(
    (channel) => entityKey(channel) === entityKey(category) || belongsTo(channel, category)
  );
}

function normalized(channels: Channel[]): Channel[] {
  return channels.map((channel, position) =>
    channel.position === position ? channel : { ...channel, position }
  );
}

export function moveChannel(
  channels: Channel[],
  draggedKey: string,
  targetKey: string | null,
  placement: ChannelDropPlacement
): Channel[] {
  const ordered = flattenChannelGroups(groupChannels(channels));
  const dragged = ordered.find((channel) => entityKey(channel) === draggedKey);
  const target = targetKey ? ordered.find((channel) => entityKey(channel) === targetKey) : null;
  if (!dragged || (targetKey && !target) || targetKey === draggedKey) return ordered;

  const moving = dragged.type === 4 ? categoryBlock(ordered, dragged) : [dragged];
  const movingKeys = new Set(moving.map(entityKey));
  const remaining = ordered.filter((channel) => !movingKeys.has(entityKey(channel)));
  let moved = moving[0];
  let insertAt = remaining.length;

  if (dragged.type === 4) {
    if (target && target.type !== 4) return ordered;
    if (target) {
      const targetBlock = categoryBlock(remaining, target);
      const targetStart = remaining.findIndex(
        (channel) => entityKey(channel) === entityKey(target)
      );
      insertAt = placement === 'after' ? targetStart + targetBlock.length : targetStart;
    }
  } else if (placement === 'ungrouped' || target === null) {
    moved = { ...dragged, parent_id: null, parent_domain: null };
    const firstCategory = remaining.findIndex((channel) => channel.type === 4);
    insertAt = firstCategory < 0 ? remaining.length : firstCategory;
  } else if (!target) {
    return ordered;
  } else if (target.type === 4 || placement === 'inside') {
    const category =
      target.type === 4
        ? target
        : remaining.find(
            (channel) =>
              channel.type === 4 &&
              channel.id === target.parent_id &&
              channel.origin_domain === target.parent_domain
          );
    if (!category) return ordered;
    moved = {
      ...dragged,
      parent_id: category.id,
      parent_domain: category.origin_domain
    };
    const block = categoryBlock(remaining, category);
    const categoryIndex = remaining.findIndex(
      (channel) => entityKey(channel) === entityKey(category)
    );
    insertAt = categoryIndex + block.length;
  } else {
    moved = {
      ...dragged,
      parent_id: target.parent_id,
      parent_domain: target.parent_domain
    };
    const targetIndex = remaining.findIndex((channel) => entityKey(channel) === entityKey(target));
    insertAt = targetIndex + (placement === 'after' ? 1 : 0);
  }

  const next = [...remaining];
  next.splice(insertAt, 0, moved, ...moving.slice(1));
  return normalized(next);
}
