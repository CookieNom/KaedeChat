import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_commands.dart';
import 'package:kaede_mobile/src/domain/application_directory.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/application_launcher.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';

void main() {
  group('strict mobile Application Directory contract', () {
    test('parses reviewed apps and produces a review-first install route', () {
      final page = MobileDirectoryPage.fromJson(
        _pageJson(items: <Map<String, Object?>>[_applicationJson()]),
        expectedOrigin: Domain('home.example'),
        expectedCollection: null,
      );

      expect(page.items.single.ref.wire, '20@home.example');
      expect(page.items.single.installTemplate.slug, 'standard');
      expect(
        mobileApplicationInstallPath(
          page.items.single,
          Domain('home.example'),
        ),
        '/applications/20%40home.example/install/standard?instance=home.example',
      );
    });

    test('rejects extra fields, foreign lineage, and inconsistent templates',
        () {
      final extra = _applicationJson()..['untrusted_url'] = 'https://evil.test';
      final foreign = _applicationJson(domain: 'evil.test');
      final template = Map<String, Object?>.from(
        _applicationJson()['install_template']! as Map,
      )..['install_types'] = <String>['guild_install'];
      final inconsistent = _applicationJson()..['install_template'] = template;

      for (final application in <Map<String, Object?>>[
        extra,
        foreign,
        inconsistent,
      ]) {
        expect(
          () => MobileDirectoryApplication.fromJson(
            application,
            expectedOrigin: Domain('home.example'),
          ),
          throwsA(isA<FormatException>()),
        );
      }
    });

    test('bot profile application is exact and bound to the requested bot', () {
      final expectedBot = EntityRef.parse('30@apps.example');
      final profile = MobileBotProfileApplication.fromJson(
        _botProfileJson(),
        expectedBot: expectedBot,
      );

      expect(profile.bot, expectedBot);
      expect(profile.application.wire, '20@apps.example');
      expect(
        mobileBotApplicationInstallPath(profile, Domain('home.example')),
        '/applications/20%40apps.example/install/standard?instance=home.example',
      );
      expect(
        () => MobileBotProfileApplication.fromJson(
          _botProfileJson()..['bot_ref'] = '31@apps.example',
          expectedBot: expectedBot,
        ),
        throwsA(isA<FormatException>()),
      );
    });
  });

  group('launcher ranking and request helpers', () {
    test('recents are bounded, canonical, and scoped by composite account', () {
      final application = EntityRef.parse('20@apps.example');
      var history = <String>['invalid', application.wire, application.wire];
      for (var index = 21; index < 50; index += 1) {
        history = mobileRememberRecentApplication(
          history,
          EntityRef.parse('$index@apps.example'),
        );
      }

      expect(history, hasLength(20));
      expect(mobileRecentApplicationRefs(history), hasLength(20));
      expect(
        mobileAppRecentStorageKey(EntityRef.parse('1@users.example')),
        isNot(
          mobileAppRecentStorageKey(EntityRef.parse('2@users.example')),
        ),
      );
    });

    test('curated sections remove duplicates with stable priority', () {
      final first = MobileDirectoryApplication.fromJson(
        _applicationJson(),
        expectedOrigin: Domain('home.example'),
      );
      final second = MobileDirectoryApplication.fromJson(
        _applicationJson(id: '21', name: 'Second App'),
        expectedOrigin: Domain('home.example'),
      );
      final sections = mobileUniqueDirectorySections(
        <String, Iterable<MobileDirectoryApplication>>{
          'featured': <MobileDirectoryApplication>[first, second],
          'staff-picks': <MobileDirectoryApplication>[first, second],
        },
        excluded: <EntityRef>[first.ref],
      );

      expect(sections['featured'], <MobileDirectoryApplication>[second]);
      expect(sections['staff-picks'], isEmpty);
      expect(
        mobileDirectoryResponseIsCurrent(
          requestGeneration: 3,
          currentGeneration: 3,
          requestQuery: 'weather',
          currentQuery: ' weather ',
        ),
        isTrue,
      );
      expect(
        mobileDirectoryResponseIsCurrent(
          requestGeneration: 2,
          currentGeneration: 3,
          requestQuery: 'old',
          currentQuery: 'new',
        ),
        isFalse,
      );
    });
  });

  group('Application Directory repository', () {
    test('searches the selected catalog authority with bounded parameters',
        () async {
      final adapter = _DirectoryAdapter(<_Reply>[
        _Reply(jsonEncode(_pageJson(
          items: <Map<String, Object?>>[
            _applicationJson(domain: 'directory.example'),
          ],
          selectedCollection: 'featured',
        ))),
      ]);
      final api = KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      )..selectInstance(Domain('home.example'));
      final repository = KaedeRepository(api);

      final page = await repository.applicationDirectory(
        query: 'weather',
        collection: 'featured',
        domain: Domain('directory.example'),
        limit: 12,
      );

      expect(page.items.single.name, 'Weather App');
      expect(adapter.requests.single.method, 'GET');
      expect(
        adapter.requests.single.path,
        '/api/v1/application-directory',
      );
      expect(adapter.requests.single.queryParameters, <String, Object?>{
        'q': 'weather',
        'collection': 'featured',
        'domain': 'directory.example',
        'limit': 12,
      });
    });

    test('bot lookup is request-bound and treats 404 as no action', () async {
      final adapter = _DirectoryAdapter(<_Reply>[
        _Reply(jsonEncode(_botProfileJson())),
        const _Reply(
          '{"detail":{"code":"APPLICATION_NOT_FOUND"}}',
          status: 404,
        ),
      ]);
      final api = KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      )..selectInstance(Domain('home.example'));
      final repository = KaedeRepository(api);
      final bot = EntityRef.parse('30@apps.example');

      expect((await repository.botProfileApplication(bot))?.bot, bot);
      expect(await repository.botProfileApplication(bot), isNull);
      expect(
        adapter.requests.first.path,
        '/api/v1/application-directory/bot-profiles/30%40apps.example',
      );
    });
  });

  group('native App Launcher', () {
    testWidgets('opens with zero commands and routes a reviewed app to install',
        (tester) async {
      MobileAppLauncherSelection? selected;
      await tester.pumpWidget(
        _LauncherHarness(
          commands: const <MobileApplicationCommand>[],
          loadDirectory: ({
            query,
            collection,
            domain,
            required limit,
          }) async =>
              MobileDirectoryPage.fromJson(
            _pageJson(
              items: <Map<String, Object?>>[
                if (collection == 'featured') _applicationJson(),
              ],
              selectedCollection: collection,
            ),
            expectedOrigin: Domain('home.example'),
            expectedCollection: collection,
            requestedLimit: limit,
          ),
          onSelected: (value) => selected = value,
        ),
      );

      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();

      expect(find.text('Featured'), findsOneWidget);
      expect(
        find.byKey(const ValueKey('directory-card-20@home.example')),
        findsOneWidget,
      );
      await tester.tap(
        find.byKey(const ValueKey('directory-card-20@home.example')),
      );
      await tester.pumpAndSettle();

      expect(selected, isA<MobileAppLauncherInstallSelection>());
    });

    testWidgets('stale debounced searches cannot replace the latest result',
        (tester) async {
      final searches = <String, Completer<MobileDirectoryPage>>{};
      await tester.pumpWidget(
        _LauncherHarness(
          commands: const <MobileApplicationCommand>[],
          debounce: Duration.zero,
          loadDirectory: ({
            query,
            collection,
            domain,
            required limit,
          }) {
            if (collection != null) {
              return Future<MobileDirectoryPage>.value(
                MobileDirectoryPage.fromJson(
                  _pageJson(selectedCollection: collection),
                  expectedOrigin: Domain('home.example'),
                  expectedCollection: collection,
                  requestedLimit: limit,
                ),
              );
            }
            return (searches[query!] ??= Completer<MobileDirectoryPage>())
                .future;
          },
        ),
      );
      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();
      final search = find.byKey(const ValueKey('app-launcher-search'));

      await tester.enterText(search, 'a');
      await tester.pump();
      await tester.enterText(search, 'ab');
      await tester.pump();
      searches['ab']!.complete(
        MobileDirectoryPage.fromJson(
          _pageJson(
            items: <Map<String, Object?>>[
              _applicationJson(id: '22', name: 'Newest App'),
            ],
          ),
          expectedOrigin: Domain('home.example'),
          expectedCollection: null,
        ),
      );
      await tester.pump();
      searches['a']!.complete(
        MobileDirectoryPage.fromJson(
          _pageJson(
            items: <Map<String, Object?>>[
              _applicationJson(id: '21', name: 'Stale App'),
            ],
          ),
          expectedOrigin: Domain('home.example'),
          expectedCollection: null,
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Newest App'), findsOneWidget);
      expect(find.text('Stale App'), findsNothing);
    });

    testWidgets('discovery failure leaves installed command selection usable',
        (tester) async {
      MobileAppLauncherSelection? selected;
      final command = _command();
      await tester.pumpWidget(
        _LauncherHarness(
          commands: <MobileApplicationCommand>[command],
          loadDirectory: ({
            query,
            collection,
            domain,
            required limit,
          }) async =>
              throw StateError('offline'),
          onSelected: (value) => selected = value,
        ),
      );

      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();
      expect(find.text('/forecast'), findsOneWidget);
      expect(find.textContaining('recommendations could not be loaded'),
          findsOneWidget);
      await tester.tap(find.text('/forecast'));
      await tester.pumpAndSettle();

      expect(selected, isA<MobileAppLauncherCommandSelection>());
    });

    testWidgets('zero-command recent and search rows open attested app review',
        (tester) async {
      MobileAppLauncherSelection? selected;
      var lookups = 0;
      await tester.pumpWidget(
        _LauncherHarness(
          commands: const <MobileApplicationCommand>[],
          loadInstalledApplications: () async => <UserApplicationInstallation>[
            _installation(),
          ],
          loadBotProfileApplication: (bot) async {
            lookups += 1;
            return MobileBotProfileApplication.fromJson(
              _botProfileJson(),
              expectedBot: bot,
            );
          },
          loadDirectory: ({
            query,
            collection,
            domain,
            required limit,
          }) async =>
              MobileDirectoryPage.fromJson(
            _pageJson(selectedCollection: collection),
            expectedOrigin: domain!,
            expectedCollection: collection,
            requestedLimit: limit,
          ),
          onSelected: (value) => selected = value,
        ),
      );

      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const ValueKey('recent-app-20@apps.example')),
      );
      await tester.pumpAndSettle();

      expect(selected, isA<MobileAppLauncherBotInstallSelection>());
      expect(lookups, 1);

      selected = null;
      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('app-launcher-search')),
        'Weather',
      );
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const ValueKey('installed-app-20@apps.example')),
      );
      await tester.pumpAndSettle();

      expect(selected, isA<MobileAppLauncherBotInstallSelection>());
      expect(lookups, 2);
    });

    testWidgets('failed explicit app review is visible but non-fatal',
        (tester) async {
      await tester.pumpWidget(
        _LauncherHarness(
          commands: const <MobileApplicationCommand>[],
          loadInstalledApplications: () async => <UserApplicationInstallation>[
            _installation(),
          ],
          loadBotProfileApplication: (_) async => null,
          loadDirectory: ({
            query,
            collection,
            domain,
            required limit,
          }) async =>
              MobileDirectoryPage.fromJson(
            _pageJson(selectedCollection: collection),
            expectedOrigin: domain!,
            expectedCollection: collection,
            requestedLimit: limit,
          ),
        ),
      );

      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const ValueKey('recent-app-20@apps.example')),
      );
      await tester.pumpAndSettle();

      expect(find.text('App details are unavailable.'), findsOneWidget);
      expect(
        find.byKey(const ValueKey('app-launcher-search')),
        findsOneWidget,
      );
    });

    testWidgets(
        'app review result is dropped after the account context changes',
        (tester) async {
      final lookup = Completer<MobileBotProfileApplication?>();
      MobileAppLauncherSelection? selected;
      var current = true;
      await tester.pumpWidget(
        _LauncherHarness(
          commands: const <MobileApplicationCommand>[],
          isAccountCurrent: (_) => current,
          loadInstalledApplications: () async => <UserApplicationInstallation>[
            _installation(),
          ],
          loadBotProfileApplication: (_) => lookup.future,
          loadDirectory: ({
            query,
            collection,
            domain,
            required limit,
          }) async =>
              MobileDirectoryPage.fromJson(
            _pageJson(selectedCollection: collection),
            expectedOrigin: domain!,
            expectedCollection: collection,
            requestedLimit: limit,
          ),
          onSelected: (value) => selected = value,
        ),
      );

      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const ValueKey('recent-app-20@apps.example')),
      );
      await tester.pump();
      current = false;
      lookup.complete(
        MobileBotProfileApplication.fromJson(
          _botProfileJson(),
          expectedBot: EntityRef.parse('30@apps.example'),
        ),
      );
      await tester.pump();

      expect(selected, isNull);
      expect(find.text('App details are unavailable.'), findsNothing);
    });

    testWidgets('switches to a canonical federated Directory authority',
        (tester) async {
      final requestedDomains = <Domain>[];
      await tester.pumpWidget(
        _LauncherHarness(
          commands: const <MobileApplicationCommand>[],
          loadDirectory: ({
            query,
            collection,
            domain,
            required limit,
          }) async {
            final authority = domain!;
            requestedDomains.add(authority);
            return MobileDirectoryPage.fromJson(
              _pageJson(
                items: <Map<String, Object?>>[
                  if (collection == 'featured')
                    _applicationJson(domain: authority.value),
                ],
                selectedCollection: collection,
              ),
              expectedOrigin: authority,
              expectedCollection: collection,
              requestedLimit: limit,
            );
          },
        ),
      );

      await tester.tap(find.text('Open Apps'));
      await tester.pumpAndSettle();
      expect(requestedDomains, everyElement(Domain('home.example')));

      await tester.tap(
        find.byKey(const ValueKey('app-launcher-directory-instance')),
      );
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(
          const ValueKey('app-launcher-directory-domain-input'),
        ),
        'DIRECTORY.EXAMPLE.',
      );
      await tester.tap(
        find.byKey(const ValueKey('app-launcher-directory-apply')),
      );
      await tester.pumpAndSettle();

      expect(find.text('Directory: directory.example'), findsOneWidget);
      expect(
        requestedDomains.where((item) => item == Domain('directory.example')),
        hasLength(3),
      );
      expect(
        find.byKey(
          const ValueKey('directory-card-20@directory.example'),
        ),
        findsOneWidget,
      );
    });
  });

  testWidgets('bot profile adds only a server-attested application action',
      (tester) async {
    MobileBotProfileApplication? selected;
    final bot = KaedeUser(
      ref: EntityRef.parse('30@apps.example'),
      username: 'weather',
      handle: 'weather@apps.example',
      accountType: AccountType.bot,
    );
    final profile = MobileBotProfileApplication.fromJson(
      _botProfileJson(),
      expectedBot: bot.ref,
    );
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: UserProfileSheet(
            user: bot,
            presence: PresenceStatus.online,
            applicationLookup: (requested) async {
              expect(requested, bot.ref);
              return profile;
            },
            onAddApplication: (application) => selected = application,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Add App'), findsOneWidget);
    expect(userProfileSupportsFriendshipActions(bot), isFalse);
    expect(find.text('Send friend request'), findsNothing);
    await tester.tap(find.text('Add App'));
    expect(selected, profile);
  });
}

