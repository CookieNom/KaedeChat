import { describe, expect, it } from 'vitest';
import {
  commandAttachmentOptionIds,
  applicationCommandAllowedByChannelPermissions,
  applicationIntegrationAllowedByUsePermission,
  applicationCommandAllowedByUsePermission,
  applicationCommandRequestIdentity,
  applicationCommandByIdentity,
  applicationCommandLauncherGroups,
  commandCompletions,
  commandComposerOptions,
  commandInvocation,
  commandOptionAllowsChannelType,
  commandOptionPayload,
  commandOptionsComplete,
  commandStringOptions,
  localizedCommandText,
  parseApplicationCommandAutocompleteChoices,
  parseApplicationCommands,
  resolveCommandInvocation,
  uniqueChatInputCommand,
  type ApplicationCommand
} from './application-commands';

const commands: ApplicationCommand[] = [
  {
    id: '1',
    application_ref: '2@apps.test',
    application_name: 'Poll Bot',
    integration_type: 'guild_install',
    interaction_context: 'guild',
    name: 'poll',
    type: 'chat_input',
    description: 'Create a poll'
  },
  {
    id: '2',
    application_ref: '2@apps.test',
    application_name: 'Poll Bot',
    integration_type: 'guild_install',
    interaction_context: 'guild',
    name: 'about',
    type: 'chat_input'
  }
];

