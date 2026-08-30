import { entityKey } from './refs';
import { canonicalReactionEmoji } from './reactions';
import type { Message, UserSummary } from './types';

export type ReactionDispatchName =
  'MESSAGE_REACTION_ADD' | 'MESSAGE_REACTION_REMOVE' | 'MESSAGE_UPDATE';

export interface ReactionUpdate {
  id: string;
  origin_domain: string;
  channel_id?: string;
  channel_domain?: string;
  reaction: string;
  removed?: boolean;
  user_id?: string;
  user_domain?: string;
  emoji?: {
    id?: string | null;
    origin_domain?: string | null;
    name?: string | null;
    animated?: boolean;
  };
}

export interface ReactionClearUpdate {
  message_id: string;
  message_domain: string;
  channel_id: string;
  channel_domain: string;
  reaction?: string;
  /** Legacy dispatches carried the reaction key in this field. */
  emoji?:
    | string
    | {
        id?: string | null;
        origin_domain?: string | null;
        name?: string | null;
        animated?: boolean;
      };
}

/** Prefer the stable reaction key; structured `emoji` is display metadata. */
export function reactionClearEmoji(update: ReactionClearUpdate): string | undefined {
  const value =
    typeof update.reaction === 'string'
      ? update.reaction
      : typeof update.emoji === 'string'
        ? update.emoji
        : undefined;
  return value === undefined ? undefined : (canonicalReactionEmoji(value) ?? value);
}

export function applyReactionUpdate(
  message: Message,
  update: ReactionUpdate,
  currentUser: UserSummary | null
): Message {
  const reaction = canonicalReactionEmoji(update.reaction);
  if (!reaction) return message;
  const counts: Record<string, number> = {};
  for (const [value, count] of Object.entries(message.reaction_counts ?? {})) {
    const key = canonicalReactionEmoji(value) ?? value;
    counts[key] = (counts[key] ?? 0) + Number(count);
  }
  const next = Math.max(0, (counts[reaction] ?? 0) + (update.removed ? -1 : 1));
  if (next) counts[reaction] = next;
  else delete counts[reaction];

  const reacted = new Set(
    (message.reacted_emoji ?? []).map((value) => canonicalReactionEmoji(value) ?? value)
  );
  const updateUser =
    update.user_id && update.user_domain
      ? entityKey({ id: update.user_id, origin_domain: update.user_domain })
      : null;
  if (currentUser && updateUser === entityKey(currentUser)) {
    if (update.removed) reacted.delete(reaction);
    else reacted.add(reaction);
  }
  return { ...message, reaction_counts: counts, reacted_emoji: [...reacted] };
}

export function reactionUpdateFromDispatch(
  eventName: ReactionDispatchName,
  payload: unknown
): ReactionUpdate | null {
  if (!payload || typeof payload !== 'object') return null;
  const value = payload as Record<string, unknown>;
  const reaction = canonicalReactionEmoji(value.reaction);
  if (typeof value.id !== 'string' || typeof value.origin_domain !== 'string' || !reaction) {
    return null;
  }
  return {
    id: value.id,
    origin_domain: value.origin_domain,
    channel_id: typeof value.channel_id === 'string' ? value.channel_id : undefined,
    channel_domain: typeof value.channel_domain === 'string' ? value.channel_domain : undefined,
    reaction,
    removed:
      eventName === 'MESSAGE_REACTION_REMOVE'
        ? true
        : eventName === 'MESSAGE_REACTION_ADD'
          ? false
          : value.removed === true,
    user_id: typeof value.user_id === 'string' ? value.user_id : undefined,
    user_domain: typeof value.user_domain === 'string' ? value.user_domain : undefined,
    emoji:
      value.emoji && typeof value.emoji === 'object'
        ? (value.emoji as ReactionUpdate['emoji'])
        : undefined
  };
}

export function applyReactionDispatch(
  message: Message,
  eventName: ReactionDispatchName,
  payload: unknown,
  currentUser: UserSummary | null
): Message {
  const update = reactionUpdateFromDispatch(eventName, payload);
  return update && entityKey(message) === entityKey(update)
    ? applyReactionUpdate(message, update, currentUser)
    : message;
}

/** Reconcile either one cleared emoji group or every reaction on a message. */
export function applyReactionClear(message: Message, emoji?: string): Message {
  if (emoji === undefined) {
    return { ...message, reaction_counts: {}, reacted_emoji: [] };
  }
  const canonical = canonicalReactionEmoji(emoji);
  const counts = Object.fromEntries(
    Object.entries(message.reaction_counts ?? {}).filter(([value]) =>
      canonical ? canonicalReactionEmoji(value) !== canonical : value !== emoji
    )
  );
  return {
    ...message,
    reaction_counts: counts,
    reacted_emoji: (message.reacted_emoji ?? []).filter((value) =>
      canonical ? canonicalReactionEmoji(value) !== canonical : value !== emoji
    )
  };
}

export function messageReactionsPath(channel: string, message: string): string {
  return `/channels/${encodeURIComponent(channel)}/messages/${encodeURIComponent(message)}/reactions`;
}

/** Discord-compatible route for removing the authenticated user's reaction. */
export function ownReactionPath(channel: string, message: string, emoji: string): string {
  return `${messageReactionsPath(channel, message)}/${encodeURIComponent(emoji)}/@me`;
}

/** Build the distinct moderation routes without colliding with self-removal. */
export function reactionClearPath(channel: string, message: string, emoji?: string): string {
  return emoji === undefined
    ? messageReactionsPath(channel, message)
    : `${messageReactionsPath(channel, message)}/${encodeURIComponent(emoji)}`;
}
