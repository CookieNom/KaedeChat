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

/// Dio client that answers every Kaede API call from canned payloads and
/// records each request so tests can assert on what the UI sent.
Dio _cannedClient(
  KaedeGuild guild,
  KaedeUser user,
  List<Map<String, Object?>> log,
) {
  final dio = Dio(BaseOptions(baseUrl: 'https://chat.example'));
  dio.interceptors.add(InterceptorsWrapper(
    onRequest: (options, handler) {
      final path = options.path;
      final method = options.method;
      log.add({'method': method, 'path': path, 'data': options.data});
      Object? data;
      if (path == '/api/v1/users/@me/settings') {
        data = _settingsPayload;
      } else if (method == 'PATCH' && path == '/api/v1/users/@me') {
        data = user.toJson();
      } else if (path == '/api/v1/auth/sessions') {
        data = _sessionsPayload;
      } else if (path == '/api/v1/guilds/1@chat.example') {
        data = guild.toJson();
      } else if (path == '/api/v1/guilds/1@chat.example/audit-logs') {
        data = _auditPayload;
      } else if (path == '/api/v1/guilds/1@chat.example/members') {
        data = <Object?>[
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
  List<Map<String, Object?>> requestLog,
) async {
  final api = KaedeApiClient(
    vault: const SessionVault(),
    httpClient: _cannedClient(guild, user, requestLog),
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
    final controller = await fixtureController(
        fixtureGuild(user), user, <Map<String, Object?>>[]);

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
    expect(find.text('DEVICES'), findsOneWidget);
    expect(find.text('Log out'), findsOneWidget);
    expect(find.text('Open-source licences'), findsOneWidget);
    expect(find.text('Kaede'), findsWidgets);

    // Sessions load through the repository and render as flat rows.
    expect(find.text('Kaede Desktop'), findsOneWidget);
    expect(find.text('Pixel 9 (Android)'), findsOneWidget);

    // Tapping the direct-message toggle saves the notification preference.
    await tester.tap(find.text('Direct messages'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
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
    await tester.pageBack();
    await tester.pumpAndSettle();
  });

  testWidgets('guild audit renders readable, expandable event details',
      (tester) async {
    tester.view.physicalSize = const Size(840, 2400);
    tester.view.devicePixelRatio = 2.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final user = fixtureUser();
    final guild = fixtureGuild(user);
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
    await tester.tap(find.text('Audit'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.text('All members'), findsOneWidget);
    expect(find.text('All actions'), findsOneWidget);
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
    expect(find.textContaining('Keep the welcome channel calm'), findsOneWidget);
    expect(find.text('CHANGES'), findsOneWidget);
    expect(find.textContaining('Rate limit per user:'), findsOneWidget);
  });
}
