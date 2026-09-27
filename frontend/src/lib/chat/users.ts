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

/** Trusted application identity from the API, never from display text. */
export function isApplicationUser(user: UserSummary | null | undefined): boolean {
  return user?.account_type === 'bot' || user?.bot === true;
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

/** Only the local server can issue this account type; federation cannot admit it. */
export function isSystemUser(user: UserSummary | null | undefined): boolean {
  return user?.account_type === 'system';
}

/** Account IDs encode creation time, unlike a remote profile's local cache timestamp. */
export function accountCreatedAt(user: UserSummary): Date | null {
  if (user.profile_resolved === false || !/^[1-9]\d{0,18}$/.test(user.id)) return null;
  const id = BigInt(user.id);
  if (id > 9_223_372_036_854_775_807n) return null;
  // Kaede's epoch and 10 worker + 12 sequence bits (backend/app/core/snowflake.py).
  return new Date(1_767_225_600_000 + Number(id >> 22n));
}
