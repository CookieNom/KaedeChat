import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/developer_portal_repository.dart';
import 'package:kaede_mobile/src/api/instance_administration_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/user_identity.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/bot_e2ee_participation.dart';
import 'package:kaede_mobile/src/domain/client_preferences.dart';
import 'package:kaede_mobile/src/domain/developer_portal.dart';
import 'package:kaede_mobile/src/domain/instance_administration.dart';
import 'package:kaede_mobile/src/features/settings/administration_attachment_viewer.dart';

void main() {
  test('client appearance preferences parse safe persisted values', () {
    expect(parseThemePreference('light'), KaedeThemePreference.light);
    expect(parseThemePreference('unexpected'), KaedeThemePreference.system);
    expect(parseLocalePreference('ja_JP'), const Locale('ja', 'JP'));
    expect(parseLocalePreference(null), const Locale('en', 'US'));
    expect(
      parseDeveloperMode(const <String, Object?>{'developer_mode': true}),
      isTrue,
    );
    expect(
      parseDeveloperMode(const <String, Object?>{'developer_mode': 'true'}),
      isFalse,
    );
  });

  test('developer and administration models preserve qualified identities', () {
    final application = DeveloperApplicationDetail.fromJson(_application());
    final team = DeveloperTeam.fromJson(<String, Object?>{
      'ref': '5@chat.example',
      'name': 'Platform',
      'personal': false,
      'role': 'administrator',
    });
    final identity = AdministrationIdentity.fromJson(_identity());
    final report = AdministrationReport.fromJson(<String, Object?>{
      'id': '88',
      'source': 'user',
      'severity': 'high',
      'target_type': 'message',
      'target_ref': '77@remote.example',
      'category': 'harassment',
      'description': 'Evidence',
      'status': 'submitted',
      'resolution': null,
      'subject_ref': '7@remote.example',
      'reporter_ref': '8@chat.example',
      'evidence': const <String, Object?>{'server_verified': true},
      'encryption_mode': 'plaintext',
      'created_at': '2026-08-28T10:00:00Z',
    });

    expect(application.ref.wire, '10@apps.example');
    expect(application.supportedInstallTypes,
        containsAll(['guild_install', 'user_install']));
    expect(application.userInstallContexts,
        containsAll(['bot_dm', 'private_channel']));
    expect(team.canManageMembers, isTrue);
    expect(identity.can('reports.read'), isTrue);
    expect(
      AdministrationIdentity.fromJson(<String, Object?>{
        'user': const <String, Object?>{
          'id': '1',
          'origin_domain': 'chat.example',
          'username': 'reader',
        },
        'roles': const ['administrator'],
        'capabilities': const ['admin.read'],
      }).can('reports.read'),
      isFalse,
      reason: 'capability-gated sections must not inherit unrelated grants',
    );
    expect(identity.userRef.wire, '1@chat.example');
    expect(report.subjectRef, '7@remote.example');
    expect(report.evidence['server_verified'], isTrue);
  });

  test('administration applications preserve their remote state authority', () {
    final application = AdministrationApplication.fromJson(<String, Object?>{
      'ref': '10@apps.example',
      'name': 'Tasks',
      'status': 'active',
      'team_ref': '5@apps.example',
      'state_authority': 'apps.example',
      'can_manage_state': false,
      'updated_at': '2026-08-28T10:00:00Z',
    });

    expect(application.ref.wire, '10@apps.example');
    expect(application.stateAuthority.value, 'apps.example');
    expect(application.canManageState, isFalse);
  });

  test('repositories use audited human routes and qualified application refs',
      () async {
    final adapter = _QueueAdapter(<_Reply>[
      _Reply(jsonEncode(<Object?>[
        <String, Object?>{
          'ref': '5@chat.example',
          'name': 'Platform',
          'personal': false,
          'role': 'owner',
        }
      ])),
      _Reply(jsonEncode(_application())),
      const _Reply('{}'),
      _Reply(jsonEncode(_identity())),
      _Reply(jsonEncode(<Object?>[
        <String, Object?>{
          'id': '2',
          'origin_domain': 'chat.example',
          'username': 'member',
          'display_name': 'Member',
          'account_type': 'human',
          'disabled_at': null,
          'suspended_until': null,
          'age_assurance_state': 'adult',
        }
      ])),
      const _Reply('', status: 204),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final application = EntityRef.parse('10@apps.example');

    expect((await repository.developerTeams()).single.name, 'Platform');
    expect((await repository.developerApplication(application)).name, 'Tasks');
    await repository.replaceApplicationCommands(application, const [
      <String, Object?>{
        'name': 'hello',
        'description': 'Say hello',
        'type': 'chat_input',
      }
    ]);
    expect(
        (await repository.administrationIdentity()).roles, contains('owner'));
    expect((await repository.administrationUsers()).single.username, 'member');
    await repository.putAdministrationBlock(
      domain: 'bad.example',
      level: 'suspend',
      includeSubdomains: true,
      reason: 'Abuse source',
    );

    expect(
      adapter.requests.map((request) => request.path),
      [
        '/api/v1/developer-teams',
        '/api/v1/applications/10%40apps.example',
        '/api/v1/applications/10%40apps.example/commands',
        '/api/v1/administration/@me',
        '/api/v1/administration/users',
        '/api/v1/administration/instances/blocks',
      ],
    );
    expect(adapter.requests[2].method, 'PUT');
    expect(adapter.requests[2].data, <String, Object?>{
      'commands': const <Object?>[
        <String, Object?>{
          'name': 'hello',
          'description': 'Say hello',
          'type': 'chat_input',
        }
      ],
    });
    expect(adapter.requests[5].data, <String, Object?>{
      'domain': 'bad.example',
      'level': 'suspend',
      'include_subdomains': true,
      'reason': 'Abuse source',
    });
  });

  test('people pickers share qualified-ref and handle resolution', () async {
    final adapter = _QueueAdapter(<_Reply>[
      _Reply(jsonEncode(<String, Object?>{
        'id': '9',
        'origin_domain': 'remote.example',
        'username': 'member',
        'handle': 'member@remote.example',
      })),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

    expect(
      (await repository.resolveUserIdentity('8@chat.example')).wire,
      '8@chat.example',
    );
    expect(adapter.requests, isEmpty);
    expect(
      (await repository.resolveUserIdentity('member@remote.example')).wire,
      '9@remote.example',
    );
    expect(adapter.requests.single.path, '/api/v1/users/lookup');
    expect(adapter.requests.single.queryParameters,
        <String, Object?>{'handle': 'member@remote.example'});
  });

  test('guild bot E2EE consent preserves qualified refs and audit reasons',
      () async {
    Map<String, Object?> grant(List<Object?> devices) => <String, Object?>{
          'application_ref': '3@apps.example',
          'channel_ref': '2@guild.example',
          'e2ee_mode': 'participant',
          'devices': devices,
        };
    final adapter = _QueueAdapter(<_Reply>[
      _Reply(jsonEncode(grant(const <Object?>[
        <String, Object?>{
          'device_id': 'kbe_worker',
          'status': 'active',
          'consent_generation': '2',
          'joined_epoch': '7',
          'history_floor_message_ref': '9@guild.example',
        }
      ]))),
      _Reply(jsonEncode(grant(const <Object?>[]))),
      _Reply(jsonEncode(grant(const <Object?>[]))),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final guild = EntityRef.parse('1@guild.example');
    final channel = EntityRef.parse('2@guild.example');
    final application = EntityRef.parse('3@apps.example');

    final state = await repository.botE2eeParticipation(
      guild: guild,
      channel: channel,
      application: application,
    );
    await repository.grantBotE2eeParticipation(
      guild: guild,
      channel: channel,
      application: application,
      reason: 'Reviewed devices',
    );
    await repository.revokeBotE2eeParticipation(
      guild: guild,
      channel: channel,
      application: application,
      reason: 'Integration removed',
    );

    expect(state, isA<BotE2eeParticipation>());
    expect(state.devices.single.historyNotice, contains('9@guild.example'));
    expect(
      adapter.requests.map((request) => request.path).toSet(),
      <String>{
        '/api/v1/guilds/1@guild.example/channels/2@guild.example/e2ee/bots/3@apps.example',
      },
    );
    expect(adapter.requests.map((request) => request.method),
        <String>['GET', 'PUT', 'DELETE']);
    expect(
        adapter.requests[1].headers['X-Audit-Log-Reason'], 'Reviewed devices');
    expect(adapter.requests[2].headers['X-Audit-Log-Reason'],
        'Integration removed');
  });

  test('report attachments enforce authority, encryption and scan fences', () {
    final report = AdministrationReport.fromJson(<String, Object?>{
      'id': '88',
      'source': 'user',
      'severity': 'high',
      'target_type': 'message',
      'target_ref': '77@chat.example',
      'category': 'harassment',
      'status': 'submitted',
      'evidence': <String, Object?>{
        'attachments': const <Object?>[
          <String, Object?>{
            'attachment_ref': '90@chat.example',
            'filename': '../screen:shot.png',
            'content_type': 'image/png',
            'size': 2048,
            'encryption_mode': 'plaintext',
          },
          <String, Object?>{
            'attachment_ref': '91@remote.example',
            'filename': 'remote.png',
            'content_type': 'image/png',
            'encryption_mode': 'plaintext',
          },
          <String, Object?>{
            'attachment_ref': '92@chat.example',
            'filename': 'cipher.bin',
            'content_type': 'application/octet-stream',
            'encryption_mode': 'e2ee',
          },
        ],
      },
      'encryption_mode': 'plaintext',
      'created_at': '2026-08-28T10:00:00Z',
    });
    final local = report.attachments[0];
    final remote = report.attachments[1];
    final encrypted = report.attachments[2];

    expect(report.attachmentRestriction(local, Domain('chat.example')), isNull);
    expect(report.canPreview(local, Domain('chat.example')), isTrue);
    expect(
      report.attachmentRestriction(remote, Domain('chat.example')),
      contains('authority instance'),
    );
    expect(
      report.attachmentRestriction(encrypted, Domain('chat.example')),
      contains('end-to-end encrypted'),
    );
    expect(safeEvidenceFilename('../screen:shot.png'), '.._screen_shot.png');
    expect(safeEvidenceFilename('..'), 'reported-evidence');
  });
}

Map<String, Object?> _application() => <String, Object?>{
      'ref': '10@apps.example',
      'name': 'Tasks',
      'description': 'Task automation',
      'icon_hash': null,
      'support_url': 'https://support.example',
      'privacy_url': 'https://support.example/privacy',
      'status': 'active',
      'target_policy': 'open',
      'default_scopes': const ['applications.commands', 'interactions.respond'],
      'default_intents': const ['interactions'],
      'default_permissions': '0',
      'supported_install_types': const ['guild_install', 'user_install'],
      'user_install_scopes': const [
        'applications.commands',
        'interactions.respond',
      ],
      'user_install_contexts': const ['bot_dm', 'private_channel'],
      'e2ee_modes': const ['participant'],
      'bot_user': const <String, Object?>{
        'handle': 'tasks@apps.example',
      },
    };

Map<String, Object?> _identity() => <String, Object?>{
      'user': const <String, Object?>{
        'id': '1',
        'origin_domain': 'chat.example',
        'username': 'owner',
      },
      'roles': const ['owner'],
      'capabilities': const [
        'admin.read',
        'reports.read',
        'reports.manage',
        'users.manage',
        'bots.manage',
        'instances.manage',
        'audit.read',
      ],
    };

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
