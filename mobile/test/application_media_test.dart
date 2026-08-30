import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/application_media_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_media.dart';
import 'package:kaede_mobile/src/features/settings/application_media_screen.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('application media models parse typed fields and serialize drafts', () {
    final application = DeveloperApplication.fromJson(_applicationJson());
    final asset = ApplicationAsset.fromJson(_assetJson());
    final emoji = ApplicationEmoji.fromJson(_emojiJson());

    expect(application.ref.wire, '10@apps.example');
    expect(application.name, 'Weather');
    expect(asset.applicationRef, application.ref);
    expect(asset.kind, ApplicationAssetKind.cover);
    expect(asset.dimensions, '1200 × 600');
    expect(emoji.name, 'sunny');
    expect(emoji.available, isTrue);

    const assetDraft = ApplicationAssetDraft(
      name: '  launch cover  ',
      kind: ApplicationAssetKind.store,
    );
    expect(assetDraft.createPayload('88'), <String, Object?>{
      'attachment_id': '88',
      'kind': 'store',
      'name': 'launch cover',
    });
    expect(assetDraft.patchPayload, <String, Object?>{
      'kind': 'store',
      'name': 'launch cover',
    });

    const emojiDraft = ApplicationEmojiDraft(name: 'sunny_day');
    expect(emojiDraft.createPayload('89'), <String, Object?>{
      'attachment_id': '89',
      'name': 'sunny_day',
    });
    expect(emojiDraft.patchPayload, <String, Object?>{'name': 'sunny_day'});
  });

  test('application media validation gives actionable local errors', () {
    expect(
      const ApplicationAssetDraft(
        name: '   ',
        kind: ApplicationAssetKind.icon,
      ).validationMessage,
      'Enter an asset name.',
    );
    expect(
      applicationEmojiNameValidation('not valid!'),
      'Emoji names use 2–32 letters, numbers, or underscores.',
    );
    expect(applicationEmojiNameValidation('valid_name'), isNull);
    expect(
      applicationImageValidation(
        filename: 'photo.bmp',
        contentType: null,
        size: 12,
      ),
      'Choose a PNG, JPEG, GIF, or WebP image.',
    );
    expect(
      applicationImageValidation(
        filename: 'empty.png',
        contentType: 'image/png',
        size: 0,
      ),
      'The selected image is empty.',
    );
  });

  test('repository uses human application media endpoints and exact payloads',
      () async {
    final adapter = _QueueAdapter([
      _Reply(jsonEncode(<Object?>[_applicationJson()])),
      _Reply(jsonEncode(<Object?>[_assetJson()])),
      _Reply(jsonEncode(<Object?>[_emojiJson()])),
      _Reply(jsonEncode(_assetJson(name: 'Launch art', kind: 'activity'))),
      const _Reply('', status: 204),
      _Reply(jsonEncode(_emojiJson(name: 'sunny_day', available: false))),
      const _Reply('', status: 204),
      _Reply(jsonEncode(<String, Object?>{
        'id': '99',
        'upload_url': 'https://uploads.example/object',
      })),
    ]);
    final repository = _repository(adapter);
    final application = EntityRef.parse('10@apps.example');

    expect((await repository.developerApplications()).single.name, 'Weather');
    expect((await repository.applicationAssets(application)).single.name,
        'Launch cover');
    expect(
        (await repository.applicationEmojis(application)).single.name, 'sunny');
    await repository.updateApplicationAsset(
      application,
      Snowflake('20'),
      const ApplicationAssetDraft(
        name: 'Launch art',
        kind: ApplicationAssetKind.activity,
      ),
    );
    await repository.deleteApplicationAsset(application, Snowflake('20'));
    await repository.updateApplicationEmoji(
      application,
      Snowflake('30'),
      const ApplicationEmojiDraft(name: 'sunny_day'),
    );
    await repository.deleteApplicationEmoji(application, Snowflake('30'));
    await repository.createApplicationMediaTicket(
      application: application,
      collection: 'assets',
      filename: 'cover.png',
      contentType: 'image/png',
      size: 4096,
    );

    expect(adapter.requests.map((request) => request.path), [
      '/api/v1/applications',
      '/api/v1/applications/10@apps.example/assets',
      '/api/v1/applications/10@apps.example/emojis',
      '/api/v1/applications/10@apps.example/assets/20',
      '/api/v1/applications/10@apps.example/assets/20',
      '/api/v1/applications/10@apps.example/emojis/30',
      '/api/v1/applications/10@apps.example/emojis/30',
      '/api/v1/applications/10@apps.example/assets/tickets',
    ]);
    expect(adapter.requests[3].method, 'PATCH');
    expect(adapter.requests[3].data, <String, Object?>{
      'kind': 'activity',
      'name': 'Launch art',
    });
    expect(adapter.requests[5].data, <String, Object?>{'name': 'sunny_day'});
    expect(adapter.requests[7].data, <String, Object?>{
      'filename': 'cover.png',
      'content_type': 'image/png',
      'size': 4096,
    });
  });

  test('two-phase processing polls nested status and recommits once clean',
      () async {
    var commits = 0;
    final result = await completeScannedMediaResource<ApplicationAsset>(
      commit: () async {
        commits += 1;
        return commits == 1
            ? <String, Object?>{
                'status': 'processing',
                'attachment': <String, Object?>{'scan_status': 'pending'},
              }
            : _assetJson();
      },
      isComplete: (json) => json['application_ref'] != null,
      parse: ApplicationAsset.fromJson,
      pollInterval: Duration.zero,
      maxPollAttempts: 3,
    );

    expect(result.name, 'Launch cover');
    expect(commits, 2);
  });

  test('synchronous commit returns without polling or duplicate commit',
      () async {
    var commits = 0;
    await completeScannedMediaResource<ApplicationEmoji>(
      commit: () async {
        commits += 1;
        return _emojiJson();
      },
      isComplete: (json) => json['application_ref'] != null,
      parse: ApplicationEmoji.fromJson,
      pollInterval: Duration.zero,
    );
    expect(commits, 1);
  });

  test('two-phase processing stops on safety rejection', () async {
    await expectLater(
      completeScannedMediaResource<ApplicationEmoji>(
        commit: () async => <String, Object?>{
          'status': 'rejected',
          'attachment': <String, Object?>{'scan_status': 'infected'},
        },
        isComplete: (json) => json['application_ref'] != null,
        parse: ApplicationEmoji.fromJson,
        pollInterval: Duration.zero,
      ),
      throwsA(
        isA<KaedeException>()
            .having((error) => error.status, 'status', 422)
            .having(
              (error) => error.message,
              'message',
              contains('did not pass media safety processing'),
            ),
      ),
    );
  });

  testWidgets('application picker exposes owned app and media controls',
      (tester) async {
    final adapter = _QueueAdapter([
      _Reply(jsonEncode(<Object?>[_applicationJson()])),
      _Reply(jsonEncode(<Object?>[
        <String, Object?>{..._assetJson(), 'media_hash': 'preview-fixture'},
      ])),
      _Reply(jsonEncode(<Object?>[
        <String, Object?>{..._emojiJson(), 'media_hash': 'preview-fixture'},
      ])),
    ]);

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: ApplicationMediaScreen(repository: _repository(adapter)),
    ));
    await tester.pumpAndSettle();

    expect(find.text('DEVELOPER APPLICATIONS'), findsOneWidget);
    expect(find.text('Weather'), findsOneWidget);
    await tester.tap(find.text('Weather'));
    await tester.pumpAndSettle();

    expect(find.text('Add asset'), findsOneWidget);
    expect(find.byKey(const Key('application-asset-20')), findsOneWidget);
    expect(find.text('Launch cover'), findsOneWidget);

    await tester.tap(find.text('Emoji'));
    await tester.pumpAndSettle();
    expect(find.text('Add emoji'), findsOneWidget);
    final emojiTile = find.byKey(const Key('application-emoji-30'));
    expect(emojiTile, findsOneWidget);
    expect(
      find.byKey(const Key('application-emoji-availability-30')),
      findsOneWidget,
    );

    await tester.tap(
      find.descendant(of: emojiTile, matching: find.byTooltip('Edit')),
    );
    await tester.pumpAndSettle();
    expect(
      find.byKey(const Key('application-emoji-name-field')),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('application-emoji-availability-field')),
      findsNothing,
    );
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();

    await tester.tap(
      find.descendant(of: emojiTile, matching: find.byTooltip('Delete')),
    );
    await tester.pumpAndSettle();
    expect(find.text('Delete :sunny:?'), findsOneWidget);
    expect(
      find.byKey(const Key('confirm-application-media-delete')),
      findsOneWidget,
    );
    await tester.tap(find.text('Cancel'));
  });

  testWidgets('permission-aware state explains developer team roles',
      (tester) async {
    final adapter = _QueueAdapter([
      const _Reply(
        '{"detail":{"code":"APPLICATION_TEAM_ROLE_REQUIRED","message":"Forbidden"}}',
        status: 403,
      ),
      const _Reply(
        '{"detail":{"code":"APPLICATION_TEAM_ROLE_REQUIRED","message":"Forbidden"}}',
        status: 403,
      ),
    ]);
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: ApplicationMediaManagerScreen(
        application: DeveloperApplication.fromJson(_applicationJson()),
        repository: _repository(adapter),
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Media management is restricted'), findsOneWidget);
    expect(
      find.textContaining('owners, administrators, and developers'),
      findsOneWidget,
    );
  });
}

