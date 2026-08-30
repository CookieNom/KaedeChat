// Layout smoke test for the Discord-style settings surfaces. The screens
// are rendered with a real mobile controller wired to a canned Dio client,
// so the redesigned layout is verified without a live device.
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/settings/settings_screen.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sqflite/sqflite.dart' show Database;

/// No-op database; the settings flows never touch the local cache.
final class _TestDatabase implements Database {
  @override
  dynamic noSuchMethod(Invocation invocation) =>
      throw UnimplementedError('$invocation');
}

KaedeUser fixtureUser() => KaedeUser(
      ref: EntityRef(Snowflake('100'), Domain('chat.example')),
      username: 'kaede',
      handle: '@kaede@chat.example',
      displayName: 'Kaede',
      bio: 'Designing better fediverse chats.',
      customStatus: 'building guild settings',
      email: 'kaede@example.com',
      emailVerified: true,
      mfaEnabled: false,
      ageAssuranceState: 'adult',
    );

KaedeUser fixtureRemoteAuditActor() => KaedeUser(
      ref: EntityRef(Snowflake('101'), Domain('remote.example')),
      username: 'remote_mod',
      handle: '@remote_mod@remote.example',
      displayName: 'Remote Moderator',
    );

KaedeUser fixturePagedAuditActor(int id) => KaedeUser(
      ref: EntityRef(Snowflake('$id'), Domain('chat.example')),
      username: 'paged_$id',
      handle: '@paged_$id@chat.example',
      displayName: 'Paged member $id',
    );

KaedeGuild fixtureGuild(KaedeUser user) {
  final guildRef = EntityRef(Snowflake('1'), Domain('chat.example'));
  final category = KaedeChannel(
    ref: EntityRef(Snowflake('11'), Domain('chat.example')),
    guildRef: guildRef,
    type: ChannelType.category,
    position: 0,
    name: 'Community',
    permissions: BigInt.zero,
  );
  final general = KaedeChannel(
    ref: EntityRef(Snowflake('12'), Domain('chat.example')),
    guildRef: guildRef,
    type: ChannelType.text,
    position: 1,
    name: 'general',
    topic: 'Guild-wide discussion',
    parentRef: category.ref,
    permissions: BigInt.zero,
  );
  final announcements = KaedeChannel(
    ref: EntityRef(Snowflake('13'), Domain('chat.example')),
    guildRef: guildRef,
    type: ChannelType.announcement,
    position: 2,
    name: 'announcements',
    parentRef: category.ref,
    permissions: BigInt.zero,
    encryptionMode: 'e2ee',
  );
  final lounge = KaedeChannel(
    ref: EntityRef(Snowflake('14'), Domain('chat.example')),
    guildRef: guildRef,
    type: ChannelType.voice,
    position: 3,
    name: 'Lounge',
    permissions: BigInt.zero,
  );
  final moderator = KaedeRole(
    ref: EntityRef(Snowflake('21'), Domain('chat.example')),
    guildRef: guildRef,
    name: 'Moderator',
    color: 0x5865F2,
    permissions: BigInt.from(Permission.manageNicknames),
    position: 2,
    hoist: true,
    mentionable: true,
  );
  final member = KaedeRole(
    ref: EntityRef(Snowflake('22'), Domain('chat.example')),
    guildRef: guildRef,
    name: 'Member',
    color: 0,
    permissions: BigInt.zero,
    position: 1,
    hoist: false,
    mentionable: false,
  );
  return KaedeGuild(
    ref: guildRef,
    name: 'Kaede Guild',
    description: 'Testing the new Discord-style settings.',
    ownerRef: user.ref,
    permissions: BigInt.zero,
    unavailable: false,
    channels: [category, general, announcements, lounge],
    roles: [moderator, member],
  );
}

const _settingsPayload = <String, Object?>{
  'presence_preference': 'online',
  'dm_privacy': 'friends',
  'age_restricted_dm_commands_enabled': false,
  'notification_settings': <String, Object?>{
    'direct_messages': true,
    'mentions': true,
    'relationships': true,
    'show_notification_previews': true,
  },
};

const _sessionsPayload = <Map<String, Object?>>[
  {
    'id': 's1',
    'device_name': 'Pixel 9 (Android)',
    'created_at': '2026-08-01 10:00',
    'last_seen_at': '2026-08-21 03:00',
  },
  {
    'id': 's2',
    'device_name': 'Kaede Desktop',
    'created_at': '2026-07-12 09:00',
    'last_seen_at': '2026-08-20 22:10',
  },
];

