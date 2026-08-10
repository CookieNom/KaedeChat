import type { Guild, ReadStateStatus } from '$lib/chat/types';

export interface ChannelUnreadPresentation {
  unread: boolean;
  mentionCount: number;
  showUnreadDot: boolean;
}

export function channelUnreadPresentation(
  state: ReadStateStatus | undefined
): ChannelUnreadPresentation {
  const unread = state?.unread === true;
  const mentionCount = unread ? Math.max(0, state?.mention_count ?? 0) : 0;
  return {
    unread,
    mentionCount,
    showUnreadDot: unread && mentionCount === 0
  };
}

export function guildMentionCount(readStates: ReadStateStatus[], guild: Guild): number {
  return readStates
    .filter(
      (state) =>
        state.guild_id === guild.id && state.guild_domain === guild.origin_domain && state.unread
    )
    .reduce((total, state) => total + state.mention_count, 0);
}

export function directMessageUnreadCount(readStates: ReadStateStatus[]): number {
  return readStates
    .filter((state) => state.guild_id === null && state.unread)
    .reduce((total, state) => total + Math.max(1, state.mention_count), 0);
}

export function compactBadgeCount(count: number): string {
  return count > 99 ? '99+' : String(Math.max(0, count));
}