final class _LauncherHarness extends StatefulWidget {
  const _LauncherHarness({
    required this.commands,
    required this.loadDirectory,
    this.onSelected,
    this.debounce = const Duration(milliseconds: 300),
    this.loadInstalledApplications,
    this.loadBotProfileApplication,
    this.isAccountCurrent,
  });

  final List<MobileApplicationCommand> commands;
  final MobileDirectoryLoader loadDirectory;
  final ValueChanged<MobileAppLauncherSelection>? onSelected;
  final Duration debounce;
  final Future<List<UserApplicationInstallation>> Function()?
      loadInstalledApplications;
  final MobileBotProfileApplicationLoader? loadBotProfileApplication;
  final bool Function(EntityRef account)? isAccountCurrent;

  @override
  State<_LauncherHarness> createState() => _LauncherHarnessState();
}

final class _LauncherHarnessState extends State<_LauncherHarness> {
  Future<void> _open(BuildContext sheetContext) async {
    final selected = await showModalBottomSheet<MobileAppLauncherSelection>(
      context: sheetContext,
      isScrollControlled: true,
      builder: (context) => MobileApplicationLauncherSheet(
        commands: widget.commands,
        account: EntityRef.parse('1@users.example'),
        home: Domain('home.example'),
        isAccountCurrent: widget.isAccountCurrent ?? (_) => true,
        loadInstalledApplications: widget.loadInstalledApplications ??
            () async => const <UserApplicationInstallation>[],
        loadRecentApplications: () async => const <String>[],
        loadDirectory: widget.loadDirectory,
        loadBotProfileApplication:
            widget.loadBotProfileApplication ?? (_) async => null,
        searchDebounce: widget.debounce,
      ),
    );
    if (selected != null) widget.onSelected?.call(selected);
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
        home: Builder(
          builder: (sheetContext) => Scaffold(
            body: Center(
              child: FilledButton(
                onPressed: () => _open(sheetContext),
                child: const Text('Open Apps'),
              ),
            ),
          ),
        ),
      );
}

