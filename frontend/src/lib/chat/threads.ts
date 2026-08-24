import { api } from '$lib/api/client';
import { compareEntityRefs, entityKey, entityRef } from './refs';
import type { Channel, ForumTag, Message, ThreadMember, UserSummary } from './types';

export const THREAD_TYPES = [10, 11, 12] as const;
export const FORUM_TYPE = 15 as const;
export const FORUM_POST_CONTENT_MAX_LENGTH = 2000;

export type ForumSortOrder = 'recent_activity' | 'creation_date';
export type ForumLayout = 'list' | 'gallery';

export interface ThreadPage {
  threads: Channel[];
  members: ThreadMember[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface CreatedThread {
  channel: Channel;
  starter_message: Message | null;
}

export interface CreateThreadRequest {
  name: string;
  content?: string;
  type?: 10 | 11 | 12;
  invitable?: boolean;
  appliedTagIds?: string[];
  attachmentIds?: string[];
  autoArchiveDuration?: number;
  clientNonce?: string;
}

export interface ForumFilters {
  query: string;
  selectedTagIds: ReadonlySet<string>;
  sort: ForumSortOrder;
}

export interface ThreadMembersUpdate {
  removed_member_ids?: string[];
  removed_member_refs?: Array<{ id: string; origin_domain: string }>;
}

export interface NativeThreadCommand {
  name: string;
  message: string;
}

export function forumDefaultReactionPayload(
  emojiName: string,
  emojiId: string | null | undefined
): NonNullable<Channel['default_reaction_emoji']> | null {
  const id = emojiId?.trim();
  if (id) return { emoji_id: id };
  const name = emojiName.trim();
  return name ? { emoji_name: name } : null;
}

export function isThreadChannel(channel: Pick<Channel, 'type'> | null | undefined): boolean {
  return Boolean(channel && THREAD_TYPES.includes(channel.type as (typeof THREAD_TYPES)[number]));
}

function unquoteOption(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2) {
    const first = trimmed[0];
    const last = trimmed.at(-1);
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return trimmed.slice(1, -1).trim();
    }
  }
  return trimmed;
}

export function parseNativeThreadCommand(content: string): NativeThreadCommand | null {
  const trimmed = content.trim();
  if (!/^\/thread(?:\s|$)/i.test(trimmed)) return null;
  const raw = trimmed.replace(/^\/thread\b/i, '').trim();
  const markers = [...raw.matchAll(/(?:^|\s)(name|message):/gi)];
  const values = new Map<string, string>();
  for (let index = 0; index < markers.length; index += 1) {
    const marker = markers[index];
    const key = marker[1].toLocaleLowerCase();
    const start = (marker.index ?? 0) + marker[0].length;
    const end = markers[index + 1]?.index ?? raw.length;
    values.set(key, unquoteOption(raw.slice(start, end)));
  }
  const name = values.get('name') ?? '';
  const message = values.get('message') ?? '';
  return name && message ? { name, message } : null;
}

export function isForumChannel(channel: Pick<Channel, 'type'> | null | undefined): boolean {
  return channel?.type === FORUM_TYPE;
}

export function isThreadParentChannel(channel: Pick<Channel, 'type'> | null | undefined): boolean {
  return channel?.type === 0 || channel?.type === 5;
}

export function threadRequiresE2EEActivation(
  channel:
    | Pick<Channel, 'type' | 'e2ee_required' | 'encryption_mode' | 'encryption_state'>
    | null
    | undefined
): boolean {
  return Boolean(
    channel &&
    isThreadChannel(channel) &&
    channel.e2ee_required &&
    (channel.encryption_mode !== 'e2ee' || channel.encryption_state !== 'active')
  );
}

export function threadParentAllowsChildCreation(
  channel: Pick<Channel, 'type' | 'encryption_mode' | 'encryption_state'> | null | undefined
): boolean {
  return Boolean(
    channel &&
    isThreadParentChannel(channel) &&
    (channel.encryption_mode !== 'e2ee' || channel.encryption_state === 'active')
  );
}

export function ordinaryGuildChannels(channels: Channel[]): Channel[] {
  return channels.filter((channel) => !isThreadChannel(channel));
}

export function activeThreadsForParent(channels: Channel[], parent: Channel): Channel[] {
  return channels
    .filter(
      (channel) =>
        isThreadChannel(channel) &&
        !channel.archived &&
        channel.parent_id === parent.id &&
        channel.parent_domain === parent.origin_domain
    )
    .sort((left, right) => {
      const leftTime = Date.parse(left.last_message?.created_at ?? left.archive_timestamp ?? '');
      const rightTime = Date.parse(right.last_message?.created_at ?? right.archive_timestamp ?? '');
      return (
        (Number.isFinite(rightTime) ? rightTime : 0) - (Number.isFinite(leftTime) ? leftTime : 0) ||
        compareEntityRefs(left, right)
      );
    });
}