describe('application commands', () => {
  it('keeps remote user-installed commands when guild command use is denied', () => {
    const external = (['chat_input', 'user', 'message'] as const).map((type, index) => ({
      ...commands[0],
      id: `remote-${index}`,
      application_ref: '9@remote.example',
      application_name: 'Remote Tools',
      integration_type: 'user_install' as const,
      type
    }));

    expect(
      [commands[0], ...external].filter((command) =>
        applicationCommandAllowedByUsePermission(command, false)
      )
    ).toEqual(external);
    expect(
      [commands[0], ...external].filter((command) =>
        applicationCommandAllowedByUsePermission(command, true)
      )
    ).toEqual([commands[0], ...external]);
  });

  it('fails closed when a component is missing its installation lineage', () => {
    expect(applicationIntegrationAllowedByUsePermission(null, false)).toBe(false);
    expect(applicationIntegrationAllowedByUsePermission(undefined, false)).toBe(false);
    expect(applicationIntegrationAllowedByUsePermission('user_install', false)).toBe(true);
    expect(applicationIntegrationAllowedByUsePermission('dm_capability', false)).toBe(true);
  });

  it('requires send permission only for user context commands', () => {
    const external = (['chat_input', 'user', 'message'] as const).map((type, index) => ({
      ...commands[0],
      id: `external-${index}`,
      integration_type: 'user_install' as const,
      type
    }));

    expect(
      external.filter((command) =>
        applicationCommandAllowedByChannelPermissions(command, false, false)
      )
    ).toEqual([external[0], external[2]]);
    expect(
      external.filter((command) =>
        applicationCommandAllowedByChannelPermissions(command, false, true)
      )
    ).toEqual(external);
    expect(applicationCommandAllowedByChannelPermissions(commands[0], false, true)).toBe(false);
  });

  it('matches command and application names for composer completion', () => {
    expect(commandCompletions(commands, 'pol').map((item) => item.value)).toEqual([
      '/poll',
      '/about'
    ]);
  });

  it('parses only unambiguous published commands and keeps arguments opaque', () => {
    expect(commandInvocation('/poll lunch tomorrow', commands)).toEqual({
      command: commands[0],
      options: { raw: 'lunch tomorrow' }
    });
    expect(commandInvocation('/missing hello', commands)).toBeNull();
  });

  it('parses lowercase Unicode command names using the Discord naming contract', () => {
    const unicodeCommands: ApplicationCommand[] = [
      { ...commands[0], id: '3', name: 'météo' },
      { ...commands[0], id: '4', name: 'मौसम' },
      { ...commands[0], id: '5', name: 'อากาศ' }
    ];
    expect(commandInvocation('/météo demain', unicodeCommands)?.command.name).toBe('météo');
    expect(commandInvocation('/मौसम', unicodeCommands)?.command.name).toBe('मौसम');
    expect(commandInvocation('/อากาศ', unicodeCommands)?.command.name).toBe('อากาศ');
    expect(commandInvocation('/Météo', unicodeCommands)).toBeNull();
  });

  it('projects localized labels while invoking the default command identity', () => {
    const localized: ApplicationCommand[] = [
      {
        ...commands[0],
        name: 'weather',
        name_localizations: { fr: 'météo', 'en-GB': 'forecast' },
        description: 'Current weather',
        description_localizations: { fr: 'Météo actuelle', 'es-ES': 'Tiempo actual' }
      }
    ];
    expect(commandCompletions(localized, 'mét', 'fr')[0]).toMatchObject({
      value: '/météo',
      label: '/météo',
      detail: 'Météo actuelle · Poll Bot'
    });
    expect(commandInvocation('/météo demain', localized, 'fr')).toEqual({
      command: localized[0],
      options: { raw: 'demain' }
    });
    expect(commandInvocation('/weather tomorrow', localized, 'fr')?.command.name).toBe('weather');
    expect(uniqueChatInputCommand(localized, 'météo', 'fr')?.name).toBe('weather');
    expect(localizedCommandText('Weather', { 'en-GB': 'Forecast' }, 'en-US')).toBe('Forecast');
    expect(localizedCommandText('Weather', { 'es-ES': 'Tiempo' }, 'es-419')).toBe('Tiempo');
  });

  it('does not guess when two commands project the same localized invocation name', () => {
    const ambiguous: ApplicationCommand[] = [
      { ...commands[0], id: '3', name: 'weather', name_localizations: { fr: 'météo' } },
      { ...commands[0], id: '4', name: 'forecast', name_localizations: { fr: 'météo' } }
    ];
    expect(uniqueChatInputCommand(ambiguous, 'météo', 'fr')).toBeNull();
    expect(resolveCommandInvocation('/météo', ambiguous, 'fr')).toEqual({
      kind: 'ambiguous',
      commands: ambiguous
    });
  });

  it('retains exact app and installation identity in same-name completions', () => {
    const duplicate: ApplicationCommand = {
      ...commands[0],
      id: '1',
      application_ref: '9@remote.example',
      application_name: 'Remote Poll',
      integration_type: 'user_install'
    };
    const options = commandCompletions([commands[0], duplicate], 'poll');

    expect(options).toHaveLength(2);
    expect(
      applicationCommandByIdentity([commands[0], duplicate], options[1].applicationCommand)
    ).toBe(duplicate);
    expect(options[1].applicationCommand).toEqual({
      id: '1',
      applicationRef: '9@remote.example',
      integrationType: 'user_install',
      interactionContext: 'guild'
    });
  });

  it('groups the Apps launcher by qualified application before commands', () => {
    const remote = {
      ...commands[0],
      id: '1',
      application_ref: '9@remote.example',
      application_name: 'Remote Poll'
    };
    expect(applicationCommandLauncherGroups([remote, ...commands], 'poll')).toEqual([
      {
        applicationRef: '2@apps.test',
        applicationName: 'Poll Bot',
        commands: [commands[1], commands[0]]
      },
      {
        applicationRef: '9@remote.example',
        applicationName: 'Remote Poll',
        commands: [remote]
      }
    ]);
  });

  it('exposes the bounded string options needed by Discord-style command fields', () => {
    const thread = {
      ...commands[0],
      name: 'thread',
      options: [
        { type: 'string' as const, name: 'name', required: true },
        { type: 'string' as const, name: 'message', required: true },
        { type: 'boolean' as const, name: 'private' }
      ]
    };
    expect(commandStringOptions(thread).map((option) => option.name)).toEqual(['name', 'message']);
    expect(commandOptionsComplete(thread, { name: 'test', message: '' })).toBe(false);
    expect(commandOptionsComplete(thread, { name: 'test', message: 'hello' })).toBe(true);
  });

  it('exposes numeric fields that support dynamic autocomplete', () => {
    const command = {
      ...commands[0],
      options: [
        { type: 'integer' as const, name: 'issue', autocomplete: true },
        { type: 'boolean' as const, name: 'private' }
      ]
    };
    expect(commandComposerOptions(command)).toEqual([
      { type: 'integer', name: 'issue', autocomplete: true },
      { type: 'boolean', name: 'private' }
    ]);
    expect(commandOptionPayload(command, { issue: '42', private: true })).toEqual({
      issue: 42,
      private: true
    });
  });

  it('composes typed options beneath selected subcommand groups', () => {
    const command: ApplicationCommand = {
      ...commands[0],
      name: 'admin',
      options: [
        {
          type: 'subcommand_group',
          name: 'member',
          options: [
            {
              type: 'subcommand',
              name: 'timeout',
              options: [
                { type: 'user', name: 'user', required: true },
                { type: 'integer', name: 'minutes', required: true, min_value: 1, max_value: 60 },
                { type: 'boolean', name: 'notify' }
              ]
            }
          ]
        }
      ]
    };
    const values = {
      '$container:': 'member',
      '$container:member': 'timeout',
      'member.timeout.user': '9@chat.example',
      'member.timeout.minutes': '15',
      'member.timeout.notify': false
    };
    expect(commandOptionsComplete(command, values)).toBe(true);
    expect(commandOptionPayload(command, values)).toEqual({
      member: {
        timeout: {
          user: '9@chat.example',
          minutes: 15,
          notify: false
        }
      }
    });
    expect(commandOptionsComplete(command, { ...values, 'member.timeout.minutes': '90' })).toBe(
      false
    );
  });

  it('returns only attachment uploads consumed by the selected command leaf', () => {
    const command: ApplicationCommand = {
      ...commands[0],
      options: [
        {
          type: 'subcommand',
          name: 'inspect',
          options: [
            { type: 'attachment', name: 'first', required: true },
            { type: 'attachment', name: 'second' },
            { type: 'string', name: 'caption' }
          ]
        },
        {
          type: 'subcommand',
          name: 'other',
          options: [{ type: 'attachment', name: 'file' }]
        }
      ]
    };
    expect(
      commandAttachmentOptionIds(command, {
        '$container:': 'inspect',
        'inspect.first': '91',
        'inspect.second': '92',
        'inspect.caption': 'release',
        'other.file': '93'
      })
    ).toEqual(['91', '92']);
  });

  it('filters channel choices only when the command advertises channel types', () => {
    const unrestricted = { type: 'channel' as const, name: 'destination' };
    const announcements = {
      ...unrestricted,
      channel_types: [5, 10]
    };

    expect(commandOptionAllowsChannelType(unrestricted, 17)).toBe(true);
    expect(commandOptionAllowsChannelType(announcements, 5)).toBe(true);
    expect(commandOptionAllowsChannelType(announcements, 10)).toBe(true);
    expect(commandOptionAllowsChannelType(announcements, 0)).toBe(false);
    expect(commandOptionAllowsChannelType({ type: 'user', name: 'person' }, 5)).toBe(false);
  });

  it('rejects scalar children in federated command tree arrays', () => {
    expect(() =>
      parseApplicationCommands([
        {
          ...commands[0],
          options: [
            { type: 'string', name: 'target', description: 'Target' },
            'silently dropped before this regression'
          ]
        }
      ])
    ).toThrow(/option/u);
    expect(() =>
      parseApplicationCommands([
        {
          ...commands[0],
          options: [
            {
              type: 'string',
              name: 'target',
              description: 'Target',
              choices: [{ name: 'Safe', value: 'safe' }, 7]
            }
          ]
        }
      ])
    ).toThrow(/choice/u);
    expect(() =>
      parseApplicationCommands([
        {
          ...commands[0],
          options: [
            {
              type: 'channel',
              name: 'destination',
              description: 'Destination',
              channel_types: [0, '2']
            }
          ]
        }
      ])
    ).toThrow(/channel types/u);
  });

  it('rejects a malformed autocomplete array instead of partially applying it', () => {
    expect(
      parseApplicationCommandAutocompleteChoices([
        { name: 'Safe', value: 'safe' },
        { name: 'Count', value: 2 }
      ])
    ).toEqual([
      { name: 'Safe', value: 'safe' },
      { name: 'Count', value: 2 }
    ]);
    expect(() =>
      parseApplicationCommandAutocompleteChoices([
        { name: 'Safe', value: 'safe' },
        'silently dropped before this regression'
      ])
    ).toThrow(/autocomplete choice/u);
    expect(() =>
      parseApplicationCommandAutocompleteChoices([{ name: 'Infinite', value: Infinity }])
    ).toThrow(/autocomplete choice/u);
  });

  it('retains exact effective identities and rejects duplicates', () => {
    expect(parseApplicationCommands(commands)).toEqual(commands);
    expect(() => parseApplicationCommands([commands[0], { ...commands[0] }])).toThrow(
      /duplicated/u
    );
  });

  it('retains bot-DM capability lineage in plaintext and encrypted requests', () => {
    const capability = parseApplicationCommands([
      {
        ...commands[0],
        integration_type: 'dm_capability',
        interaction_context: 'bot_dm',
        dm_capability_id: `kbdg_${'a'.repeat(43)}`,
        dm_capability_revision: '7'
      }
    ])[0];

    expect(applicationCommandRequestIdentity(capability)).toEqual({
      application_ref: '2@apps.test',
      command_id: '1',
      integration_type: 'dm_capability',
      command_name: 'poll',
      command_type: 'chat_input',
      dm_capability_id: `kbdg_${'a'.repeat(43)}`,
      dm_capability_revision: '7'
    });
    expect(() =>
      parseApplicationCommands([
        {
          ...commands[0],
          integration_type: 'dm_capability',
          interaction_context: 'bot_dm'
        }
      ])
    ).toThrow(/capability lineage/u);
  });
});
