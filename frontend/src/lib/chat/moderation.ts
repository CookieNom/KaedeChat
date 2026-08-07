import { Permission } from '$lib/generated/permissions';
import { entityKey } from './refs';
import type { Guild, GuildMemberSummary, UserSummary } from './types';

export type GuildModerationAction = {
  id: 'kick' | 'timeout' | 'ban';
  label: string;
};

function hasPermission(guild: Guild, permission: bigint): boolean {
  const effective = BigInt(guild.permissions ?? '0');
  return (
    (effective & Permission.ADMINISTRATOR) === Permission.ADMINISTRATOR ||
    (effective & permission) === permission
  );
}

export function guildModerationActions(
  guild: Guild | null,
  currentUser: UserSummary | null,
  target: UserSummary,
  members: readonly GuildMemberSummary[]
): GuildModerationAction[] {
  if (!guild || !currentUser) return [];
  if (entityKey(target) === entityKey(currentUser)) return [];
  if (
    target.id === guild.owner_id &&
    target.origin_domain === (guild.owner_domain ?? guild.origin_domain)
  ) {
    return [];
  }
  if (!members.some((member) => entityKey(member.user) === entityKey(target))) return [];

  const actions: GuildModerationAction[] = [];
  if (hasPermission(guild, Permission.MODERATE_MEMBERS)) {
    actions.push({ id: 'timeout', label: 'Timeout' });
  }
  if (hasPermission(guild, Permission.KICK_MEMBERS)) {
    actions.push({ id: 'kick', label: 'Kick' });
  }
  if (hasPermission(guild, Permission.BAN_MEMBERS)) {
    actions.push({ id: 'ban', label: 'Ban' });
  }
  return actions;
}
