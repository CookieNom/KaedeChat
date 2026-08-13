export type MessageSearchOperator = 'from' | 'mentions' | 'has';

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
