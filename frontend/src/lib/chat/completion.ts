import type { Channel, GuildMemberSummary, Role } from './types';
import { entityRef } from './refs';
import { assetUrl } from '$lib/media/assets';
import { userDisplayName, userPublicHandle } from './users';

export interface CompletionOption {
  value: string;
  label: string;
  detail?: string;
  emoji?: string;
  imageUrl?: string;
  color?: string;
  kind?: 'user' | 'role' | 'channel' | 'unicode-emoji' | 'custom-emoji';
}

export interface CompletionQuery {
  marker: '@' | '#' | ':';
  query: string;
  start: number;
  end: number;
}

export function completionAt(value: string, cursor: number): CompletionQuery | null {
  const prefix = value.slice(0, cursor);
  const match = /(?:^|\s)([@#:])([\w.+-]*)(?::)?$/.exec(prefix);
  if (!match) return null;
  const marker = match[1] as '@' | '#' | ':';
  const query = match[2];
  const start = prefix.length - match[0].length + match[0].indexOf(marker);
  return { marker, query, start, end: cursor };
}

export function replaceCompletion(
  value: string,
  completion: CompletionQuery,
  replacement: string
): string {
  return `${value.slice(0, completion.start)}${replacement} ${value.slice(completion.end)}`;
}

export function memberCompletions(members: GuildMemberSummary[], query: string) {
  const needle = query.toLocaleLowerCase();
  return members
    .filter((member) => {
      const user = member.user;
      return [member.nickname, userDisplayName(user), userPublicHandle(user)].some((value) =>
        value?.toLocaleLowerCase().includes(needle)
      );
    })
    .map((member) => ({
      value: `<@${entityRef(member.user)}>`,
      label: member.nickname ?? userDisplayName(member.user),
      detail: userPublicHandle(member.user)
        ? `@${userPublicHandle(member.user)}`
        : 'Profile unavailable',
      imageUrl: member.user.avatar_hash
        ? assetUrl(member.user.avatar_hash, 'thumbnail_128', member.user)
        : undefined,
      kind: 'user' as const
    }));
}

export function roleCompletions(
  roles: Role[],
  query: string,
  options: { canMentionUnmentionable?: boolean } = {}
): CompletionOption[] {
  const needle = query.toLocaleLowerCase();
  return roles
    .filter(
      (role) =>
        (role.mentionable || options.canMentionUnmentionable) &&
        role.name.toLocaleLowerCase().includes(needle)
    )
    .sort((left, right) => right.position - left.position || left.name.localeCompare(right.name))
    .map((role) => ({
      value: `<@&${entityRef(role)}>`,
      label: `@${role.name}`,
      detail: 'Role',
      color: `#${role.color.toString(16).padStart(6, '0')}`,
      kind: 'role' as const
    }));
}

export function channelCompletions(channels: Channel[], query: string) {
  const needle = query.toLocaleLowerCase();
  return channels
    .filter((channel) => channel.name?.toLocaleLowerCase().includes(needle))
    .map((channel) => ({
      value: `#${channel.name}`,
      label: `#${channel.name}`,
      kind: 'channel' as const
    }));
}