MobileApplicationCommand _command() => MobileApplicationCommand(
      id: '90',
      application: EntityRef.parse('20@home.example'),
      applicationName: 'Weather App',
      name: 'forecast',
      type: 'chat_input',
      description: 'Show the forecast',
      integrationType: 'user_install',
      interactionContext: 'private_channel',
    );

UserApplicationInstallation _installation() => UserApplicationInstallation(
      id: '40',
      application: EntityRef.parse('20@apps.example'),
      applicationName: 'Weather App',
      applicationDescription: 'Forecasts and severe weather alerts.',
      botUser: EntityRef.parse('30@apps.example'),
      user: EntityRef.parse('1@users.example'),
      scopes: const <String>['applications.commands'],
      intents: const <String>['interactions'],
      contexts: const <String>['private_channel'],
      e2eeParticipantCapable: false,
      grantRevision: '1',
      status: 'active',
    );

Map<String, Object?> _applicationJson({
  String id = '20',
  String name = 'Weather App',
  String domain = 'home.example',
}) =>
    <String, Object?>{
      'id': id,
      'ref': '$id@$domain',
      'origin_domain': domain,
      'name': name,
      'summary': 'Forecasts and severe weather alerts.',
      'category': 'utilities',
      'tags': <String>['weather'],
      'collections': <String>['featured'],
      'icon_hash': null,
      'banner_hash': null,
      'verified': true,
      'install_template': _templateJson(),
      'user_install_supported': true,
    };

