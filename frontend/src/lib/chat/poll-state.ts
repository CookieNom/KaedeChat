import { entityKey } from './refs';
import type { MessagePoll } from './rich-content';
import type { Message, UserSummary } from './types';

export type PollVoteDispatchName = 'MESSAGE_POLL_VOTE_ADD' | 'MESSAGE_POLL_VOTE_REMOVE';

export interface PollVoteUpdate {
  message_id: string;
  message_domain: string;
  channel_id: string;
  channel_domain: string;
  user_id: string;
  user_domain: string;
  answer_id: number;
}

export function pollVoteUpdateFromDispatch(payload: unknown): PollVoteUpdate | null {
  if (!payload || typeof payload !== 'object') return null;
  const value = payload as Record<string, unknown>;
  if (
    typeof value.message_id !== 'string' ||
    typeof value.message_domain !== 'string' ||
    typeof value.channel_id !== 'string' ||
    typeof value.channel_domain !== 'string' ||
    typeof value.user_id !== 'string' ||
    typeof value.user_domain !== 'string' ||
    typeof value.answer_id !== 'number' ||
    !Number.isSafeInteger(value.answer_id) ||
    value.answer_id < 1
  ) {
    return null;
  }
  return value as unknown as PollVoteUpdate;
}

export function applyPollVoteUpdate(
  poll: MessagePoll,
  update: PollVoteUpdate,
  added: boolean,
  currentUser: UserSummary | null
): MessagePoll {
  if (!poll.answers.some((answer) => answer.answer_id === update.answer_id)) return poll;
  const currentUserVoted =
    currentUser !== null &&
    entityKey(currentUser) === entityKey({ id: update.user_id, origin_domain: update.user_domain });
  const counts = poll.results.answer_counts.map((count) =>
    count.id === update.answer_id
      ? {
          ...count,
          count: Math.max(0, count.count + (added ? 1 : -1)),
          me_voted: currentUserVoted ? added : count.me_voted
        }
      : count
  );
  return { ...poll, results: { ...poll.results, answer_counts: counts } };
}

export function applyPollVoteDispatch(
  message: Message,
  eventName: PollVoteDispatchName,
  payload: unknown,
  currentUser: UserSummary | null
): Message {
  const update = pollVoteUpdateFromDispatch(payload);
  if (
    !update ||
    !message.poll ||
    entityKey(message) !==
      entityKey({ id: update.message_id, origin_domain: update.message_domain })
  ) {
    return message;
  }
  return {
    ...message,
    poll: applyPollVoteUpdate(
      message.poll,
      update,
      eventName === 'MESSAGE_POLL_VOTE_ADD',
      currentUser
    )
  };
}