const _suspendedApplicationInstallationsPayload = <Map<String, Object?>>[
  {
    'id': '10',
    'application_ref': '20@apps.example',
    'application_name': 'Tasks',
    'application_description': 'Keeps work organized.',
    'application_icon_hash': null,
    'bot_user_ref': '30@apps.example',
    'user_ref': '100@chat.example',
    'scopes': <String>[
      'applications.commands',
      'interactions.respond',
    ],
    'intents': <String>['interactions'],
    'contexts': <String>['guild', 'private_channel'],
    'e2ee_participant_capable': true,
    'grant_revision': '3',
    'status': 'suspended',
    'revoked_at': null,
    'created_at': '2026-08-27T12:00:00Z',
    'updated_at': '2026-08-28T12:00:00Z',
  },
];

const _auditPayload = <Map<String, Object?>>[
  {
    'id': '900',
    'actor_id': '100',
    'actor_domain': 'chat.example',
    'action_type': 11,
    'target_type': 'channel',
    'target_ref': <String, Object?>{'id': '12'},
    'reason': 'Keep the welcome channel calm',
    'changes': <Object?>[
      <String, Object?>{
        'key': 'rate_limit_per_user',
        'old_value': 0,
        'new_value': 15,
      },
    ],
    'created_at': '2026-08-21T12:30:00Z',
  },
  {
    'id': '899',
    'actor_id': '100',
    'actor_domain': 'chat.example',
    'action_type': 30,
    'target_type': 'role',
    'target_ref': <String, Object?>{'id': '21'},
    'changes': <Object?>[],
    'created_at': '2026-08-21T12:00:00Z',
  },
];

const _emojiPayload = <Map<String, Object?>>[
  {
    'id': '50',
    'origin_domain': 'chat.example',
    'guild_id': '1',
    'guild_domain': 'chat.example',
    'name': 'owned_wave',
    'animated': false,
    'available': true,
    'roles': <Object?>[],
    'media_hash':
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'creator_id': '100',
    'creator_domain': 'chat.example',
  },
  {
    'id': '51',
    'origin_domain': 'chat.example',
    'guild_id': '1',
    'guild_domain': 'chat.example',
    'name': 'other_wave',
    'animated': false,
    'available': true,
    'roles': <Object?>[],
    'media_hash':
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'creator_id': '101',
    'creator_domain': 'chat.example',
  },
];

const _stickerPayload = <Map<String, Object?>>[
  {
    'id': '60',
    'origin_domain': 'chat.example',
    'guild_id': '1',
    'guild_domain': 'chat.example',
    'guild_name': 'Kaede Guild',
    'name': 'owned_sticker',
    'description': 'Mine',
    'animated': false,
    'available': true,
    'media_hash':
        'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    'creator_id': '100',
    'creator_domain': 'chat.example',
  },
  {
    'id': '61',
    'origin_domain': 'chat.example',
    'guild_id': '1',
    'guild_domain': 'chat.example',
    'guild_name': 'Kaede Guild',
    'name': 'other_sticker',
    'description': 'Someone else’s',
    'animated': false,
    'available': true,
    'media_hash':
        'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    'creator_id': '101',
    'creator_domain': 'chat.example',
  },
];

const _soundboardPayload = <Map<String, Object?>>[
  {
    'id': '70',
    'origin_domain': 'chat.example',
    'guild_id': '1',
    'guild_domain': 'chat.example',
    'name': 'Owned chime',
    'media_hash':
        'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    'content_type': 'audio/mpeg',
    'volume': .8,
    'emoji_id': null,
    'emoji_domain': null,
    'emoji_name': '🎉',
    'available': true,
    'duration_ms': 1800,
    'created_by_id': '100',
    'created_by_domain': 'chat.example',
    'version': '1',
  },
  {
    'id': '71',
    'origin_domain': 'chat.example',
    'guild_id': '1',
    'guild_domain': 'chat.example',
    'name': 'Other chime',
    'media_hash':
        'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    'content_type': 'audio/ogg',
    'volume': .7,
    'emoji_id': null,
    'emoji_domain': null,
    'emoji_name': '🔔',
    'available': true,
    'duration_ms': 1600,
    'created_by_id': '101',
    'created_by_domain': 'chat.example',
    'version': '1',
  },
];