Map<String, Object?> _applicationJson() => <String, Object?>{
      'id': '10',
      'origin_domain': 'apps.example',
      'ref': '10@apps.example',
      'name': 'Weather',
      'description': 'Forecast commands',
      'icon_hash': null,
      'status': 'active',
    };

Map<String, Object?> _assetJson({
  String name = 'Launch cover',
  String kind = 'cover',
}) =>
    <String, Object?>{
      'id': '20',
      'application_ref': '10@apps.example',
      'kind': kind,
      'name': name,
      'media_hash': 'a' * 64,
      'content_type': 'image/png',
      'width': 1200,
      'height': 600,
      'version': 2,
    };

Map<String, Object?> _emojiJson({
  String name = 'sunny',
  bool available = true,
}) =>
    <String, Object?>{
      'id': '30',
      'application_ref': '10@apps.example',
      'name': name,
      'media_hash': 'b' * 64,
      'animated': false,
      'available': available,
      'version': 3,
    };

KaedeRepository _repository(_QueueAdapter adapter) => KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

final class _Reply {
  const _Reply(this.body, {this.status = 200});

  final String body;
  final int status;
}

final class _QueueAdapter implements HttpClientAdapter {
  _QueueAdapter(this._replies);

  final List<_Reply> _replies;
  final List<RequestOptions> requests = [];

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
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
