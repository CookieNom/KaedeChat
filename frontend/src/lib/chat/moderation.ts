import { Permission } from '$lib/generated/permissions';
import { entityKey } from './refs';
import type { Guild, GuildMemberSummary, Role, UserSummary } from './types';

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

function compareRoleRank(left: Role, right: Role): number {
  if (left.position !== right.position) return left.position - right.position;
  try {
    const leftId = BigInt(left.id);
    const rightId = BigInt(right.id);
    return leftId === rightId ? 0 : leftId < rightId ? 1 : -1;
  } catch {
    return right.id.localeCompare(left.id);
  }
}

function highestRole(guild: Guild, member: GuildMemberSummary | undefined): Role | null {
  if (!member) return null;
  const roles = guild.roles ?? [];
  if (member.role_ids.some((roleId) => !roles.some((role) => role.id === roleId))) return null;
  return (
    roles
      .filter((role) => role.id === guild.id || member.role_ids.includes(role.id))
      .sort(compareRoleRank)
      .at(-1) ?? null
  );
}

/** Mirrors the authority rule: guild owners outrank everyone; otherwise the
 * actor's highest role must be strictly above the target's highest role. */
export function guildMemberOutranks(
  guild: Guild | null,
  currentUser: UserSummary | null,
  target: UserSummary,
  members: readonly GuildMemberSummary[]
): boolean {
  if (!guild || !currentUser || entityKey(target) === entityKey(currentUser)) return false;
  const ownerKey = `${guild.owner_id}@${guild.owner_domain ?? guild.origin_domain}`;
  if (entityKey(target) === ownerKey) return false;

  const targetMember = members.find((member) => entityKey(member.user) === entityKey(target));
  if (!targetMember) return false;
  if (entityKey(currentUser) === ownerKey) return true;
  const actorMember = members.find((member) => entityKey(member.user) === entityKey(currentUser));
  const actorHighest =
    guild.roles?.find((role) => role.id === guild.actor_highest_role_id) ??
    highestRole(guild, actorMember);
  const targetHighest = highestRole(guild, targetMember);
  return Boolean(actorHighest && targetHighest && compareRoleRank(actorHighest, targetHighest) > 0);
}

/** Channel overwrite targets use role hierarchy even when MANAGE_ROLES comes
 * from the channel itself rather than the guild-wide projection. */
export function guildRoleOutranks(
  guild: Guild | null,
  currentUser: UserSummary | null,
  target: Role,
  members: readonly GuildMemberSummary[]
): boolean {
  if (!guild || !currentUser) return false;
  const resolvedTarget = guild.roles?.find((role) => entityKey(role) === entityKey(target));
  if (!resolvedTarget) return false;

  const ownerKey = `${guild.owner_id}@${guild.owner_domain ?? guild.origin_domain}`;
  if (entityKey(currentUser) === ownerKey) return true;
  const actorMember = members.find((member) => entityKey(member.user) === entityKey(currentUser));
  const actorHighest =
    guild.roles?.find((role) => role.id === guild.actor_highest_role_id) ??
    highestRole(guild, actorMember);
  return Boolean(actorHighest && compareRoleRank(actorHighest, resolvedTarget) > 0);
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
  if (!guildMemberOutranks(guild, currentUser, target, members)) return [];

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
