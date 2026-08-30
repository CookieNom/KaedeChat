interface MentionableUser {
  id: string;
  origin_domain: string;
  username: string;
  handle: string;
}

const mentionTokenCharacter = /[a-z0-9_.@-]/i;

function hasTokenEndBoundary(content: string, offset: number): boolean {
  const after = content[offset];
  if (after === undefined || !mentionTokenCharacter.test(after)) return true;
  // A final period is normally sentence punctuation; a period followed by a
  // token character is still part of a longer username or federated handle.
  return after === '.' && !mentionTokenCharacter.test(content[offset + 1] ?? '');
}

function containsMentionToken(content: string, token: string): boolean {
  const haystack = content.toLowerCase();
  const needle = token.toLowerCase();
  let offset = haystack.indexOf(needle);
  while (offset >= 0) {
    const before = haystack[offset - 1];
    if (
      (before === undefined || !mentionTokenCharacter.test(before)) &&
      hasTokenEndBoundary(haystack, offset + needle.length)
    ) {
      return true;
    }
    offset = haystack.indexOf(needle, offset + 1);
  }
  return false;
}

/** Match an explicit @username or federated @handle without prefix collisions. */
export function mentionsUser(
  content: string,
  user: MentionableUser,
  localDomain?: string
): boolean {
  return (
    content.includes(`<@${user.id}@${user.origin_domain}>`) ||
    (user.origin_domain === localDomain && content.includes(`<@${user.id}>`)) ||
    containsMentionToken(content, `@${user.handle}`) ||
    containsMentionToken(content, `@${user.username}`)
  );
}

/** Expand authenticated E2EE user/role/everyone intent against the current guild roster. */
export function expandedEncryptedGuildMentionRecipients(
  intent: RichMessageMentionIntent,
  members: readonly GuildMemberSummary[],
  roles: readonly Role[],
  repliedUserRef: string | null = null,
  canMentionEveryone = false
): string[] {
  const recipients = new Set(intent.userRefs);
  const memberRef = (member: GuildMemberSummary) =>
    `${member.user.id}@${member.user.origin_domain}`;
  if (intent.everyone && canMentionEveryone)
    members.forEach((member) => recipients.add(memberRef(member)));

  const roleByRef = new Map(roles.map((role) => [`${role.id}@${role.origin_domain}`, role]));
  for (const roleRef of intent.roleRefs) {
    const role = roleByRef.get(roleRef);
    if (!role || (!role.mentionable && !canMentionEveryone)) continue;
    const everyoneRole = role.id === role.guild_id && role.origin_domain === role.guild_domain;
    for (const member of members) {
      if (
        everyoneRole ||
        member.role_ids.includes(role.id) ||
        member.role_ids.includes(`${role.id}@${role.origin_domain}`)
      ) {
        recipients.add(memberRef(member));
      }
    }
  }
  if (repliedUserRef) recipients.add(repliedUserRef);
  return [...recipients].sort();
}
import type { GuildMemberSummary, Role } from './types';
import type { RichMessageMentionIntent } from '$lib/e2ee/client';
