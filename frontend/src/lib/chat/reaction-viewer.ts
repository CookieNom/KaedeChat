import { entityKey } from './refs';
import type { UserSummary } from './types';

/** Merge paginated reactors while preserving federated composite identity. */
export function mergeReactionUsers(current: UserSummary[], incoming: UserSummary[]): UserSummary[] {
  const users = new Map(current.map((user) => [entityKey(user), user]));
  for (const user of incoming) users.set(entityKey(user), user);
  return [...users.values()];
}