export function forumTags(channel: Pick<Channel, 'available_tags'>): ForumTag[] {
  return [...(channel.available_tags ?? [])].sort(
    (left, right) => left.name.localeCompare(right.name) || left.id.localeCompare(right.id)
  );
}

export function forumDefaultSort(channel: Pick<Channel, 'default_sort_order'>): ForumSortOrder {
  return channel.default_sort_order === 1 || channel.default_sort_order === 'creation_date'
    ? 'creation_date'
    : 'recent_activity';
}

export function forumDefaultLayout(channel: Pick<Channel, 'default_forum_layout'>): ForumLayout {
  return channel.default_forum_layout === 2 || channel.default_forum_layout === 'gallery'
    ? 'gallery'
    : 'list';
}

export function isPinnedForumPost(
  channel: Pick<Channel, 'archived' | 'flags' | 'pinned'>
): boolean {
  if (channel.archived) return false;
  if (channel.pinned !== undefined) return channel.pinned;
  const flags = Number(channel.flags ?? 0);
  return Number.isFinite(flags) && Boolean(flags & (1 << 1));
}

export function forumRequiresTag(channel: Pick<Channel, 'flags'>): boolean {
  const flags = Number(channel.flags ?? 0);
  return Number.isFinite(flags) && Boolean(flags & (1 << 4));
}

export function filterForumPosts(posts: Channel[], filters: ForumFilters): Channel[] {
  const needle = filters.query.trim().toLocaleLowerCase();
  const selected = filters.selectedTagIds;
  // The API owns pinned, creation-date, and last-activity ordering and returns
  // an opaque cursor tied to that exact keyset. Re-sorting a page here loses
  // reply activity because Discord's public Channel shape has no activity
  // timestamp, and can also invalidate ordering across page boundaries.
  return posts.filter((post) => {
    if (needle && !(post.name ?? '').toLocaleLowerCase().includes(needle)) return false;
    if (!selected.size) return true;
    const applied = new Set(post.applied_tag_ids ?? []);
    return [...selected].some((tag) => applied.has(tag));
  });
}

export function threadMembersUpdateRemovesUser(
  update: ThreadMembersUpdate,
  user: Pick<UserSummary, 'id' | 'origin_domain'> | null | undefined
): boolean {
  if (!user) return false;
  const refs = Array.isArray(update.removed_member_refs) ? update.removed_member_refs : [];
  if (refs.length) {
    return refs.some((item) => item.id === user.id && item.origin_domain === user.origin_domain);
  }
  return Array.isArray(update.removed_member_ids) && update.removed_member_ids.includes(user.id);
}

export function parseThreadPage(payload: unknown): ThreadPage {
  if (Array.isArray(payload)) {
    return { threads: payload as Channel[], members: [], has_more: false, next_cursor: null };
  }
  if (!payload || typeof payload !== 'object') {
    return { threads: [], members: [], has_more: false, next_cursor: null };
  }
  const value = payload as Record<string, unknown>;
  return {
    threads: Array.isArray(value.threads) ? (value.threads as Channel[]) : [],
    members: Array.isArray(value.members) ? (value.members as ThreadMember[]) : [],
    has_more: value.has_more === true,
    next_cursor: typeof value.next_cursor === 'string' ? value.next_cursor : null
  };
}

export function parseCreatedThread(payload: unknown): CreatedThread {
  if (!payload || typeof payload !== 'object') throw new Error('The thread response is invalid.');
  const value = payload as Record<string, unknown>;
  const channel = (value.channel ?? value.thread ?? value) as Channel;
  if (!channel.id || !channel.origin_domain || !isThreadChannel(channel)) {
    throw new Error('The thread response is invalid.');
  }
  const starter = value.starter_message;
  return {
    channel,
    starter_message: starter && typeof starter === 'object' ? (starter as Message) : null
  };
}

export async function fetchChannel(channelRef: string): Promise<Channel> {
  return api<Channel>(`/channels/${encodeURIComponent(channelRef)}`);
}

