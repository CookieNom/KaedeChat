import 'dart:async';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('session refresh', () {
    const vault = SessionVault();
    final original = SessionTokens(
      instance: Domain('chat.example'),
      userRef: EntityRef.parse('1@chat.example'),
      accessToken: 'old-access',
      refreshToken: 'old-refresh',
    );

    setUp(() => FlutterSecureStorage.setMockInitialValues({}));

    Dio refreshClient(_JsonAdapter adapter) => Dio(BaseOptions(
          validateStatus: (status) => status != null && status < 500,
        ))
          ..httpClientAdapter = adapter;

    test('app and notification clients rotate a shared token only once',
        () async {
      var requests = 0;
      final started = Completer<void>();
      final response = Completer<ResponseBody>();
      final adapter = _JsonAdapter('', respond: (_) {
        requests++;
        started.complete();
        return response.future;
      });
      final app =
          KaedeApiClient(vault: vault, refreshClient: refreshClient(adapter));
      final notification =
          KaedeApiClient(vault: vault, refreshClient: refreshClient(adapter));
      await app.useTokens(original);
      await notification.restore();

      final first = notification.refreshTokens();
      await started.future;
      final second = app.refreshTokens();
      response.complete(_response(
          '{"access_token":"new-access","refresh_token":"new-refresh"}'));
      expect((await first).refreshToken, 'new-refresh');
      expect((await second).refreshToken, 'new-refresh');
      expect(app.tokens?.accessToken, 'new-access');
      expect((await vault.read())?.refreshToken, 'new-refresh');
      expect(requests, 1);
    });

    for (final separateClient in [false, true]) {
      test(
          'old refresh cannot sign out a newer login (separate client: $separateClient)',
          () async {
        final started = Completer<void>();
        final response = Completer<ResponseBody>();
        final api = KaedeApiClient(
          vault: vault,
          refreshClient: refreshClient(_JsonAdapter('', respond: (_) {
            started.complete();
            return response.future;
          })),
        );
        await api.useTokens(original);
        var expired = false;
        final subscription = api.sessionExpired.listen((_) => expired = true);
        addTearDown(subscription.cancel);
        final pending = expectLater(
          api.refreshTokens(),
          throwsA(isA<KaedeException>()
              .having((error) => error.code, 'code', 'SESSION_CHANGED')),
        );
        await started.future;
        final login = separateClient ? KaedeApiClient(vault: vault) : api;
        await login.useTokens(original.copyWith(
            accessToken: 'login-access', refreshToken: 'login-refresh'));
        response.complete(_response(
            '{"detail":{"code":"INVALID_REFRESH_TOKEN"}}',
            status: 401));
        await pending;
        await Future<void>.delayed(Duration.zero);
        expect(expired, isFalse);
        expect(login.tokens?.refreshToken, 'login-refresh');
        expect((await vault.read())?.refreshToken, 'login-refresh');
      });
    }

    test('delayed access-token rejection reuses the already refreshed token',
        () async {
      final started = Completer<void>();
      final oldResponse = Completer<ResponseBody>();
      var requests = 0;
      var refreshes = 0;
      final api = KaedeApiClient(
        vault: vault,
        httpClient: Dio()
          ..httpClientAdapter = _JsonAdapter('', respond: (request) {
            if (++requests == 1) {
              expect(request.headers['Authorization'], 'Bearer old-access');
              started.complete();
              return oldResponse.future;
            }
            expect(request.headers['Authorization'], 'Bearer new-access');
            return Future.value(_response('{}'));
          }),
        refreshClient: refreshClient(_JsonAdapter('', respond: (_) async {
          refreshes++;
          return _response(
              '{"access_token":"new-access","refresh_token":"new-refresh"}');
        })),
      );
      await api.useTokens(original);
      final request = api.getJson('/api/v1/users/@me');
      await started.future;
      await api.refreshTokens();
      oldResponse.complete(_response('{}', status: 401));
      expect(await request, isEmpty);
      expect(requests, 2);
      expect(refreshes, 1);
    });

    for (final status in [400, 401, 503]) {
      test('only a rejected refresh token ($status) expires the session',
          () async {
        final api = KaedeApiClient(
          vault: vault,
          refreshClient: refreshClient(_JsonAdapter(
              '{"detail":{"code":"INVALID_REFRESH_TOKEN"}}',
              status: status)),
        );
        await api.useTokens(original);
        final other = KaedeApiClient(vault: vault);
        await other.restore();
        var expired = false;
        final subscription = api.sessionExpired.listen((_) => expired = true);
        addTearDown(subscription.cancel);
        await expectLater(api.refreshTokens(), throwsA(isA<KaedeException>()));
        await Future<void>.delayed(Duration.zero);
        expect(expired, status == 401);
        expect(api.signedIn, status != 401);
        expect(await vault.read(), status == 401 ? isNull : isNotNull);
        if (status == 401) {
          var otherExpired = false;
          final otherSubscription =
              other.sessionExpired.listen((_) => otherExpired = true);
          addTearDown(otherSubscription.cancel);
          await expectLater(
            other.refreshTokens(),
            throwsA(isA<KaedeException>()
                .having((error) => error.code, 'code', 'SESSION_EXPIRED')),
          );
          await Future<void>.delayed(Duration.zero);
          expect(otherExpired, isTrue);
          expect(other.signedIn, isFalse);
        }
      });
    }
  });

  test('network JSON strips recursive client-only decryption state', () async {
    final adapter = _JsonAdapter(
      '{"id":"1","mention_role_refs":[{"id":"3","origin_domain":"chat.example"}],'
      '"mention_everyone":true,"e2ee_verified":true,'
      '"decrypted_content":"peer-injected plaintext",'
      '"attachments":[{"id":"2","encrypted_manifest":{"key":"secret"}}],'
      '"nested":{"decrypted_attachments":[{"key":"secret"}]}}',
    );
    final api = KaedeApiClient(
      vault: const SessionVault(),
      httpClient: Dio()..httpClientAdapter = adapter,
    );

    expect(await api.getJson('/api/v1/channels/1/messages'), <String, Object?>{
      'id': '1',
      'mention_role_refs': <Object?>[
        <String, Object?>{'id': '3', 'origin_domain': 'chat.example'},
      ],
      'mention_everyone': true,
      'attachments': <Object?>[
        <String, Object?>{'id': '2'},
      ],
      'nested': <String, Object?>{},
    });
  });

  test('network JSON rejects scalar entries in object arrays', () async {
    final adapter = _JsonAdapter('[{"id":"1"},"malformed"]');
    final api = KaedeApiClient(
      vault: const SessionVault(),
      httpClient: Dio()..httpClientAdapter = adapter,
    );

    await expectLater(
      api.getList('/api/v1/channels'),
      throwsA(isA<FormatException>()),
    );
  });

  test('channel reorder accepts the API empty response', () async {
    final adapter = _JsonAdapter('', status: 204);
    final repository = KaedeRepository(KaedeApiClient(
      vault: const SessionVault(),
      httpClient: Dio()..httpClientAdapter = adapter,
    ));

    await repository.reorderChannels(
      EntityRef.parse('1@chat.example'),
      <Map<String, Object?>>[
        <String, Object?>{'id': '2', 'position': 0, 'parent_id': null},
      ],
    );

    expect(adapter.request?.data, <String, Object?>{
      'channels': [
        <String, Object?>{'id': '2', 'position': 0, 'parent_id': null},
      ]
    });
    expect(adapter.request?.method, 'PATCH');
    expect(adapter.request?.path, '/api/v1/guilds/1@chat.example/channels');
  });

  test('role reorder accepts the API array response', () async {
    final adapter = _JsonAdapter('[{"id":"2"}]');
    final repository = KaedeRepository(KaedeApiClient(
      vault: const SessionVault(),
      httpClient: Dio()..httpClientAdapter = adapter,
    ));
    final role = KaedeRole(
      ref: EntityRef.parse('2@chat.example'),
      guildRef: EntityRef.parse('1@chat.example'),
      name: 'Role',
      position: 1,
      permissions: BigInt.zero,
      color: 0,
      hoist: false,
      mentionable: false,
      version: 'v1',
    );

    await repository.reorderRoles(
      EntityRef.parse('1@chat.example'),
      <KaedeRole>[role],
    );

    expect(adapter.request?.data, <String, Object?>{
      'roles': [
        <String, Object?>{'id': '2', 'position': 1, 'version': 'v1'},
      ]
    });
    expect(adapter.request?.method, 'PATCH');
    expect(adapter.request?.path, '/api/v1/guilds/1@chat.example/roles');
  });

  test('bot-DM commands submit exact discovery capability lineage', () async {
    final adapter = _JsonAdapter('{}');
    final repository = KaedeRepository(KaedeApiClient(
      vault: const SessionVault(),
      httpClient: Dio()..httpClientAdapter = adapter,
    ));

    await repository.invokeApplicationCommand(
      channel: EntityRef.parse('1@chat.example'),
      application: EntityRef.parse('2@apps.example'),
      commandId: '3',
      integrationType: 'dm_capability',
      dmCapabilityId: 'kbdg_${List.filled(43, 'a').join()}',
      dmCapabilityRevision: '7',
      name: 'inspect',
      type: 'chat_input',
    );

    expect(adapter.request?.data,
        containsPair('integration_type', 'dm_capability'));
    expect(
      adapter.request?.data,
      containsPair('dm_capability_id', 'kbdg_${List.filled(43, 'a').join()}'),
    );
    expect(adapter.request?.data, containsPair('dm_capability_revision', '7'));

    expect(
      () => repository.invokeApplicationCommand(
        channel: EntityRef.parse('1@chat.example'),
        application: EntityRef.parse('2@apps.example'),
        commandId: '3',
        integrationType: 'dm_capability',
        dmCapabilityId: 'kbdg_${List.filled(43, 'a').join()}',
        name: 'inspect',
        type: 'chat_input',
      ),
      throwsArgumentError,
    );
  });
}

final class _JsonAdapter implements HttpClientAdapter {
  _JsonAdapter(this.body, {this.status = 200, this.respond});

  final String body;
  final int status;
  final Future<ResponseBody> Function(RequestOptions)? respond;
  RequestOptions? request;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    request = options;
    return respond == null
        ? _response(body, status: status)
        : respond!(options);
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _response(String body, {int status = 200}) =>
    ResponseBody.fromString(body, status, headers: <String, List<String>>{
      Headers.contentTypeHeader: <String>[Headers.jsonContentType],
    });
