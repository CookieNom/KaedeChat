import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';

void main() {
  test('mobile command permissions use qualified authority routes and payloads',
      () async {
    final scope = <String, Object?>{
      'id': '20@apps.example',
      'application_ref': '20@apps.example',
      'application_name': 'Tasks',
      'guild_ref': '30@guilds.example',
      'command': null,
      'command_ref': null,
      'synced': false,
      'permissions': <Object?>[
        <String, Object?>{
          'id': '30@guilds.example',
          'type': 'role',
          'permission': false,
        },
      ],
    };
    final adapter = _PermissionAdapter(<String>[
      jsonEncode(<Object?>[scope]),
      jsonEncode(<String, Object?>{
        ...scope,
        'permissions': <Object?>[
          <String, Object?>{
            'id': '31@guilds.example',
            'type': 'channel',
            'permission': true,
          },
        ],
      }),
    ]);
    final repository = KaedeRepository(KaedeApiClient(
      vault: const SessionVault(),
      httpClient: Dio()..httpClientAdapter = adapter,
    ));
    final application = EntityRef.parse('20@apps.example');
    final guild = EntityRef.parse('30@guilds.example');

    final scopes =
        await repository.applicationCommandPermissions(application, guild);
    final updated = await repository.updateApplicationCommandPermissions(
      application,
      guild,
      scopes.single.id,
      [scopes.single.permissions.single.copyWith(permission: true)],
    );

    expect(scopes.single.applicationName, 'Tasks');
    expect(scopes.single.permissions.single.permission, isFalse);
    expect(updated.permissions.single.type, 'channel');
    expect(adapter.requests.map((request) => request.method), ['GET', 'PUT']);
    expect(
      adapter.requests.map((request) => request.path),
      [
        '/api/v1/applications/20@apps.example/guilds/30@guilds.example/commands/permissions',
        '/api/v1/applications/20@apps.example/guilds/30@guilds.example/commands/20@apps.example/permissions',
      ],
    );
    expect(adapter.requests.last.data, <String, Object?>{
      'permissions': <Object?>[
        <String, Object?>{
          'id': '30@guilds.example',
          'type': 'role',
          'permission': true,
        },
      ],
    });
  });
}

final class _PermissionAdapter implements HttpClientAdapter {
  _PermissionAdapter(this.responses);

  final List<String> responses;
  final List<RequestOptions> requests = <RequestOptions>[];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    return ResponseBody.fromString(
      responses.removeAt(0),
      200,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