export async function fetchThreads(
  parent: Channel,
  options: {
    archived?: boolean;
    includeArchived?: boolean;
    query?: string;
    tagIds?: readonly string[];
    sort?: ForumSortOrder;
    cursor?: string;
    /** Legacy timestamp cursor accepted by the server for older clients. */
    before?: string;
    limit?: number;
  } = {}
) {
  const query = new URLSearchParams();
  if (options.includeArchived) query.set('include_archived', 'true');
  else if (options.archived !== undefined) query.set('archived', String(options.archived));
  if (options.query?.trim()) query.set('query', options.query.trim());
  for (const tagId of options.tagIds ?? []) query.append('tag_id', tagId);
  if (options.sort) query.set('sort_order', options.sort === 'creation_date' ? '1' : '0');
  if (options.cursor) query.set('cursor', options.cursor);
  if (options.before) query.set('before', options.before);
  if (options.limit) query.set('limit', String(options.limit));
  const suffix = query.size ? `?${query.toString()}` : '';
  return parseThreadPage(
    await api<unknown>(`/channels/${encodeURIComponent(entityRef(parent))}/threads${suffix}`)
  );
}

export async function fetchActiveGuildThreads(guildRef: string): Promise<ThreadPage> {
  return parseThreadPage(
    await api<unknown>(`/guilds/${encodeURIComponent(guildRef)}/threads/active`)
  );
}

export async function createThread(
  parent: Channel,
  request: CreateThreadRequest
): Promise<CreatedThread> {
  const content = request.content?.trim() ?? '';
  return parseCreatedThread(
    await api<unknown>(`/channels/${encodeURIComponent(entityRef(parent))}/threads`, {
      method: 'POST',
      body: JSON.stringify({
        name: request.name.trim(),
        ...(request.type ? { type: request.type } : {}),
        ...(request.type === 12 && request.invitable !== undefined
          ? { invitable: request.invitable }
          : {}),
        applied_tag_ids: request.appliedTagIds ?? [],
        auto_archive_duration:
          request.autoArchiveDuration ?? parent.default_auto_archive_duration ?? 1440,
        ...(content || request.attachmentIds?.length
          ? {
              message: {
                content: content || undefined,
                attachment_ids: request.attachmentIds ?? [],
                client_nonce: request.clientNonce ?? crypto.randomUUID()
              }
            }
          : {})
      })
    })
  );
}

export async function createThreadFromMessage(
  parent: Channel,
  message: Message,
  name: string,
  options: { autoArchiveDuration?: number; rateLimitPerUser?: number } = {}
): Promise<CreatedThread> {
  return parseCreatedThread(
    await api<unknown>(
      `/channels/${encodeURIComponent(entityRef(parent))}/messages/${encodeURIComponent(entityRef(message))}/threads`,
      {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(),
          auto_archive_duration:
            options.autoArchiveDuration ?? parent.default_auto_archive_duration ?? 1440,
          ...(options.rateLimitPerUser !== undefined
            ? { rate_limit_per_user: options.rateLimitPerUser }
            : {})
        })
      }
    )
  );
}

export async function updateThread(
  thread: Channel,
  patch: Partial<
    Pick<
      Channel,
      | 'name'
      | 'archived'
      | 'locked'
      | 'invitable'
      | 'auto_archive_duration'
      | 'rate_limit_per_user'
      | 'applied_tag_ids'
      | 'pinned'
    >
  >
): Promise<Channel> {
  return api<Channel>(`/channels/${encodeURIComponent(entityRef(thread))}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
    headers: thread.version ? { 'If-Match': thread.version } : undefined
  });
}

export async function setThreadMembership(
  thread: Channel,
  joined: boolean,
  notificationLevel: ThreadMember['notification_level'] = 'inherit'
): Promise<void> {
  await api(`/channels/${encodeURIComponent(entityRef(thread))}/thread-members/@me`, {
    method: joined ? 'PUT' : 'DELETE',
    ...(joined
      ? {
          body: JSON.stringify({ flags: 0, notification_level: notificationLevel })
        }
      : {})
  });
}

export async function fetchThreadMembers(thread: Channel): Promise<ThreadMember[]> {
  const payload = await api<unknown>(
    `/channels/${encodeURIComponent(entityRef(thread))}/thread-members`
  );
  if (Array.isArray(payload)) return payload as ThreadMember[];
  if (payload && typeof payload === 'object') {
    const members = (payload as Record<string, unknown>).members;
    if (Array.isArray(members)) return members as ThreadMember[];
  }
  return [];
}

export async function setThreadMember(
  thread: Channel,
  userRef: string,
  joined: boolean
): Promise<void> {
  await api(
    `/channels/${encodeURIComponent(entityRef(thread))}/thread-members/${encodeURIComponent(userRef)}`,
    { method: joined ? 'PUT' : 'DELETE' }
  );
}

export function mergeThreadIntoChannels(channels: Channel[], thread: Channel): Channel[] {
  const key = entityKey(thread);
  const existing = channels.some((channel) => entityKey(channel) === key);
  return existing
    ? channels.map((channel) => (entityKey(channel) === key ? { ...channel, ...thread } : channel))
    : [...channels, thread];
}
