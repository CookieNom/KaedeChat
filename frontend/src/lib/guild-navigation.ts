import type { FederatedIdentity } from '$lib/chat/refs';
import { entityRef } from '$lib/chat/refs';

export type GuildNavigationGuildItem = { kind: 'guild'; guild: string };
export type GuildNavigationGroupItem = {
  kind: 'group';
  id: string;
  name: string;
  guilds: string[];
  collapsed: boolean;
};
export type GuildNavigationItem = GuildNavigationGuildItem | GuildNavigationGroupItem;
export type GuildNavigation = { items: GuildNavigationItem[] };
export type GuildDropPosition = 'before' | 'inside' | 'after';

const GROUP_ID = /^[A-Za-z0-9_-]{1,36}$/;
const ENTITY_REF = /^[0-9]+@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/;

function serializedEntityRef(value: unknown): string | null {
  if (typeof value === 'string') return ENTITY_REF.test(value) ? value : null;
  if (!value || typeof value !== 'object') return null;
  const reference = value as Record<string, unknown>;
  const domain = reference.origin_domain ?? reference.domain;
  if (typeof reference.id !== 'string' || typeof domain !== 'string') return null;
  const rendered = `${reference.id}@${domain}`;
  return ENTITY_REF.test(rendered) ? rendered : null;
}

export function parseGuildNavigation(value: unknown): GuildNavigation {
  if (!value || typeof value !== 'object' || !Array.isArray((value as { items?: unknown }).items)) {
    return { items: [] };
  }
  const seenGuilds = new Set<string>();
  const seenGroups = new Set<string>();
  const items: GuildNavigationItem[] = [];
  for (const raw of (value as { items: unknown[] }).items.slice(0, 200)) {
    if (!raw || typeof raw !== 'object') continue;
    const item = raw as Record<string, unknown>;
    const guildReference = serializedEntityRef(item.guild);
    if (item.kind === 'guild' && guildReference) {
      if (!seenGuilds.has(guildReference)) {
        seenGuilds.add(guildReference);
        items.push({ kind: 'guild', guild: guildReference });
      }
      continue;
    }
    if (
      item.kind !== 'group' ||
      typeof item.id !== 'string' ||
      !GROUP_ID.test(item.id) ||
      seenGroups.has(item.id) ||
      typeof item.name !== 'string' ||
      !item.name.trim() ||
      !Array.isArray(item.guilds)
    ) {
      continue;
    }
    const guilds = item.guilds
      .map(serializedEntityRef)
      .filter((guild): guild is string => guild !== null)
      .filter((guild) => {
        if (seenGuilds.has(guild)) return false;
        seenGuilds.add(guild);
        return true;
      })
      .slice(0, 100);
    if (!guilds.length) continue;
    seenGroups.add(item.id);
    items.push({
      kind: 'group',
      id: item.id,
      name: item.name.trim().slice(0, 32),
      guilds,
      collapsed: item.collapsed === true
    });
  }
  return { items };
}

export function reconcileGuildNavigation(
  navigation: GuildNavigation,
  guilds: readonly FederatedIdentity[]
): GuildNavigation {
  const accessible = new Set(guilds.map(entityRef));
  const seen = new Set<string>();
  const items: GuildNavigationItem[] = [];
  for (const item of navigation.items) {
    if (item.kind === 'guild') {
      if (accessible.has(item.guild) && !seen.has(item.guild)) {
        seen.add(item.guild);
        items.push(item);
      }
      continue;
    }
    const groupGuilds = item.guilds.filter((guild) => {
      if (!accessible.has(guild) || seen.has(guild)) return false;
      seen.add(guild);
      return true;
    });
    if (groupGuilds.length) items.push({ ...item, guilds: groupGuilds });
  }
  for (const guild of guilds) {
    const ref = entityRef(guild);
    if (!seen.has(ref)) items.push({ kind: 'guild', guild: ref });
  }
  return { items };
}

function withoutGuild(items: GuildNavigationItem[], guild: string): GuildNavigationItem[] {
  const remaining: GuildNavigationItem[] = [];
  for (const item of items) {
    if (item.kind === 'guild') {
      if (item.guild !== guild) remaining.push(item);
      continue;
    }
    const guilds = item.guilds.filter((candidate) => candidate !== guild);
    if (guilds.length) remaining.push({ ...item, guilds });
  }
  return remaining;
}

export function placeGuild(
  navigation: GuildNavigation,
  source: string,
  target: string,
  position: GuildDropPosition,
  newGroupId: string
): GuildNavigation {
  if (source === target) return navigation;
  let items = withoutGuild(navigation.items, source);
  const groupIndex = items.findIndex(
    (item) => item.kind === 'group' && item.guilds.includes(target)
  );
  if (groupIndex >= 0) {
    const group = items[groupIndex] as GuildNavigationGroupItem;
    const targetIndex = group.guilds.indexOf(target);
    const insertAt = position === 'before' ? targetIndex : targetIndex + 1;
    const guilds = [...group.guilds];
    guilds.splice(insertAt, 0, source);
    items = items.map((item, index) => (index === groupIndex ? { ...group, guilds } : item));
    return { items };
  }
  const targetIndex = items.findIndex((item) => item.kind === 'guild' && item.guild === target);
  if (targetIndex < 0) return { items: [...items, { kind: 'guild', guild: source }] };
  if (position === 'inside') {
    items.splice(targetIndex, 1, {
      kind: 'group',
      id: newGroupId,
      name: 'Guild group',
      guilds: [target, source],
      collapsed: false
    });
  } else {
    items.splice(targetIndex + (position === 'after' ? 1 : 0), 0, {
      kind: 'guild',
      guild: source
    });
  }
  return { items };
}

export function placeGuildInGroup(
  navigation: GuildNavigation,
  guild: string,
  groupId: string
): GuildNavigation {
  const items = withoutGuild(navigation.items, guild).map((item) =>
    item.kind === 'group' && item.id === groupId
      ? { ...item, guilds: [...item.guilds, guild], collapsed: false }
      : item
  );
  return { items };
}

export function updateGuildGroup(
  navigation: GuildNavigation,
  groupId: string,
  update: { name?: string; collapsed?: boolean }
): GuildNavigation {
  return {
    items: navigation.items.map((item) =>
      item.kind === 'group' && item.id === groupId ? { ...item, ...update } : item
    )
  };
}

export function ungroupGuilds(navigation: GuildNavigation, groupId: string): GuildNavigation {
  return {
    items: navigation.items.flatMap((item) =>
      item.kind === 'group' && item.id === groupId
        ? item.guilds.map((guild) => ({ kind: 'guild' as const, guild }))
        : [item]
    )
  };
}

export function moveGuildGroup(
  navigation: GuildNavigation,
  groupId: string,
  targetGuildOrGroup: string,
  after: boolean
): GuildNavigation {
  const sourceIndex = navigation.items.findIndex(
    (item) => item.kind === 'group' && item.id === groupId
  );
  const targetIndex = navigation.items.findIndex((item) =>
    item.kind === 'group'
      ? item.id === targetGuildOrGroup || item.guilds.includes(targetGuildOrGroup)
      : item.guild === targetGuildOrGroup
  );
  if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) return navigation;
  const items = [...navigation.items];
  const [source] = items.splice(sourceIndex, 1);
  let destination = targetIndex;
  if (sourceIndex < targetIndex) destination -= 1;
  if (after) destination += 1;
  items.splice(destination, 0, source);
  return { items };
}
