import { compareEntityRefs } from '$lib/chat/refs';
import type { Channel, Message, ReadStateStatus, UserSummary } from '$lib/chat/types';

type ReadStateChannel = Pick<Channel, 'id' | 'origin_domain' | 'guild_id' | 'guild_domain'>;

export interface ReadStateDispatch {
  channel_id: string;
  channel_domain: string;
  last_message_id: string | null;
  last_message_domain: string | null;
  mention_count: number;
}

function messageIsNewer(state: ReadStateStatus, message: Message): boolean {
  if (state.last_message_id === null || state.last_message_domain === null) return true;
  return (
    compareEntityRefs(message, {
      id: state.last_message_id,
      origin_domain: state.last_message_domain
    }) > 0
  );
}

function latestMessageIsUnread(state: ReadStateStatus, update: ReadStateDispatch): boolean {
  if (state.last_message_id === null || state.last_message_domain === null) return false;
  if (update.last_message_id === null || update.last_message_domain === null) return true;
  return (
    compareEntityRefs(
      { id: state.last_message_id, origin_domain: state.last_message_domain },
      { id: update.last_message_id, origin_domain: update.last_message_domain }
    ) > 0
  );
}

export function applyIncomingMessage(
  readStates: ReadStateStatus[],
  message: Message,
  currentUser: UserSummary | null,
  channel: ReadStateChannel | null
): ReadStateStatus[] {
  if (
    !currentUser ||
    (message.author_id === currentUser.id && message.author_domain === currentUser.origin_domain)
  ) {
    return readStates;
  }
  // Older retained gateway events may contain `{}` here because Lua cjson
  // collapsed empty JSON arrays while assigning their topic sequence. Treat
  // malformed/legacy empty mention projections as empty so one event cannot
  // abort all unread-state reducers in the tab.
  const mentions = Array.isArray(message.mention_user_refs) ? message.mention_user_refs : [];
  const mentioned = mentions.some(
    (reference) =>
      reference.id === currentUser.id && reference.origin_domain === currentUser.origin_domain
  );
  const existing = readStates.some(
    (state) =>
      state.channel_id === message.channel_id && state.channel_domain === message.channel_domain
  );
  if (!existing) {
    if (
      !channel ||
      channel.id !== message.channel_id ||
      channel.origin_domain !== message.channel_domain
    ) {
      return readStates;
    }
    return [
      ...readStates,
      {
        channel_id: message.channel_id,
        channel_domain: message.channel_domain,
        guild_id: channel.guild_id,
        guild_domain: channel.guild_domain,
        last_message_id: message.id,
        last_message_domain: message.origin_domain,
        read_message_id: null,
        read_message_domain: null,
        mention_count: mentioned ? 1 : 0,
        unread: true
      }
    ];
  }
  return readStates.map((state) => {
    const sameChannel =
      state.channel_id === message.channel_id && state.channel_domain === message.channel_domain;
    if (!sameChannel || !messageIsNewer(state, message)) return state;
    return {
      ...state,
      last_message_id: message.id,
      last_message_domain: message.origin_domain,
      mention_count: state.mention_count + (mentioned ? 1 : 0),
      unread: true
    };
  });
}

export function applyReadStateDispatch(
  readStates: ReadStateStatus[],
  update: ReadStateDispatch
): ReadStateStatus[] {
  return readStates.map((state) => {
    const sameChannel =
      state.channel_id === update.channel_id && state.channel_domain === update.channel_domain;
    if (!sameChannel) return state;
    return {
      ...state,
      read_message_id: update.last_message_id,
      read_message_domain: update.last_message_domain,
      mention_count: update.mention_count,
      unread: update.mention_count > 0 || latestMessageIsUnread(state, update)
    };
  });
}
