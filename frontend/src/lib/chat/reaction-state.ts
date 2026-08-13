import { entityKey } from './refs';
import type { Message, UserSummary } from './types';

export interface ReactionUpdate {
  id: string;
  origin_domain: string;
  reaction: string;
  removed?: boolean;
  user_id?: string;
  user_domain?: string;
}

export function applyReactionUpdate(
  message: Message,
  update: ReactionUpdate,
  currentUser: UserSummary | null
): Message {
  const counts = { ...(message.reaction_counts ?? {}) };
  const next = Math.max(0, (counts[update.reaction] ?? 0) + (update.removed ? -1 : 1));
  if (next) counts[update.reaction] = next;
  else delete counts[update.reaction];

  const reacted = new Set(message.reacted_emoji ?? []);
  const updateUser =
    update.user_id && update.user_domain
      ? entityKey({ id: update.user_id, origin_domain: update.user_domain })
      : null;
  if (currentUser && updateUser === entityKey(currentUser)) {
    if (update.removed) reacted.delete(update.reaction);
    else reacted.add(update.reaction);
  }
  return { ...message, reaction_counts: counts, reacted_emoji: [...reacted] };
}
