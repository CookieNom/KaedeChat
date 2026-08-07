import type { GuildMemberSummary, PresenceStatus, Role } from './types';

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
  return member.nickname ?? member.user.display_name ?? member.user.username;
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
    .sort((left, right) => right.position - left.position || right.id.localeCompare(left.id));
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
