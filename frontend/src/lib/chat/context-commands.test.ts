import { describe, expect, it } from 'vitest';
import type { ApplicationCommand } from './application-commands';
import {
  appContextCommandGroups,
  appContextCommandHistory,
  appContextCommandHistoryStorageKey,
  appContextCommandMenuModel,
  appContextCommandUsageKey,
  messageAppContextCommands,
  rememberAppContextCommand,
  userAppContextCommands
} from './context-commands';
import type { Message, UserSummary } from './types';

const user = {
  id: '7',
  origin_domain: 'chat.example',
  username: 'mika',
  display_name: 'Mika',
  avatar_hash: null,
  handle: 'mika@chat.example'
} satisfies UserSummary;

const commands: ApplicationCommand[] = [
  {
    id: '1',
    application_ref: '20@apps.example',
    application_name: 'Tools',
    integration_type: 'guild_install',
    interaction_context: 'guild',
    name: 'inspect',
    type: 'message'
  },
  {
    id: '2',
    application_ref: '20@apps.example',
    application_name: 'Tools',
    integration_type: 'guild_install',
    interaction_context: 'guild',
    name: 'profile',
    type: 'user'
  },
  {
    id: '3',
    application_ref: '20@apps.example',
    application_name: 'Tools',
    integration_type: 'guild_install',
    interaction_context: 'guild',
    name: 'search',
    type: 'chat_input'
  }
];

class MemoryStorage {
  readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

describe('Apps context-command submenu', () => {
  it('collects message and author commands beneath one Apps entry', () => {
    const author = user;
    const message = { id: '9', origin_domain: 'chat.example', author } as unknown as Message;
    const entries = messageAppContextCommands(commands, message);

    expect(entries.map((entry) => entry.command.type)).toEqual(['message', 'user']);
    expect(entries[0].target).toBe(message);
    expect(entries[1].target).toBe(user);
    expect(entries[1].detail).toBe('Tools · Mika');
  });

  it('keeps slash commands in the composer and user commands in profile Apps', () => {
    const entries = userAppContextCommands(commands, user);

    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      key: 'user:20@apps.example:profile',
      detail: 'Tools'
    });
    expect(entries[0].target).toBe(user);
  });

  it('groups commands by application and searches app and localized command names', () => {
    const message = { id: '9', origin_domain: 'chat.example', author: user } as unknown as Message;
    const remote: ApplicationCommand = {
      ...commands[0],
      id: '4',
      application_ref: '30@remote.example',
      application_name: 'Moderation',
      name: 'review',
      name_localizations: { 'es-ES': 'revisar' }
    };
    const entries = messageAppContextCommands([...commands, remote], message);

    expect(
      appContextCommandGroups(entries, '', 'en-US').map((group) => group.applicationName)
    ).toEqual(['Moderation', 'Tools']);
    expect(appContextCommandGroups(entries, 'moderat', 'en-US')[0].entries).toHaveLength(1);
    expect(appContextCommandGroups(entries, 'revisar', 'es-ES')[0].entries[0].command).toBe(remote);
    expect(appContextCommandGroups(entries, 'missing', 'en-US')).toEqual([]);
  });

  it('keeps all fifteen commands of each context-command type searchable', () => {
    const message = { id: '9', origin_domain: 'chat.example', author: user } as unknown as Message;
    const maximum = (type: 'message' | 'user') =>
      Array.from({ length: 15 }, (_, index): ApplicationCommand => ({
        ...commands[0],
        id: `${type}-${index}`,
        name: `${type}-${index}`,
        type
      }));
    const entries = messageAppContextCommands([...maximum('message'), ...maximum('user')], message);

    const [group] = appContextCommandGroups(entries, '', 'en-US');
    expect(group.entries).toHaveLength(30);
    expect(appContextCommandGroups(entries, 'message-', 'en-US')[0].entries).toHaveLength(15);
    expect(appContextCommandGroups(entries, 'user-', 'en-US')[0].entries).toHaveLength(15);
  });

  it('hoists successful frequent commands with bounded per-account history', () => {
    const storage = new MemoryStorage();
    const firstAccount = '7@users.example';
    const secondAccount = '8@users.example';
    const message = { id: '9', origin_domain: 'chat.example', author: user } as unknown as Message;
    const remote: ApplicationCommand = {
      ...commands[0],
      id: 'remote-review',
      application_ref: '30@remote.example',
      application_name: 'Moderation',
      name: 'review'
    };
    const entries = messageAppContextCommands([...commands, remote], message);

    rememberAppContextCommand(firstAccount, remote, storage);
    rememberAppContextCommand(firstAccount, commands[0], storage);
    rememberAppContextCommand(firstAccount, commands[0], storage);
    const history = appContextCommandHistory(firstAccount, storage);
    const model = appContextCommandMenuModel(entries, '', history, 'en-US');

    expect(model.frequent.map((entry) => entry.command)).toEqual([commands[0], remote]);
    expect(model.groups.flatMap((group) => group.entries).map((entry) => entry.command)).toEqual([
      commands[1]
    ]);
    expect(appContextCommandHistory(secondAccount, storage)).toEqual([]);
    expect(appContextCommandHistoryStorageKey(firstAccount)).not.toBe(
      appContextCommandHistoryStorageKey(secondAccount)
    );
    expect(history).toEqual([
      appContextCommandUsageKey(remote),
      appContextCommandUsageKey(commands[0]),
      appContextCommandUsageKey(commands[0])
    ]);

    for (let index = 0; index < 110; index += 1) {
      rememberAppContextCommand(firstAccount, remote, storage);
    }
    expect(appContextCommandHistory(firstAccount, storage)).toHaveLength(100);
  });
});
