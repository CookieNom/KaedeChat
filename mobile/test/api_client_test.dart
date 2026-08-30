import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/network_json.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

void main() {
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

  test(
      'nested network object arrays reject scalar children without partial data',
      () {
    expect(
      () => strictNetworkObjectList(<Object?>[
        <String, Object?>{'id': '1'},
        'silently dropped before this regression',
      ], label: 'Users'),
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
  _JsonAdapter(this.body, {this.status = 200});

  final String body;
  final int status;
  RequestOptions? request;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    request = options;
    return ResponseBody.fromString(
      body,
      status,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
