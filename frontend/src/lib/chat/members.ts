import type { GuildMemberSummary, PresenceStatus } from './types';

export interface GuildMemberGroups {
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
  presenceFor: (member: GuildMemberSummary) => PresenceStatus
): GuildMemberGroups {
  const sorted = [...members].sort((left, right) => {
    const presenceDifference = presenceOrder[presenceFor(left)] - presenceOrder[presenceFor(right)];
    if (presenceDifference) return presenceDifference;
    return memberCollator.compare(memberDisplayName(left), memberDisplayName(right));
  });

  const firstOffline = sorted.findIndex((member) => presenceFor(member) === 'offline');
  if (firstOffline === -1) return { online: sorted, offline: [] };
  return {
    online: sorted.slice(0, firstOffline),
    offline: sorted.slice(firstOffline)
  };
}
