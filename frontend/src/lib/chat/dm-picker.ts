import { entityKey } from './refs';
import type { Channel, Relationship, UserSummary } from './types';

function searchableUserText(user: UserSummary): string {
  return [user.display_name, user.username, user.handle, user.origin_domain]
    .filter((value): value is string => Boolean(value))
    .join('\n')
    .toLocaleLowerCase();
}

export function friendsWithoutVisibleDm(
  relationships: Relationship[],
  directMessages: Channel[]
): UserSummary[] {
  const visibleRecipients = new Set(
    directMessages.flatMap((channel) => channel.recipients ?? []).map(entityKey)
  );

  return relationships
    .filter(
      (relationship) =>
        relationship.type === 'friend' && !visibleRecipients.has(entityKey(relationship.user))
    )
    .map((relationship) => relationship.user)
    .sort((left, right) => {
      const leftName = left.display_name?.trim() || left.username;
      const rightName = right.display_name?.trim() || right.username;
      return leftName.localeCompare(rightName, undefined, { sensitivity: 'base' });
    });
}

export function filterDmFriends(friends: UserSummary[], query: string): UserSummary[] {
  const normalized = query.trim().replace(/^@/, '').toLocaleLowerCase();
  if (!normalized) return friends;
  return friends.filter((friend) => searchableUserText(friend).includes(normalized));
}
