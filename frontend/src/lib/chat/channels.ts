import { compareEntityRefs, entityKey } from './refs';
import { ordinaryGuildChannels } from './threads';
import type { Channel } from './types';

export interface ChannelGroup {
  key: string;
  category: Channel | null;
  channels: Channel[];
}

export type ChannelDropPlacement = 'before' | 'after' | 'inside' | 'ungrouped';

export interface ChannelPositionRequest {
  id: string;
  position: number;
  parent_id?: string | null;
}

/** Channel-scoped resources the reorder endpoint will authorize for this batch. */
export function channelOrderPermissionTargets(
  previous: Channel[],
  next: Channel[],
  movedKey: string
): Channel[] | null {
  const previousByKey = new Map(previous.map((channel) => [entityKey(channel), channel]));
  if (!previousByKey.has(movedKey)) return null;
  const nextByKey = new Map(next.map((channel) => [entityKey(channel), channel]));
  const targets = new Map<string, Channel>();

  for (const request of channelPositionRequest(previous, next, movedKey)) {
    const after = next.find((channel) => channel.id === request.id);
    const before = after ? previousByKey.get(entityKey(after)) : undefined;
    if (!before || !after) return null;

    if (Object.hasOwn(request, 'parent_id')) {
      targets.set(entityKey(before), before);
      if (request.parent_id !== null) {
        const parent = next.find(
          (channel) =>
            channel.type === 4 &&
            channel.id === request.parent_id &&
            channel.origin_domain === after.parent_domain
        );
        if (!parent) return null;
        targets.set(entityKey(parent), parent);
      }
    } else if (before.parent_id && before.parent_domain) {
      const parent = nextByKey.get(`${before.parent_id}@${before.parent_domain}`);
      if (!parent || parent.type !== 4) return null;
      targets.set(entityKey(parent), parent);
    }
  }

  return [...targets.values()];
}

function compareChannels(left: Channel, right: Channel): number {
  return left.position - right.position || compareEntityRefs(left, right);
}

function belongsTo(channel: Channel, category: Channel): boolean {
  return channel.parent_id === category.id && channel.parent_domain === category.origin_domain;
}

export function groupChannels(channels: Channel[]): ChannelGroup[] {
  const ordered = ordinaryGuildChannels(channels).sort(compareChannels);
  const categories = ordered.filter((channel) => channel.type === 4);
  const categoryKeys = new Set(categories.map(entityKey));
  const topLevel = ordered.filter(
    (channel) =>
      channel.type === 4 ||
      channel.parent_id === null ||
      channel.parent_domain === null ||
      !categoryKeys.has(`${channel.parent_id}@${channel.parent_domain}`)
  );
  const groups: ChannelGroup[] = [];
  let ungroupedRun: Channel[] = [];

  function finishUngroupedRun() {
    if (!ungroupedRun.length) return;
    groups.push({
      key: `ungrouped:${entityKey(ungroupedRun[0])}`,
      category: null,
      channels: ungroupedRun
    });
    ungroupedRun = [];
  }

  for (const channel of topLevel) {
    if (channel.type !== 4) {
      ungroupedRun.push(channel);
      continue;
    }
    finishUngroupedRun();
    groups.push({
      key: entityKey(channel),
      category: channel,
      channels: ordered.filter((candidate) => candidate.type !== 4 && belongsTo(candidate, channel))
    });
  }
  finishUngroupedRun();

  // Keep an empty top-level drop target available when every channel belongs to a category.
  if (!groups.some((group) => group.category === null)) {
    groups.push({ key: 'ungrouped:empty', category: null, channels: [] });
  }
  return groups;
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

export function channelPositionRequest(
  previous: Channel[],
  next: Channel[],
  movedKey: string
): ChannelPositionRequest[] {
  const previousByKey = new Map(previous.map((channel) => [entityKey(channel), channel]));
  const moved = previousByKey.get(movedKey);
  if (!moved) return [];
  const movedKeys = new Set(
    moved.type === 4
      ? previous
          .filter((channel) => entityKey(channel) === movedKey || belongsTo(channel, moved))
          .map(entityKey)
      : [movedKey]
  );
  return next.flatMap((channel) => {
    if (!movedKeys.has(entityKey(channel))) return [];
    const before = previousByKey.get(entityKey(channel));
    const parentChanged =
      !before ||
      channel.parent_id !== before.parent_id ||
      channel.parent_domain !== before.parent_domain;
    if (before && channel.position === before.position && !parentChanged) return [];
    const request: ChannelPositionRequest = {
      id: channel.id,
      position: channel.position
    };
    if (parentChanged) {
      request.parent_id = channel.parent_id;
    }
    return [request];
  });
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
    if (target) {
      const targetCategory =
        target.type === 4
          ? target
          : target.parent_id && target.parent_domain
            ? remaining.find(
                (candidate) =>
                  candidate.type === 4 &&
                  candidate.id === target.parent_id &&
                  candidate.origin_domain === target.parent_domain
              )
            : null;
      const targetAnchor = targetCategory ?? target;
      const targetBlock =
        targetAnchor.type === 4 ? categoryBlock(remaining, targetAnchor) : [targetAnchor];
      const targetStart = remaining.findIndex(
        (channel) => entityKey(channel) === entityKey(targetAnchor)
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
