import type { Channel, UserSummary } from './types';
import { userDisplayName } from './users';

export function isGroupDm(channel: Channel | null | undefined): boolean {
  return channel?.conversation_type === 'group';
}

export function promoteDirectMessage(channels: Channel[], active: Channel): Channel[] {
  return [
    active,
    ...channels.filter(
      (channel) => channel.id !== active.id || channel.origin_domain !== active.origin_domain
    )
  ];
}

export function dmTitle(channel: Channel | null | undefined): string {
  if (!channel) return 'Conversation';
  if (!isGroupDm(channel)) {
    return channel.recipients?.[0] ? userDisplayName(channel.recipients[0]) : 'Conversation';
  }
  if (channel.name?.trim()) return channel.name.trim();
  const names = (channel.recipients ?? []).map(userDisplayName);
  if (!names.length) return 'Group conversation';
  return names.slice(0, 3).join(', ') + (names.length > 3 ? ` +${names.length - 3}` : '');
}

export function groupDmSubtitle(channel: Channel): string {
  const count = (channel.recipients?.length ?? 0) + 1;
  return `${count} ${count === 1 ? 'member' : 'members'}`;
}

export function ownsGroupDm(channel: Channel, currentUser: UserSummary | null): boolean {
  return Boolean(
    currentUser &&
    channel.owner_id === currentUser.id &&
    channel.owner_domain === currentUser.origin_domain
  );
}
