import { entityRef } from './refs';
import type { UserSummary } from './types';

export type MessageSearchOperator = 'from' | 'mentions' | 'has';
export const MESSAGE_SEARCH_AUTHOR_TYPES = ['user', 'bot', 'webhook'] as const;
export type MessageSearchAuthorType = (typeof MESSAGE_SEARCH_AUTHOR_TYPES)[number];

export type MessageSearchOperatorMatch = {
  operator: MessageSearchOperator;
  needle: string;
  start: number;
};

export function messageSearchOperator(query: string): MessageSearchOperatorMatch | null {
  const match = query.match(/(?:^|\s)(from|mentions|has):([^\s]*)$/i);
  if (!match) return null;
  return {
    operator: match[1].toLowerCase() as MessageSearchOperator,
    needle: match[2].toLowerCase(),
    start: match.index ?? 0
  };
}

export function beginMessageSearchOperator(query: string, operator: MessageSearchOperator): string {
  const prefix = query.trimEnd();
  return `${prefix}${prefix ? ' ' : ''}${operator}:`;
}

export function replaceMessageSearchOperator(query: string, replacement = ''): string {
  const match = messageSearchOperator(query);
  if (!match) return query;
  const prefix = query.slice(0, match.start).trimEnd();
  return [prefix, replacement].filter(Boolean).join(' ');
}

export function moveSearchSuggestion(current: number, direction: 1 | -1, total: number): number {
  if (total <= 0) return 0;
  return (current + direction + total) % total;
}

/**
 * Build autocomplete candidates without reducing federated identities to a
 * display name or a bare snowflake. The first copy wins so an authoritative
 * roster profile is not replaced by a potentially older message snapshot.
 */
export function messageSearchUserCandidates(
  users: ReadonlyArray<UserSummary | null | undefined>
): UserSummary[] {
  const candidates = new Map<string, UserSummary>();
  for (const user of users) {
    if (user && !candidates.has(entityRef(user))) candidates.set(entityRef(user), user);
  }
  return [...candidates.values()];
}
