import { applicationCommandGroups, type ApplicationCommand } from './application-commands';
import type { Message, UserSummary } from './types';
import { userDisplayName } from './users';
import { parseCanonicalEntityRef } from './refs';

export interface AppContextCommandEntry {
  key: string;
  command: ApplicationCommand;
  target: Message | UserSummary;
  detail: string;
}

export interface AppContextCommandGroup {
  applicationRef: string;
  applicationName: string;
  entries: AppContextCommandEntry[];
}

export interface AppContextCommandMenuModel {
  frequent: AppContextCommandEntry[];
  groups: AppContextCommandGroup[];
}

export interface ContextCommandUsageStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const CONTEXT_COMMAND_TYPES = new Set<ApplicationCommand['type']>(['message', 'user']);
const CONTEXT_COMMAND_HISTORY_LIMIT = 100;
const FREQUENT_COMMAND_LIMIT = 5;
const CONTEXT_COMMAND_HISTORY_PREFIX = 'kaede.context-command-history.v1:';

export function appContextCommandUsageKey(command: ApplicationCommand): string {
  return JSON.stringify([
    command.application_ref,
    command.id,
    command.type,
    command.integration_type,
    command.interaction_context
  ]);
}

export function appContextCommandHistoryStorageKey(accountRef: string): string | null {
  return parseCanonicalEntityRef(accountRef)
    ? `${CONTEXT_COMMAND_HISTORY_PREFIX}${encodeURIComponent(accountRef)}`
    : null;
}

function browserUsageStorage(): ContextCommandUsageStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function appContextCommandHistory(
  accountRef: string,
  storage: ContextCommandUsageStorage | null = browserUsageStorage()
): string[] {
  const storageKey = appContextCommandHistoryStorageKey(accountRef);
  if (!storage || !storageKey) return [];
  try {
    const parsed: unknown = JSON.parse(storage.getItem(storageKey) ?? '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is string => typeof item === 'string' && item.length <= 512)
      .slice(-CONTEXT_COMMAND_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

export function rememberAppContextCommand(
  accountRef: string,
  command: ApplicationCommand,
  storage: ContextCommandUsageStorage | null = browserUsageStorage()
): void {
  const storageKey = appContextCommandHistoryStorageKey(accountRef);
  if (!storage || !storageKey || !CONTEXT_COMMAND_TYPES.has(command.type)) return;
  const history = appContextCommandHistory(accountRef, storage);
  history.push(appContextCommandUsageKey(command));
  try {
    storage.setItem(storageKey, JSON.stringify(history.slice(-CONTEXT_COMMAND_HISTORY_LIMIT)));
  } catch {
    // Command invocation must remain successful when optional local history is unavailable.
  }
}

/** Group and search context commands without losing their bound user/message target. */
export function appContextCommandGroups(
  entries: AppContextCommandEntry[],
  query: string,
  locale?: string
): AppContextCommandGroup[] {
  const byCommand = new Map(entries.map((entry) => [entry.command, entry]));
  return applicationCommandGroups(
    entries.map((entry) => entry.command),
    query,
    locale,
    CONTEXT_COMMAND_TYPES
  ).map((group) => ({
    applicationRef: group.applicationRef,
    applicationName: group.applicationName,
    entries: group.commands.flatMap((command) => {
      const entry = byCommand.get(command);
      return entry ? [entry] : [];
    })
  }));
}

/** Searchable app groups with successful frequent commands hoisted once above them. */
export function appContextCommandMenuModel(
  entries: AppContextCommandEntry[],
  query: string,
  history: readonly string[],
  locale?: string
): AppContextCommandMenuModel {
  const groups = appContextCommandGroups(entries, query, locale);
  const matching = groups.flatMap((group) => group.entries);
  const metrics = new Map<string, { count: number; last: number }>();
  history.slice(-CONTEXT_COMMAND_HISTORY_LIMIT).forEach((key, index) => {
    const current = metrics.get(key);
    metrics.set(key, { count: (current?.count ?? 0) + 1, last: index });
  });
  const frequent = matching
    .filter((entry) => metrics.has(appContextCommandUsageKey(entry.command)))
    .sort((left, right) => {
      const leftKey = appContextCommandUsageKey(left.command);
      const rightKey = appContextCommandUsageKey(right.command);
      const leftMetric = metrics.get(leftKey)!;
      const rightMetric = metrics.get(rightKey)!;
      return (
        rightMetric.count - leftMetric.count ||
        rightMetric.last - leftMetric.last ||
        (leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0)
      );
    })
    .slice(0, FREQUENT_COMMAND_LIMIT);
  const frequentKeys = new Set(frequent.map((entry) => appContextCommandUsageKey(entry.command)));
  return {
    frequent,
    groups: groups.flatMap((group) => {
      const remaining = group.entries.filter(
        (entry) => !frequentKeys.has(appContextCommandUsageKey(entry.command))
      );
      return remaining.length ? [{ ...group, entries: remaining }] : [];
    })
  };
}

export function messageAppContextCommands(
  commands: ApplicationCommand[],
  message: Message
): AppContextCommandEntry[] {
  const entries: AppContextCommandEntry[] = commands
    .filter((command) => command.type === 'message')
    .map((command) => ({
      key: `message:${command.application_ref}:${command.name}`,
      command,
      target: message,
      detail: command.application_name
    }));
  if (!message.author) return entries;
  entries.push(
    ...commands
      .filter((command) => command.type === 'user')
      .map((command) => ({
        key: `user:${command.application_ref}:${command.name}`,
        command,
        target: message.author!,
        detail: `${command.application_name} · ${userDisplayName(message.author)}`
      }))
  );
  return entries;
}

export function userAppContextCommands(
  commands: ApplicationCommand[],
  user: UserSummary
): AppContextCommandEntry[] {
  return commands
    .filter((command) => command.type === 'user')
    .map((command) => ({
      key: `user:${command.application_ref}:${command.name}`,
      command,
      target: user,
      detail: command.application_name
    }));
}