/// Dio client that answers every Kaede API call from canned payloads and
/// records each request so tests can assert on what the UI sent.
Dio _cannedClient(
  KaedeGuild guild,
  KaedeUser user,
  List<Map<String, Object?>> log, {
  List<Map<String, Object?>> applicationInstallations = const [],
}) {
  final dio = Dio(BaseOptions(baseUrl: 'https://chat.example'));
  dio.interceptors.add(InterceptorsWrapper(
    onRequest: (options, handler) {
      final path = options.path;
      final method = options.method;
      log.add({
        'method': method,
        'path': path,
        'data': options.data,
        'query': Map<String, Object?>.from(options.queryParameters),
      });
      Object? data;
      if (path == '/api/v1/users/@me/settings') {
        data = _settingsPayload;
      } else if (method == 'PATCH' && path == '/api/v1/users/@me') {
        data = user.toJson();
      } else if (path == '/api/v1/auth/sessions') {
        data = _sessionsPayload;
      } else if (path == '/api/v1/users/@me/application-installations') {
        data = applicationInstallations;
      } else if (path == '/api/v1/guilds/1@chat.example') {
        data = guild.toJson();
      } else if (path == '/api/v1/guilds/1@chat.example/audit-logs') {
        data = _auditPayload;
      } else if (path == '/api/v1/guilds/1@chat.example/emojis') {
        data = _emojiPayload;
      } else if (path == '/api/v1/guilds/1@chat.example/stickers') {
        data = _stickerPayload;
      } else if (path == '/api/v1/guilds/1@chat.example/soundboard-sounds') {
        data = <String, Object?>{'items': _soundboardPayload};
      } else if (path == '/api/v1/guilds/1@chat.example/members') {
        final query =
            '${options.queryParameters['query'] ?? ''}'.trim().toLowerCase();
        final after = '${options.queryParameters['after'] ?? ''}';
        data = query.contains('paged')
            ? (after.isEmpty
                ? List<Object?>.generate(
                    100,
                    (index) => <String, Object?>{
                      'user': fixturePagedAuditActor(200 + index).toJson(),
                      'role_ids': <Object?>['21'],
                    },
                  )
                : <Object?>[
                    <String, Object?>{
                      'user': fixturePagedAuditActor(300).toJson(),
                      'role_ids': <Object?>['21'],
                    },
                  ])
            : query.contains('remote')
                ? <Object?>[
                    <String, Object?>{
                      'user': fixtureRemoteAuditActor().toJson(),
                      'role_ids': <Object?>['21'],
                    },
                  ]
                : <Object?>[
                    <String, Object?>{
                      'user': user.toJson(),
                      'role_ids': <Object?>['21'],
                    },
                  ];
      } else if (path ==
          '/api/v1/guilds/1@chat.example/notification-settings') {
        data = <String, Object?>{'level': 'mentions'};
      } else if (method == 'PATCH' &&
          path == '/api/v1/guilds/1@chat.example/channels') {
        // Batch reorder: an empty success body is a valid response.
      } else {
        data = <String, Object?>{};
      }
      handler.resolve(
          Response(requestOptions: options, data: data, statusCode: 200));
    },
  ));
  return dio;
}