Map<String, Object?> _botProfileJson() => <String, Object?>{
      'bot_ref': '30@apps.example',
      'application_ref': '20@apps.example',
      'origin_domain': 'apps.example',
      'name': 'Weather App',
      'install_template': _templateJson(),
      'directory_listed': true,
    };

Map<String, Object?> _templateJson() => <String, Object?>{
      'slug': 'standard',
      'name': 'Install Weather',
      'description': null,
      'install_types': <String>['guild_install', 'user_install'],
      'default_install_type': 'guild_install',
    };

Map<String, Object?> _pageJson({
  List<Map<String, Object?>> items = const <Map<String, Object?>>[],
  String? selectedCollection,
}) =>
    <String, Object?>{
      'items': items,
      'next_cursor': null,
      'collections': <Map<String, Object?>>[
        <String, Object?>{
          'slug': 'featured',
          'name': 'Featured',
          'description': 'Reviewed apps selected by this instance.',
        },
        <String, Object?>{
          'slug': 'staff-picks',
          'name': 'Staff Picks',
          'description': 'Apps recommended by local staff.',
        },
        <String, Object?>{
          'slug': 'new-and-noteworthy',
          'name': 'New & Noteworthy',
          'description': 'Recently highlighted reviewed apps.',
        },
      ],
      'selected_collection': selectedCollection,
    };

final class _Reply {
  const _Reply(this.body, {this.status = 200});

  final String body;
  final int status;
}

final class _DirectoryAdapter implements HttpClientAdapter {
  _DirectoryAdapter(this._replies);

  final List<_Reply> _replies;
  final List<RequestOptions> requests = <RequestOptions>[];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final reply = _replies.removeAt(0);
    return ResponseBody.fromString(
      reply.body,
      reply.status,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
