import { entityKey } from './refs';
import type { Channel, Relationship, UserSummary } from './types';

export function userDisplayName(user: UserSummary | null | undefined): string {
  if (!user) return 'Unknown user';
  if (user.profile_resolved === false) return `Remote user · ${user.origin_domain}`;
  return user.display_name?.trim() || user.username;
}

export function userPublicHandle(user: UserSummary): string | null {
  return user.profile_resolved === false ? null : user.handle;
}

export function applyUserProfileToHomeProjections(
  directMessages: Channel[],
  relationships: Relationship[],
  selectedUser: UserSummary | null,
  user: UserSummary
): {
  directMessages: Channel[];
  relationships: Relationship[];
  selectedUser: UserSummary | null;
} {
  const key = entityKey(user);
  return {
    directMessages: directMessages.map((channel) => ({
      ...channel,
      recipients: channel.recipients?.map((recipient) =>
        entityKey(recipient) === key ? { ...recipient, ...user } : recipient
      )
    })),
    relationships: relationships.map((relationship) =>
      entityKey(relationship.user) === key
        ? { ...relationship, user: { ...relationship.user, ...user } }
        : relationship
    ),
    selectedUser:
      selectedUser && entityKey(selectedUser) === key ? { ...selectedUser, ...user } : selectedUser
  };
}
