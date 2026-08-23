import type { GuildMemberSummary, PresenceStatus, Role } from './types';
import { userDisplayName } from './users';

export interface GuildMemberGroups {
  hoisted: Array<{ role: Role; members: GuildMemberSummary[] }>;
  online: GuildMemberSummary[];
  offline: GuildMemberSummary[];
}

const presenceOrder: Record<PresenceStatus, number> = {
  online: 0,
  idle: 1,
  dnd: 2,
  offline: 3
};
const memberCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

export function memberDisplayName(member: GuildMemberSummary): string {
  return member.nickname ?? userDisplayName(member.user);
}

export function compareRoleRank(left: Role, right: Role): number {
  const position = left.position - right.position;
  if (position) return position;
  const leftId = BigInt(left.id);
  const rightId = BigInt(right.id);
  return leftId === rightId ? 0 : leftId < rightId ? 1 : -1;
}

export function highestColoredRole(
  member: GuildMemberSummary | undefined,
  roles: Role[] = []
): Role | undefined {
  if (!member) return undefined;
  return roles
    .filter((role) => role.color !== 0 && member.role_ids.includes(role.id))
    .sort(compareRoleRank)
    .at(-1);
}

export function roleColorCss(color: number): string | undefined {
  if (color === 0) return undefined;
  return `#${(color & 0x00ff_ffff).toString(16).padStart(6, '0')}`;
}

export function memberRoleColor(
  member: GuildMemberSummary | undefined,
  roles: Role[] = []
): string | undefined {
  const role = highestColoredRole(member, roles);
  return role ? roleColorCss(role.color) : undefined;
}

export function groupGuildMembers(
  members: GuildMemberSummary[],
  presenceFor: (member: GuildMemberSummary) => PresenceStatus,
  roles: Role[] = []
): GuildMemberGroups {
  const sorted = [...members].sort((left, right) => {
    const presenceDifference = presenceOrder[presenceFor(left)] - presenceOrder[presenceFor(right)];
    if (presenceDifference) return presenceDifference;
    return memberCollator.compare(memberDisplayName(left), memberDisplayName(right));
  });

  const onlineMembers = sorted.filter((member) => presenceFor(member) !== 'offline');
  const offline = sorted.filter((member) => presenceFor(member) === 'offline');
  const hoistedRoles = roles
    .filter((role) => role.hoist && role.position > 0)
    .sort((left, right) => compareRoleRank(right, left));
  const hoisted = hoistedRoles
    .map((role) => ({
      role,
      members: onlineMembers.filter((member) => {
        if (!member.role_ids.includes(role.id)) return false;
        const highest = hoistedRoles.find((candidate) => member.role_ids.includes(candidate.id));
        return highest?.id === role.id && highest.origin_domain === role.origin_domain;
      })
    }))
    .filter((group) => group.members.length > 0);
  const hoistedMemberKeys = new Set(
    hoisted.flatMap((group) =>
      group.members.map((member) => `${member.user.id}@${member.user.origin_domain}`)
    )
  );
  return {
    hoisted,
    online: onlineMembers.filter(
      (member) => !hoistedMemberKeys.has(`${member.user.id}@${member.user.origin_domain}`)
    ),
    offline
  };
}