Future<MobileController> fixtureController(
  KaedeGuild guild,
  KaedeUser user,
  List<Map<String, Object?>> requestLog, {
  List<Map<String, Object?>> applicationInstallations = const [],
}) async {
  final api = KaedeApiClient(
    vault: const SessionVault(),
    httpClient: _cannedClient(
      guild,
      user,
      requestLog,
      applicationInstallations: applicationInstallations,
    ),
  );
  final repository = KaedeRepository(api);
  final gateway = GatewayClient(
    tokens: () async => null,
    socketConnector: (uri) => throw UnimplementedError(),
  );
  final database = await LocalDatabase.openWithDatabase(_TestDatabase());
  final push = PushService.test(firebaseReady: true);
  final controller = MobileController(repository, api, gateway, database, push);
  controller.state = MobileState(user: user);
  return controller;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    PackageInfo.setMockInitialValues(
      appName: 'Kaede Chat',
      packageName: 'com.example.kaede',
      version: '0.1.0',
      buildNumber: '1',
      buildSignature: 'test',
    );
  });

  testWidgets('account settings renders Discord-style sections',
      (tester) async {
    // Tall canvas so every section builds without lazy culling; this is a
    // structural check, not a pixel check.
    tester.view.physicalSize = const Size(840, 10000);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final requestLog = <Map<String, Object?>>[];
    final controller =
        await fixtureController(fixtureGuild(user), user, requestLog);

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: const Scaffold(body: SettingsScreen()),
      ),
    ));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);

    // Every Discord-style uppercase section header and the footer actions.
    expect(find.text('PROFILE'), findsOneWidget);
    expect(find.text('ACCOUNT'), findsOneWidget);
    expect(find.text('SECURITY'), findsOneWidget);
    expect(find.text('ACTIVITY STATUS'), findsOneWidget);
    expect(find.text('NOTIFICATIONS'), findsOneWidget);
    expect(find.text('PRIVACY'), findsOneWidget);
    expect(find.text('APPEARANCE'), findsOneWidget);
    expect(find.text('DEVELOPER'), findsOneWidget);
    expect(find.text('Developer Portal'), findsOneWidget);
    expect(find.text('Developer mode'), findsOneWidget);
    expect(find.text('DEVICES'), findsOneWidget);
    expect(find.text('Log out'), findsOneWidget);
    expect(find.text('Open-source licences'), findsOneWidget);
    expect(find.text('Kaede'), findsWidgets);
    expect(find.text('Age-restricted commands in direct messages'),
        findsOneWidget);

    // Sessions load through the repository and render as flat rows.
    expect(find.text('Kaede Desktop'), findsOneWidget);
    expect(find.text('Pixel 9 (Android)'), findsOneWidget);

    // Tapping the direct-message toggle saves the notification preference.
    await tester.tap(find.text('Direct messages'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);

    await tester.tap(find.text('Age-restricted commands in direct messages'));
    await tester.pumpAndSettle();
    expect(
      requestLog.where((request) {
        final data = request['data'];
        return request['method'] == 'PATCH' &&
            request['path'] == '/api/v1/users/@me/settings' &&
            data is Map &&
            data['age_restricted_dm_commands_enabled'] == true;
      }),
      hasLength(1),
    );
  });

  testWidgets('suspended authorized app is unavailable and remains revocable',
      (tester) async {
    tester.view.physicalSize = const Size(840, 10000);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final requestLog = <Map<String, Object?>>[];
    final controller = await fixtureController(
      fixtureGuild(user),
      user,
      requestLog,
      applicationInstallations: _suspendedApplicationInstallationsPayload,
    );

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: const Scaffold(body: SettingsScreen()),
      ),
    ));
    await tester.pumpAndSettle();

    expect(
        find.text('Suspended · Unavailable · Guilds · Private conversations'),
        findsOneWidget);
    await tester.tap(find.text('Tasks'));
    await tester.pumpAndSettle();

    expect(find.text('SUSPENDED · UNAVAILABLE'), findsOneWidget);
    expect(find.text('Commands unavailable'), findsOneWidget);
    expect(
      find.text(suspendedUserApplicationExplanation),
      findsOneWidget,
    );

    final contextToggles = tester
        .widgetList<CheckboxListTile>(find.byType(CheckboxListTile))
        .toList(growable: false);
    expect(contextToggles, hasLength(3));
    expect(contextToggles.every((tile) => tile.onChanged == null), isTrue);
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Save command access'),
          )
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<TextButton>(find.widgetWithText(TextButton, 'Revoke app'))
          .onPressed,
      isNotNull,
    );

    await tester.tap(find.widgetWithText(TextButton, 'Revoke app'));
    await tester.pumpAndSettle();
    expect(find.text('Revoke Tasks?'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Revoke'));
    await tester.pumpAndSettle();

    expect(
      requestLog.where((request) =>
          request['method'] == 'DELETE' &&
          request['path'] == '/api/v1/users/@me/application-installations/10'),
      hasLength(1),
    );
    expect(
      requestLog.where((request) =>
          request['method'] == 'PATCH' &&
          request['path'] == '/api/v1/users/@me/application-installations/10'),
      isEmpty,
    );
  });

  testWidgets('guild settings renders Discord-style list and tabs',
      (tester) async {
    // Tall canvas so the tab panels build without lazy culling.
    tester.view.physicalSize = const Size(840, 10000);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final guild = fixtureGuild(user);
    final requestLog = <Map<String, Object?>>[];
    final controller = await fixtureController(guild, user, requestLog);

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: GuildManagementScreen(guild: guild),
      ),
    ));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);

    // The flat section list with the guild identity header.
    expect(find.text('Kaede Guild'), findsWidgets);
    expect(find.text('Overview'), findsOneWidget);
    expect(find.text('Channels'), findsOneWidget);
    expect(find.text('Roles'), findsOneWidget);
    expect(find.text('Members'), findsOneWidget);
    expect(find.text('Bans'), findsOneWidget);
    expect(find.text('Integrations · Webhooks'), findsOneWidget);
    expect(find.text('Integrations · Channels Followed'), findsOneWidget);
    expect(find.text('Integrations · Bots & Apps'), findsOneWidget);
    expect(find.text('Audit'), findsOneWidget);

    // Overview tab: flat panels, uppercase headers, save button.
    await tester.tap(find.text('Overview').last);
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('GUILD PROFILE'), findsOneWidget);
    expect(find.text('NOTIFICATIONS'), findsOneWidget);
    expect(find.text('OWNERSHIP'), findsOneWidget);
    expect(find.text('Federated message history'), findsOneWidget);
    expect(find.text('Delete guild'), findsOneWidget);

    await tester.pageBack();
    await tester.pumpAndSettle();

    // Channels tab: flat reorderable rows on the settings surface.
    await tester.tap(find.text('Channels').last);
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('Community'), findsOneWidget);
    expect(find.text('general'), findsOneWidget);
    expect(find.text('announcements'), findsOneWidget);
    expect(find.text('Lounge'), findsOneWidget);
    expect(find.text('Create channel'), findsOneWidget);

    // Lounge has no category yet, so its row shows the no-category summary.
    expect(find.text('Voice channel · no category'), findsOneWidget);

    // Row menu offers "Move to category" and the category row offers a + to
    // create a channel inside it.
    expect(
      find.byTooltip('Add a channel to this category'),
      findsOneWidget,
    );
    await tester.tap(find.byIcon(Icons.more_vert).last);
    await tester.pumpAndSettle();
    expect(find.text('Move to category'), findsWidgets);

    // The choice sheet lists every category plus "No category". The sheet
    // is the only ConstrainedBox(maxWidth: 560) on this surface.
    await tester.tap(find.text('Move to category').last);
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    final choiceSheet = find.byWidgetPredicate(
      (widget) =>
          widget is ConstrainedBox && widget.constraints.maxWidth == 560,
    );
    expect(choiceSheet, findsOneWidget);
    expect(find.text('No category'), findsOneWidget);
    expect(
      find.descendant(of: choiceSheet, matching: find.text('Community')),
      findsOneWidget,
    );

    // Move Lounge into Community; the row re-parents and the batch reorder
    // carries the new parent to the API.
    await tester.tap(
      find.descendant(of: choiceSheet, matching: find.text('Community')),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('Voice channel · in Community'), findsOneWidget);

    final reorder = requestLog
        .where((request) =>
            request['method'] == 'PATCH' &&
            request['path'] == '/api/v1/guilds/1@chat.example/channels')
        .toList();
    expect(reorder, isNotEmpty);
    final positions =
        (reorder.last['data'] as Map<Object?, Object?>)['channels']! as List;
    final lounge = positions
        .whereType<Map<Object?, Object?>>()
        .firstWhere((item) => item['id'] == '14');
    expect(lounge['parent_id'], '11');

    // The per-category + opens the editor pinned to that category.
    await tester.tap(
      find.byTooltip('Add a channel to this category'),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('Create a channel'), findsOneWidget);
    // The editor's Category dropdown is preselected to the category whose
    // + button opened it.
    expect(
      find.descendant(
        of: find.byKey(const ValueKey('channel-category-field')),
        matching: find.text('Community'),
      ),
      findsOneWidget,
    );
    await tester.tap(find.byTooltip('Close'));
    await tester.pumpAndSettle();
  });

  testWidgets('guild audit renders readable, expandable event details',
      (tester) async {
    final semantics = tester.ensureSemantics();
    tester.view.physicalSize = const Size(840, 2400);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final guild = fixtureGuild(user);
    final requestLog = <Map<String, Object?>>[];
    final controller = await fixtureController(guild, user, requestLog);

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: GuildManagementScreen(guild: guild),
      ),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Audit'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.text('All members'), findsOneWidget);
    expect(find.text('All actions'), findsOneWidget);
    expect(
      find.bySemanticsLabel(RegExp('Filter audit log by actor')),
      findsOneWidget,
    );
    expect(find.text('Kaede updated #general'), findsOneWidget);
    expect(find.text('Kaede created @Moderator'), findsOneWidget);
    expect(find.text('11'), findsNothing);

    await tester.tap(
      find.descendant(
        of: find.byType(ExpansionTile).first,
        matching: find.byType(ListTile),
      ),
    );
    await tester.pumpAndSettle();
    expect(
        find.textContaining('Keep the welcome channel calm'), findsOneWidget);
    expect(find.text('CHANGES'), findsOneWidget);
    expect(find.textContaining('Rate limit per user:'), findsOneWidget);

    // Action filtering is authoritative: both the Discord-style action code
    // and Kaede's disambiguating target type are sent to the server. The
    // selection remains active when the user refreshes the current page.
    await tester.tap(find.text('All actions'));
    await tester.pumpAndSettle();
    final channelUpdatedOption = find.descendant(
      of: find.byType(PopupMenuItem<String>),
      matching: find.text('Channel updated'),
    );
    expect(channelUpdatedOption, findsOneWidget);
    await tester.tap(channelUpdatedOption);
    await tester.pumpAndSettle();

    List<Map<String, Object?>> auditRequests() => requestLog
        .where((request) =>
            request['path'] == '/api/v1/guilds/1@chat.example/audit-logs')
        .toList();
    expect(
      auditRequests().last['query'],
      <String, Object?>{
        'limit': 50,
        'action_type': 11,
        'target_type': 'channel',
      },
    );

    await tester.tap(find.text('All members'));
    await tester.pumpAndSettle();
    expect(
      find.byKey(const ValueKey('audit-actor-picker')),
      findsOneWidget,
    );
    await tester.enterText(
      find.byKey(const ValueKey('audit-actor-query')),
      'Remote',
    );
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();
    final remoteActor = find.byKey(
      const ValueKey('audit-actor-101@remote.example'),
    );
    expect(remoteActor, findsOneWidget);
    expect(find.text('Remote Moderator'), findsOneWidget);
    await tester.tap(remoteActor);
    await tester.pumpAndSettle();

    final memberSearches = requestLog
        .where((request) =>
            request['path'] == '/api/v1/guilds/1@chat.example/members' &&
            (request['query'] as Map<Object?, Object?>)['query'] == 'Remote')
        .toList();
    expect(memberSearches, hasLength(1));
    expect(memberSearches.single['query'], <String, Object?>{
      'limit': 100,
      'query': 'Remote',
    });
    expect(
      auditRequests().last['query'],
      <String, Object?>{
        'limit': 50,
        'user_id': '101@remote.example',
        'action_type': 11,
        'target_type': 'channel',
      },
    );

    // A departed actor need not still appear in member search. An exact,
    // canonical federated reference remains selectable and is sent unchanged.
    await tester.tap(find.byKey(const ValueKey('audit-actor-filter')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('audit-actor-query')),
      '777@departed.example',
    );
    await tester.pump();
    final departedActor = find.byKey(
      const ValueKey('audit-actor-777@departed.example'),
    );
    expect(departedActor, findsOneWidget);
    expect(find.text('Use 777@departed.example'), findsOneWidget);
    await tester.tap(departedActor);
    await tester.pumpAndSettle();
    expect(
      auditRequests().last['query'],
      <String, Object?>{
        'limit': 50,
        'user_id': '777@departed.example',
        'action_type': 11,
        'target_type': 'channel',
      },
    );

    final requestsBeforeRefresh = auditRequests().length;
    final refreshButton = find.byTooltip('Refresh audit log');
    await tester.ensureVisible(refreshButton);
    await tester.tap(refreshButton);
    await tester.pumpAndSettle();
    expect(auditRequests(), hasLength(requestsBeforeRefresh + 1));
    expect(
      auditRequests().last['query'],
      <String, Object?>{
        'limit': 50,
        'user_id': '777@departed.example',
        'action_type': 11,
        'target_type': 'channel',
      },
    );
    expect(find.text('Channel updated'), findsWidgets);
    semantics.dispose();
  });

  testWidgets('guild audit actor search debounces and paginates',
      (tester) async {
    tester.view.physicalSize = const Size(840, 2400);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final guild = fixtureGuild(user);
    final requestLog = <Map<String, Object?>>[];
    final controller = await fixtureController(guild, user, requestLog);

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: GuildManagementScreen(guild: guild),
      ),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Audit'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('audit-actor-filter')));
    await tester.pumpAndSettle();

    List<Map<String, Object?>> pagedRequests() => requestLog
        .where((request) =>
            request['path'] == '/api/v1/guilds/1@chat.example/members' &&
            (request['query'] as Map<Object?, Object?>)['query'] == 'Paged')
        .toList();

    await tester.enterText(
      find.byKey(const ValueKey('audit-actor-query')),
      'Paged',
    );
    await tester.pump(const Duration(milliseconds: 250));
    expect(pagedRequests(), isEmpty);
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pumpAndSettle();
    expect(pagedRequests(), hasLength(1));
    expect(pagedRequests().single['query'], <String, Object?>{
      'limit': 100,
      'query': 'Paged',
    });

    final picker = find.byKey(const ValueKey('audit-actor-picker'));
    for (var attempt = 0;
        attempt < 20 && pagedRequests().length == 1;
        attempt += 1) {
      await tester.drag(picker, const Offset(0, -600));
      await tester.pumpAndSettle();
    }
    expect(pagedRequests(), hasLength(2));
    expect(pagedRequests().last['query'], <String, Object?>{
      'limit': 100,
      'query': 'Paged',
      'after': '299@chat.example',
    });
  });

  testWidgets('expression creators can upload and maintain only their own',
      (tester) async {
    tester.view.physicalSize = const Size(840, 2600);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final guildJson = fixtureGuild(user).toJson()
      ..['owner_id'] = '999'
      ..['permissions'] = '${Permission.createGuildExpressions}';
    final guild = KaedeGuild.fromJson(guildJson);
    final controller =
        await fixtureController(guild, user, <Map<String, Object?>>[]);

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: GuildManagementScreen(guild: guild),
      ),
    ));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Emoji'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('emoji-actions-50@chat.example')),
        findsOneWidget);
    expect(find.byKey(const ValueKey('emoji-actions-51@chat.example')),
        findsNothing);
    expect(find.text('Upload emoji'), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();

    await tester.tap(find.text('Stickers'));
    // Sticker placeholders may keep an image-frame callback scheduled under
    // the test binding's blocked network client, so advance the route and
    // repository futures explicitly instead of waiting for a global settle.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));
    await tester.pump();
    expect(find.byKey(const ValueKey('sticker-actions-60@chat.example')),
        findsOneWidget);
    expect(find.byKey(const ValueKey('sticker-actions-61@chat.example')),
        findsNothing);
    expect(find.text('Create sticker'), findsOneWidget);
    await tester.pageBack();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));

    await tester.tap(find.text('Soundboard'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('soundboard-actions-70@chat.example')),
        findsOneWidget);
    expect(find.byKey(const ValueKey('soundboard-actions-71@chat.example')),
        findsNothing);
    expect(find.text('Upload sound'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('expression managers maintain others without create controls',
      (tester) async {
    tester.view.physicalSize = const Size(840, 1800);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final guildJson = fixtureGuild(user).toJson()
      ..['owner_id'] = '999'
      ..['permissions'] = '${Permission.manageGuildExpressions}';
    final guild = KaedeGuild.fromJson(guildJson);
    final controller =
        await fixtureController(guild, user, <Map<String, Object?>>[]);

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: GuildManagementScreen(guild: guild),
      ),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Emoji'));
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('emoji-actions-50@chat.example')),
        findsOneWidget);
    expect(find.byKey(const ValueKey('emoji-actions-51@chat.example')),
        findsOneWidget);
    expect(find.text('Upload emoji'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Soundboard playback alone does not expose management settings',
      (tester) async {
    tester.view.physicalSize = const Size(840, 1800);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final guildJson = fixtureGuild(user).toJson()
      ..['owner_id'] = '999'
      ..['permissions'] = '${Permission.useSoundboard}';
    final guild = KaedeGuild.fromJson(guildJson);
    final controller =
        await fixtureController(guild, user, <Map<String, Object?>>[]);

    await tester.pumpWidget(ProviderScope(
      overrides: [
        mobileControllerProvider.overrideWith((ref) => controller),
      ],
      child: MaterialApp(
        theme: kaedeTheme(),
        home: GuildManagementScreen(guild: guild),
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Soundboard'), findsNothing);
    expect(find.text('Emoji'), findsNothing);
    expect(find.text('Stickers'), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
