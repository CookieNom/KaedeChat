import type { Channel, GuildMemberSummary } from './types';
import { entityRef } from './refs';

export interface CompletionQuery {
  marker: '@' | '#' | ':';
  query: string;
  start: number;
  end: number;
}

export function completionAt(value: string, cursor: number): CompletionQuery | null {
  const prefix = value.slice(0, cursor);
  const match = /(?:^|\s)([@#:])([\w.-]*)$/.exec(prefix);
  if (!match) return null;
  const marker = match[1] as '@' | '#' | ':';
  const query = match[2];
  return { marker, query, start: cursor - query.length - 1, end: cursor };
}

export function replaceCompletion(
  value: string,
  completion: CompletionQuery,
  replacement: string
): string {
  return `${value.slice(0, completion.start)}${replacement} ${value.slice(completion.end)}`;
}

export function memberCompletions(members: GuildMemberSummary[], query: string) {
  const needle = query.toLocaleLowerCase();
  return members
    .filter((member) => {
      const user = member.user;
      return [member.nickname, user.display_name, user.username, user.handle].some((value) =>
        value?.toLocaleLowerCase().includes(needle)
      );
    })
    .map((member) => ({
      value: `<@${entityRef(member.user)}>`,
      label: member.nickname ?? member.user.display_name ?? member.user.username,
      detail: `@${member.user.handle}`
    }));
}

export function channelCompletions(channels: Channel[], query: string) {
  const needle = query.toLocaleLowerCase();
  return channels
    .filter((channel) => channel.name?.toLocaleLowerCase().includes(needle))
    .map((channel) => ({ value: `#${channel.name}`, label: `#${channel.name}` }));
}

export const EMOJI_COMPLETIONS = ['wave', 'heart', 'thumbsup', 'sparkles', 'kaede'].map((name) => ({
  value: `:${name}:`,
  label: `:${name}:`
}));
