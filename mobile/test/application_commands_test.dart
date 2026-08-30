import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_commands.dart';

void main() {
  final application = EntityRef.parse('7@apps.example');
  MobileApplicationCommand command(String name, String type) =>
      MobileApplicationCommand(
        id: name,
        application: application,
        applicationName: 'Tools',
        name: name,
        type: type,
        description: '',
        integrationType: 'user_install',
        interactionContext: 'private_channel',
      );

  test('guild denial keeps remote user-installed slash and context commands',
      () {
    final guild = MobileApplicationCommand(
      id: 'guild',
      application: application,
      applicationName: 'Guild tools',
      name: 'guild',
      type: 'chat_input',
      description: '',
      integrationType: 'guild_install',
      interactionContext: 'guild',
    );
    final external = <MobileApplicationCommand>[
      for (final type in const <String>['chat_input', 'user', 'message'])
        MobileApplicationCommand(
          id: 'remote-$type',
          application: EntityRef.parse('8@remote.example'),
          applicationName: 'Remote tools',
          name: 'remote-$type',
          type: type,
          description: '',
          integrationType: 'user_install',
          interactionContext: 'guild',
        ),
    ];

    expect(
      <MobileApplicationCommand>[guild, ...external].where((item) =>
          mobileApplicationCommandAllowedByUsePermission(item, false)),
      external,
    );
    expect(
      <MobileApplicationCommand>[guild, ...external].where(
          (item) => mobileApplicationCommandAllowedByUsePermission(item, true)),
      <MobileApplicationCommand>[guild, ...external],
    );
  });

  test('component permission checks fail closed without installation lineage',
      () {
    expect(
      mobileApplicationIntegrationAllowedByUsePermission(null, false),
      isFalse,
    );
    expect(
      mobileApplicationIntegrationAllowedByUsePermission('unknown', false),
      isFalse,
    );
    expect(
      mobileApplicationIntegrationAllowedByUsePermission('user_install', false),
      isTrue,
    );
    expect(
      mobileApplicationIntegrationAllowedByUsePermission(
          'dm_capability', false),
      isTrue,
    );
  });

  test('send permission is required only for user context commands', () {
    final external = <MobileApplicationCommand>[
      command('slash', 'chat_input'),
      command('user', 'user'),
      command('message', 'message'),
    ];

    expect(
      external.where((item) =>
          mobileApplicationCommandAllowedByChannelPermissions(
              item, false, false)),
      <MobileApplicationCommand>[external[0], external[2]],
    );
    expect(
      external.where((item) =>
          mobileApplicationCommandAllowedByChannelPermissions(
              item, false, true)),
      external,
    );
  });

  test('Apps menus expose only context commands for their target surface', () {
    final user = command('Inspect user', 'user');
    final message = command('Quote message', 'message');
    final slash = command('search', 'chat_input');

    expect(mobileUserContextCommands([user, message, slash]), [user]);
    expect(
        mobileMessageContextCommands([user, message, slash]), [user, message]);
  });

  test('context Apps groups are app-first and searchable', () {
    final local = command('Inspect user', 'user');
    final remote = MobileApplicationCommand(
      id: 'remote-review',
      application: EntityRef.parse('8@remote.example'),
      applicationName: 'Moderation',
      name: 'review',
      nameLocalizations: const <String, String>{'es-ES': 'revisar'},
      type: 'message',
      description: 'Review a message',
      integrationType: 'guild_install',
      interactionContext: 'guild',
    );

    expect(
      mobileContextCommandGroups([local, remote], '', 'en-US')
          .map((group) => group.applicationName),
      ['Moderation', 'Tools'],
    );
    expect(
      mobileContextCommandGroups([local, remote], 'moderat', 'en-US')
          .single
          .commands,
      [remote],
    );
    expect(
      mobileContextCommandGroups([local, remote], 'revisar', 'es-ES')
          .single
          .commands,
      [remote],
    );
    expect(
      mobileContextCommandGroups([local, remote], 'missing', 'en-US'),
      isEmpty,
    );
  });

  test('context Apps keeps all fifteen commands of each supported type', () {
    final commands = <MobileApplicationCommand>[
      for (var index = 0; index < 15; index++)
        command('message-$index', 'message'),
      for (var index = 0; index < 15; index++) command('user-$index', 'user'),
    ];

    expect(
      mobileContextCommandGroups(commands, '', 'en-US').single.commands,
      hasLength(30),
    );
    expect(
      mobileContextCommandGroups(commands, 'message-', 'en-US').single.commands,
      hasLength(15),
    );
    expect(
      mobileContextCommandGroups(commands, 'user-', 'en-US').single.commands,
      hasLength(15),
    );
  });

  test('context Apps hoists bounded successful history per account', () {
    final inspect = command('inspect', 'user');
    final quote = command('quote', 'message');
    var history = <String>[];
    history = mobileRememberContextCommand(history, quote);
    history = mobileRememberContextCommand(history, inspect);
    history = mobileRememberContextCommand(history, inspect);

    final model = mobileContextCommandMenuModel(
      [quote, inspect],
      '',
      'en-US',
      history,
    );
    expect(model.frequent, [inspect, quote]);
    expect(model.groups, isEmpty);
    expect(
      mobileContextCommandHistoryStorageKey(
        EntityRef.parse('7@users.example'),
      ),
      isNot(
        mobileContextCommandHistoryStorageKey(
          EntityRef.parse('8@users.example'),
        ),
      ),
    );

    for (var index = 0; index < 110; index += 1) {
      history = mobileRememberContextCommand(history, quote);
    }
    expect(history, hasLength(100));
    expect(history.last, mobileContextCommandUsageKey(quote));
  });

  test('context commands bind the exact user or message identity', () {
    final user = EntityRef.parse('8@users.example');
    final message = EntityRef.parse('9@chat.example');

    expect(
      mobileContextCommandTarget(command('Inspect user', 'user'), user: user),
      user,
    );
    expect(
      mobileContextCommandTarget(
        command('Quote message', 'message'),
        user: user,
        message: message,
      ),
      message,
    );
    expect(
      () => mobileContextCommandTarget(
        command('Quote message', 'message'),
        user: user,
      ),
      throwsA(isA<FormatException>()),
    );
  });

  test('authority-selected user install and DM context remain typed', () {
    final parsed = MobileApplicationCommand.fromJson(<String, Object?>{
      'application_ref': '7@apps.example',
      'id': '91',
      'application_name': 'Tools',
      'integration_type': 'user_install',
      'interaction_context': 'bot_dm',
      'name': 'inspect',
      'type': 'chat_input',
      'description': 'Inspect a record',
    });

    expect(parsed.integrationType, 'user_install');
    expect(parsed.interactionContext, 'bot_dm');
    expect(parsed.id, '91');
  });

  test('bot-DM discovery retains exact capability lineage', () {
    final parsed = MobileApplicationCommand.fromJson(<String, Object?>{
      'application_ref': '7@apps.example',
      'id': '91',
      'application_name': 'Tools',
      'integration_type': 'dm_capability',
      'dm_capability_id': 'kbdg_${List.filled(43, 'a').join()}',
      'dm_capability_revision': '7',
      'interaction_context': 'bot_dm',
      'name': 'inspect',
      'type': 'chat_input',
      'description': 'Inspect a record',
    });

    expect(parsed.integrationType, 'dm_capability');
    expect(parsed.dmCapabilityId, 'kbdg_${List.filled(43, 'a').join()}');
    expect(parsed.dmCapabilityRevision, '7');
    expect(
      () => MobileApplicationCommand.fromJson(<String, Object?>{
        'application_ref': '7@apps.example',
        'id': '91',
        'application_name': 'Tools',
        'integration_type': 'dm_capability',
        'interaction_context': 'bot_dm',
        'name': 'inspect',
        'type': 'chat_input',
        'description': 'Inspect a record',
      }),
      throwsA(isA<FormatException>()),
    );
  });

  test('same-name commands retain stable app identity and remain ambiguous',
      () {
    final local = command('inspect', 'chat_input');
    final remote = MobileApplicationCommand(
      id: 'inspect-remote',
      application: EntityRef.parse('8@remote.example'),
      applicationName: 'Remote tools',
      name: 'inspect',
      type: 'chat_input',
      description: 'Inspect remotely',
      integrationType: 'guild_install',
      interactionContext: 'guild',
    );

    expect(mobileChatInputCommandMatches([local, remote], 'inspect', 'en-US'),
        [local, remote]);
    final groups = mobileApplicationCommandLauncherGroups(
      [remote, local],
      'inspect',
      'en-US',
    );
    expect(groups.map((group) => group.application),
        [EntityRef.parse('8@remote.example'), application]);
  });

  test('attachment file types are parsed and matched before upload', () {
    final parsed = MobileApplicationCommandOption.fromJson(<String, Object?>{
      'name': 'evidence',
      'type': 'attachment',
      'description': 'Evidence',
      'file_types': <String>['IMAGE', '.PDF'],
    });

    expect(parsed.fileTypes, ['image', '.pdf']);
    expect(mobileCommandFileMatches(parsed, 'photo.png', 'image/png'), isTrue);
    expect(mobileCommandFileMatches(parsed, 'report.PDF', 'application/pdf'),
        isTrue);
    expect(
        mobileCommandFileMatches(
            parsed, 'payload.exe', 'application/octet-stream'),
        isFalse);
  });

  test('federated command trees reject scalar array children', () {
    Map<String, Object?> commandJson(Object options) => <String, Object?>{
          'application_ref': '7@apps.example',
          'id': '91',
          'application_name': 'Tools',
          'integration_type': 'user_install',
          'interaction_context': 'private_channel',
          'name': 'inspect',
          'type': 'chat_input',
          'description': 'Inspect a record',
          'options': options,
        };

    expect(
      () => MobileApplicationCommand.fromJson(commandJson(<Object>[
        <String, Object?>{
          'name': 'target',
          'type': 'string',
          'description': 'Target',
        },
        'silently dropped before this regression',
      ])),
      throwsA(isA<FormatException>()),
    );
    expect(
      () => MobileApplicationCommand.fromJson(commandJson(<Object>[
        <String, Object?>{
          'name': 'target',
          'type': 'string',
          'description': 'Target',
          'choices': <Object>[
            <String, Object?>{'name': 'Safe', 'value': 'safe'},
            7,
          ],
        },
      ])),
      throwsA(isA<FormatException>()),
    );
    expect(
      () => MobileApplicationCommand.fromJson(commandJson(<Object>[
        <String, Object?>{
          'name': 'channel',
          'type': 'channel',
          'description': 'Channel',
          'channel_types': <Object>[0, '2'],
        },
      ])),
      throwsA(isA<FormatException>()),
    );
  });

  test(
      'autocomplete rejects a malformed array instead of partially applying it',
      () {
    expect(
      mobileAutocompleteChoices(<Object?>[
        <String, Object?>{'name': 'Safe', 'value': 'safe'},
        <String, Object?>{'name': 'Count', 'value': 2},
      ]).map((choice) => choice.value),
      <Object>['safe', 2],
    );
    expect(
      () => mobileAutocompleteChoices(<Object?>[
        <String, Object?>{'name': 'Safe', 'value': 'safe'},
        'silently dropped before this regression',
      ]),
      throwsA(isA<FormatException>()),
    );
    expect(
      () => mobileAutocompleteChoices(<Object?>[
        <String, Object?>{'name': 'Infinite', 'value': double.infinity},
      ]),
      throwsA(isA<FormatException>()),
    );
  });
}
