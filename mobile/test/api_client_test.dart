import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

void main() {
  test('channel reorder accepts the API array response', () async {
    final adapter = _JsonAdapter('[{"id":"2"}]');
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
}

final class _JsonAdapter implements HttpClientAdapter {
  _JsonAdapter(this.body);

  final String body;
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
      200,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
