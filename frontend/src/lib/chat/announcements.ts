import { api } from '$lib/api/client';
import { Permission } from '$lib/generated/permissions';
import { entityRef, parseCanonicalEntityRef } from './refs';
import type { Channel, Guild, Message, UserSummary } from './types';

export const ANNOUNCEMENT_CHANNEL_TYPE = 5;
export const MESSAGE_FLAG_CROSSPOSTED = 1;
export const MESSAGE_FLAG_IS_CROSSPOST = 2;

export interface AnnouncementFollow {
  id: string;
  ref: string;
  source_channel_id: string;
  source_channel_domain: string;
  target_channel_id: string;
  target_channel_domain: string;
  creator_id: string;
  creator_domain: string;
  active: boolean;
  federated: boolean;
  generation: string | null;
  lifecycle_state: 'pending' | 'accepted' | 'active' | 'revoked';
  name: string | null;
  avatar_hash: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnnouncementTarget {
  guild: Guild;
  channel: Channel;
  ref: string;
  label: string;
}

function hasEffectivePermission(channel: Channel, guild: Guild, permission: bigint): boolean {
  try {
    const effective = BigInt(channel.permissions ?? guild.permissions ?? '0');
    return Boolean(effective & (Permission.ADMINISTRATOR | permission));
  } catch {
    return false;
  }
}

export function canReadAnnouncementChannel(channel: Channel, guild: Guild): boolean {
  return (
    channel.type === ANNOUNCEMENT_CHANNEL_TYPE &&
    hasEffectivePermission(channel, guild, Permission.VIEW_CHANNEL)
  );
}

export function canManageAnnouncementTarget(channel: Channel, guild: Guild): boolean {
  return channel.type === 0 && hasEffectivePermission(channel, guild, Permission.MANAGE_WEBHOOKS);
}

export function announcementTargets(guilds: Guild[]): AnnouncementTarget[] {
  const targets = new Map<string, AnnouncementTarget>();
  for (const guild of guilds) {
    for (const channel of guild.channels ?? []) {
      if (
        !canManageAnnouncementTarget(channel, guild) ||
        channel.encryption_mode === 'e2ee' ||
        channel.e2ee_required
      )
        continue;
      const ref = entityRef(channel);
      targets.set(ref, {
        guild,
        channel,
        ref,
        label: `${guild.name} · #${channel.name ?? 'channel'}`
      });
    }
  }
  return [...targets.values()].sort(
    (left, right) =>
      left.guild.name.localeCompare(right.guild.name) ||
      (left.channel.position ?? 0) - (right.channel.position ?? 0) ||
      left.label.localeCompare(right.label)
  );
}

export function canDeleteAnnouncementFollow(follow: AnnouncementFollow, guilds: Guild[]): boolean {
  return guilds.some((guild) =>
    (guild.channels ?? []).some(
      (channel) =>
        channel.id === follow.target_channel_id &&
        channel.origin_domain === follow.target_channel_domain &&
        canManageAnnouncementTarget(channel, guild)
    )
  );
}

export function isPublishedAnnouncement(message: Pick<Message, 'flags'>): boolean {
  return Boolean(Number(message.flags ?? 0) & MESSAGE_FLAG_CROSSPOSTED);
}

function followedChannelLabel(message: Message, channels: Iterable<Channel>): string {
  const reference = message.message_reference;
  const channelId = reference?.channel_id?.trim();
  const channelDomain = reference?.channel_domain?.trim();
  const resolved =
    channelId && channelDomain
      ? [...channels].find(
          (channel) => channel.id === channelId && channel.origin_domain === channelDomain
        )
      : null;
  const name = resolved?.name?.trim() || message.content?.trim();
  if (name) return name.startsWith('#') ? name : `#${name}`;
  if (channelId && channelDomain) return `#${channelId}@${channelDomain}`;
  if (channelId) return `#${channelId}`;
  return 'an announcement channel';
}

/** Discord type-12 text, resolving a known channel without losing a federated fallback. */
export function channelFollowSystemMessageText(
  message: Message,
  channels: Iterable<Channel>,
  author: string
): string | null {
  if (message.message_type !== 12) return null;
  return `${author} has added ${followedChannelLabel(message, channels)} to this channel. Its most important updates will show up here.`;
}

/** Mirrors the human crosspost route; qualified refs let the API reach remote authority. */
export function canPublishAnnouncementMessage(
  channel: Channel,
  message: Message,
  currentUser: UserSummary | null,
  canSendMessages: boolean,
  canManageMessages: boolean
): boolean {
  if (
    !currentUser ||
    channel.type !== ANNOUNCEMENT_CHANNEL_TYPE ||
    message.channel_id !== channel.id ||
    message.channel_domain !== channel.origin_domain ||
    channel.encryption_mode === 'e2ee' ||
    message.e2ee ||
    message.deleted_at ||
    message.pending ||
    message.queued ||
    message.failed ||
    message.delivery_status === 'failed' ||
    message.id.startsWith('pending-') ||
    isPublishedAnnouncement(message) ||
    !canSendMessages
  ) {
    return false;
  }
  const authoredByCurrentUser =
    message.author_id === currentUser.id && message.author_domain === currentUser.origin_domain;
  return authoredByCurrentUser || canManageMessages;
}

function followerPath(sourceChannelRef: string): string {
  return `/channels/${encodeURIComponent(sourceChannelRef)}/followers`;
}

const FOLLOW_FIELDS = new Set([
  'id',
  'ref',
  'source_channel_id',
  'source_channel_domain',
  'target_channel_id',
  'target_channel_domain',
  'creator_id',
  'creator_domain',
  'active',
  'federated',
  'generation',
  'lifecycle_state',
  'name',
  'avatar_hash',
  'created_at',
  'updated_at'
]);

function validatedAnnouncementFollow(
  value: unknown,
  expectedSourceRef: string,
  expectedTargetRef?: string
): AnnouncementFollow {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Announcement follow response is invalid.');
  }
  const raw = value as Record<string, unknown>;
  if (
    Object.keys(raw).length !== FOLLOW_FIELDS.size ||
    Object.keys(raw).some((key) => !FOLLOW_FIELDS.has(key))
  ) {
    throw new Error('Announcement follow response is invalid.');
  }
  const compositeRef = (id: unknown, domain: unknown) =>
    typeof id === 'string' && typeof domain === 'string'
      ? parseCanonicalEntityRef(`${id}@${domain}`)
      : null;
  const source = compositeRef(raw.source_channel_id, raw.source_channel_domain);
  const target = compositeRef(raw.target_channel_id, raw.target_channel_domain);
  const creator = compositeRef(raw.creator_id, raw.creator_domain);
  const ref = parseCanonicalEntityRef(raw.ref);
  const expectedSource = parseCanonicalEntityRef(expectedSourceRef);
  const expectedTarget =
    expectedTargetRef === undefined ? null : parseCanonicalEntityRef(expectedTargetRef);
  const created = typeof raw.created_at === 'string' ? Date.parse(raw.created_at) : Number.NaN;
  const updated = typeof raw.updated_at === 'string' ? Date.parse(raw.updated_at) : Number.NaN;
  const timestampHasZone = (item: unknown) =>
    typeof item === 'string' && (item.endsWith('Z') || /[+-]\d{2}:\d{2}$/u.test(item));
  const generationValid =
    typeof raw.generation === 'string' &&
    /^[1-9]\d{0,18}$/u.test(raw.generation) &&
    BigInt(raw.generation) <= 9_223_372_036_854_775_807n;
  if (
    typeof raw.id !== 'string' ||
    !source ||
    !target ||
    !creator ||
    !ref ||
    !expectedSource ||
    (expectedTargetRef !== undefined && !expectedTarget) ||
    entityRef(source) !== expectedSourceRef ||
    (expectedTarget && entityRef(target) !== expectedTargetRef) ||
    ref.id !== raw.id ||
    ref.origin_domain !== target.origin_domain ||
    typeof raw.active !== 'boolean' ||
    typeof raw.federated !== 'boolean' ||
    typeof raw.lifecycle_state !== 'string' ||
    !['pending', 'accepted', 'active', 'revoked'].includes(raw.lifecycle_state) ||
    raw.active !== (raw.lifecycle_state === 'active') ||
    raw.federated !== (source.origin_domain !== target.origin_domain) ||
    (raw.federated ? !generationValid : raw.generation !== null) ||
    (raw.name !== null && typeof raw.name !== 'string') ||
    (raw.avatar_hash !== null && typeof raw.avatar_hash !== 'string') ||
    !timestampHasZone(raw.created_at) ||
    !timestampHasZone(raw.updated_at) ||
    !Number.isFinite(created) ||
    !Number.isFinite(updated) ||
    updated < created
  ) {
    throw new Error('Announcement follow response changed its requested lineage.');
  }
  return raw as unknown as AnnouncementFollow;
}

export async function listAnnouncementFollows(
  sourceChannelRef: string,
  signal?: AbortSignal
): Promise<AnnouncementFollow[]> {
  const raw = await api<unknown>(followerPath(sourceChannelRef), { signal });
  if (!Array.isArray(raw)) throw new Error('Announcement follow page is invalid.');
  const follows = raw.map((item) => validatedAnnouncementFollow(item, sourceChannelRef));
  const seen = new Set<string>();
  follows.forEach((follow, index) => {
    if (!follow.active || seen.has(follow.ref)) {
      throw new Error('Announcement follow page is invalid.');
    }
    seen.add(follow.ref);
    const previous = follows[index - 1];
    if (
      previous &&
      (BigInt(previous.id) > BigInt(follow.id) ||
        (previous.id === follow.id && previous.ref.localeCompare(follow.ref) >= 0))
    ) {
      throw new Error('Announcement follow page is unordered.');
    }
  });
  return follows;
}

export async function createAnnouncementFollow(
  sourceChannelRef: string,
  targetChannelRef: string
): Promise<AnnouncementFollow> {
  return validatedAnnouncementFollow(
    await api<unknown>(followerPath(sourceChannelRef), {
      method: 'POST',
      body: JSON.stringify({ target_channel_id: targetChannelRef })
    }),
    sourceChannelRef,
    targetChannelRef
  );
}

export function deleteAnnouncementFollow(
  sourceChannelRef: string,
  followRef: string
): Promise<void> {
  return api<void>(`${followerPath(sourceChannelRef)}/${encodeURIComponent(followRef)}`, {
    method: 'DELETE'
  });
}

export async function publishAnnouncementMessage(
  channel: Channel,
  message: Message
): Promise<Message> {
  const raw = await api<unknown>(
    `/channels/${encodeURIComponent(entityRef(channel))}/messages/${encodeURIComponent(entityRef(message))}/crosspost`,
    { method: 'POST' }
  );
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('Announcement publish response is invalid.');
  }
  const rendered = raw as Record<string, unknown>;
  if (
    rendered.id !== message.id ||
    rendered.origin_domain !== message.origin_domain ||
    rendered.channel_id !== channel.id ||
    rendered.channel_domain !== channel.origin_domain ||
    typeof rendered.flags !== 'number' ||
    !Number.isSafeInteger(rendered.flags) ||
    !(rendered.flags & MESSAGE_FLAG_CROSSPOSTED)
  ) {
    throw new Error('Announcement publish response changed its requested lineage.');
  }
  return raw as Message;
}
