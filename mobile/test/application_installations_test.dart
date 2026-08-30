import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/bot_e2ee_participation.dart';
import 'package:kaede_mobile/src/features/auth/deep_link_screen.dart';

void main() {
  test('guild invite links remain review-first across home instances', () {
    final link = MobileDeepLink.parse(
      Uri.parse('https://guild.example/invite/Ab12Cd34'),
    )!;

    expect(link.kind, MobileLinkKind.invite);
    expect(qualifiedInviteCode(link, Domain('guild.example')), 'Ab12Cd34');
    expect(
      qualifiedInviteCode(link, Domain('home.example')),
      'Ab12Cd34@guild.example',
    );
    expect(
      invitePreviewDetails(<String, Object?>{
        'uses': 2,
        'max_uses': 10,
        'role_ids': <String>['7@guild.example'],
        'target_user_count': 1,
        'target_type': 'stream',
        'guild_scheduled_event': <String, Object?>{'name': 'Town hall'},
      }),
      <String>[
        '8 uses remain',
        'Grants 1 role',
        'Limited invitation',
        'Opens a Go Live stream',
        'Event: Town hall',
      ],
    );
  });

  test('user installation payload decodes its grants and application identity',
      () {
    final installation = UserApplicationInstallation.fromJson(
      _installationJson(),
    );

    expect(installation.application.wire, '20@apps.example');
    expect(installation.user.wire, '40@chat.example');
    expect(installation.contexts, ['guild', 'private_channel', 'bot_dm']);
    expect(installation.e2eeParticipantCapable, isTrue);
    expect(installation.supportsEncryptedPrivateConversation, isTrue);
    expect(installation.createdAt, DateTime.utc(2026, 8, 27, 12));
    expect(
        userApplicationGrantData(
          scopes: userApplicationScopes,
          contexts: userApplicationContexts,
        ),
        <String, Object?>{
          'scopes': ['applications.commands', 'interactions.respond'],
          'contexts': ['guild', 'private_channel', 'bot_dm'],
          'intents': ['interactions'],
        });
  });

  test('suspended and revoked installations lock grant editing', () {
    final suspended = UserApplicationInstallation.fromJson(
      _installationJson(status: 'suspended'),
    );
    expect(suspended.isActive, isFalse);
    expect(suspended.isSuspended, isTrue);
    expect(suspended.grantsEditable, isFalse);
    expect(suspended.supportsEncryptedPrivateConversation, isFalse);
    expect(
      suspended.unavailableReason,
      suspendedUserApplicationExplanation,
    );

    final revoked = UserApplicationInstallation.fromJson(
      _installationJson(status: 'revoked'),
    );
    expect(revoked.isActive, isFalse);
    expect(revoked.isSuspended, isFalse);
    expect(revoked.grantsEditable, isFalse);
    expect(revoked.unavailableReason, contains('revoked'));
  });

  test('DM participant consent preserves authority refs and lifecycle',
      () async {
    final decoded = DmBotE2eeParticipation.fromJson(_dmParticipationJson());
    expect(decoded.applicationRef.wire, '20@apps.example');
    expect(decoded.channelRef.wire, '50@dm.example');
    expect(decoded.consentGeneration, '3');
    expect(decoded.everyoneConsented, isFalse);
    expect(decoded.devices.single.joinedEpoch, '7');

    final adapter = _InstallationAdapter(<_Reply>[
      _Reply(jsonEncode(_dmParticipationJson())),
      _Reply(jsonEncode(_dmParticipationJson(state: 'active'))),
      _Reply(jsonEncode(_dmParticipationJson(state: 'revoked'))),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final channel = EntityRef.parse('50@dm.example');
    final application = EntityRef.parse('20@apps.example');
    await repository.dmBotE2eeParticipation(
      channel: channel,
      application: application,
    );
    await repository.consentToDmBotE2eeParticipation(
      channel: channel,
      application: application,
    );
    await repository.revokeDmBotE2eeParticipation(
      channel: channel,
      application: application,
    );
    expect(adapter.requests.map((request) => request.method),
        ['GET', 'PUT', 'DELETE']);
    expect(
      adapter.requests.map((request) => request.path).toSet(),
      {'/api/v1/channels/50@dm.example/e2ee/bots/20@apps.example'},
    );
  });

  test('E2EE participation arrays reject malformed federated children', () {
    final dm = _dmParticipationJson();
    dm['participants'] = <Object?>[
      ...(dm['participants']! as List),
      'not a participant',
    ];
    expect(
      () => DmBotE2eeParticipation.fromJson(dm),
      throwsA(isA<FormatException>()),
    );

    expect(
      () => BotE2eeParticipation.fromJson(<String, Object?>{
        'application_ref': '20@apps.example',
        'channel_ref': '50@dm.example',
        'e2ee_mode': 'required',
        'devices': const <Object?>['not a device'],
      }),
      throwsA(isA<FormatException>()),
    );
  });

  test('repository covers the full account installation lifecycle', () async {
    final adapter = _InstallationAdapter(<_Reply>[
      _Reply(jsonEncode(<Object?>[_installationJson()])),
      _Reply(jsonEncode(_installationJson())),
      _Reply(jsonEncode(_installationJson(contexts: const ['guild']))),
      const _Reply('', status: 204),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

    final listed = await repository.userApplicationInstallations();
    final installed = await repository.installUserApplication(
      EntityRef.parse('20@apps.example'),
      scopes: userApplicationScopes,
      contexts: userApplicationContexts,
    );
    final updated = await repository.updateUserApplicationInstallation(
      installed.id,
      contexts: const <String>['guild'],
    );
    await repository.revokeUserApplicationInstallation(updated.id);

    expect(listed.single.applicationName, 'Tasks');
    expect(updated.contexts, const ['guild']);
    expect(
      adapter.requests.map((request) => request.method),
      ['GET', 'POST', 'PATCH', 'DELETE'],
    );
    expect(
      adapter.requests.map((request) => request.path),
      [
        '/api/v1/users/@me/application-installations',
        '/api/v1/users/@me/application-installations',
        '/api/v1/users/@me/application-installations/10',
        '/api/v1/users/@me/application-installations/10',
      ],
    );
    expect(adapter.requests[1].data, <String, Object?>{
      'application_ref': '20@apps.example',
      'scopes': ['applications.commands', 'interactions.respond'],
      'contexts': ['guild', 'private_channel', 'bot_dm'],
      'intents': ['interactions'],
    });
    expect(adapter.requests[2].data, <String, Object?>{
      'contexts': ['guild'],
    });
  });

  test('application invitation links resolve to a reviewed personal install',
      () async {
    final invite = ApplicationInstallInvite.fromJson(_inviteJson());
    expect(invite.application.wire, '20@apps.example');
    expect(invite.botHandle, 'tasks@apps.example');
    expect(invite.templateSlug, 'standard');
    expect(invite.e2eeMode, 'participant');
    expect(invite.supportsGuildInstall, isTrue);
    expect(invite.supportsUserInstall, isTrue);
    expect(invite.userInstallContexts, ['bot_dm', 'private_channel']);
    expect(
      userApplicationGrantData(
        scopes: invite.userInstallScopes,
        contexts: invite.userInstallContexts,
      ),
      <String, Object?>{
        'scopes': [
          'applications.commands',
          'interactions.respond',
          'attachments.read',
        ],
        'contexts': ['bot_dm', 'private_channel'],
        'intents': ['interactions'],
      },
    );

    final httpsLink = MobileDeepLink.parse(Uri.parse(
      'https://kaede.chat/applications/20%40apps.example/install/standard',
    ));
    final nativeLink = MobileDeepLink.parse(Uri.parse(
      'kaede://app/applications/20%40apps.example/install/standard'
      '?instance=kaede.chat',
    ));
    for (final link in <MobileDeepLink?>[httpsLink, nativeLink]) {
      expect(link?.kind, MobileLinkKind.applicationInstall);
      expect(link?.application?.wire, '20@apps.example');
      expect(link?.templateSlug, 'standard');
      expect(link?.requiresSession, isTrue);
    }

    final adapter = _InstallationAdapter(<_Reply>[
      _Reply(jsonEncode(_inviteJson())),
      const _Reply('{}'),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final resolved = await repository.resolveApplicationInstallInvite(
      EntityRef.parse('20@apps.example'),
      'standard',
    );
    await repository.installGuildApplication(
      EntityRef.parse('50@chat.example'),
      resolved.application,
      resolved.templateSlug,
    );
    expect(resolved.applicationName, 'Tasks');
    expect(adapter.requests.first.method, 'GET');
    expect(
      adapter.requests.first.path,
      '/api/v1/bot-invites/20@apps.example/standard',
    );
    expect(adapter.requests.last.method, 'POST');
    expect(
      adapter.requests.last.path,
      '/api/v1/guilds/50@chat.example/integrations/bots',
    );
    expect(adapter.requests.last.queryParameters, <String, Object?>{
      'application_ref': '20@apps.example',
      'template_slug': 'standard',
    });
  });
}

Map<String, Object?> _installationJson({
  List<String> contexts = userApplicationContexts,
  String status = 'active',
}) =>
    <String, Object?>{
      'id': '10',
      'application_ref': '20@apps.example',
      'application_name': 'Tasks',
      'bot_user_ref': '30@apps.example',
      'user_ref': '40@chat.example',
      'scopes': userApplicationScopes,
      'intents': userApplicationIntents,
      'contexts': contexts,
      'e2ee_participant_capable': true,
      'grant_revision': '2',
      'status': status,
      'created_at': '2026-08-27T12:00:00Z',
    };

Map<String, Object?> _dmParticipationJson({String state = 'pending'}) =>
    <String, Object?>{
      'application_ref': '20@apps.example',
      'channel_ref': '50@dm.example',
      'consent_state': state,
      'consent_generation': '3',
      'history_floor_message_ref': '60@dm.example',
      'participants': <Object?>[
        <String, Object?>{
          'user_ref': '40@chat.example',
          'consented': true,
        },
        <String, Object?>{
          'user_ref': '41@remote.example',
          'consented': state == 'active',
        },
      ],
      'devices': <Object?>[
        <String, Object?>{
          'device_id': 'kbe_device',
          'status': state == 'active' ? 'active' : 'pending',
          'joined_epoch': '7',
        },
      ],
      'encryption_policy': <String, Object?>{'generation': '3'},
    };

Map<String, Object?> _inviteJson() => <String, Object?>{
      'application': <String, Object?>{
        'id': '20',
        'origin_domain': 'apps.example',
        'ref': '20@apps.example',
        'name': 'Tasks',
        'description': 'Keeps work organized.',
        'icon_hash': null,
        'support_url': 'https://apps.example/support',
        'privacy_url': 'https://apps.example/privacy',
        'supported_install_types': <String>[
          'guild_install',
          'user_install',
        ],
        'user_install_scopes': <String>[
          'applications.commands',
          'interactions.respond',
          'attachments.read',
        ],
        'user_install_contexts': <String>['bot_dm', 'private_channel'],
        'bot_user': <String, Object?>{
          'id': '30',
          'origin_domain': 'apps.example',
          'ref': '30@apps.example',
          'username': 'tasks',
          'handle': 'tasks@apps.example',
        },
      },
      'template': <String, Object?>{
        'slug': 'standard',
        'name': 'Tasks',
        'description': 'Standard access',
        'scopes': <String>['commands.read'],
        'intents': <String>['interactions'],
        'permissions': '0',
        'e2ee_mode': 'participant',
      },
    };

final class _Reply {
  const _Reply(this.body, {this.status = 200});

  final String body;
  final int status;
}

final class _InstallationAdapter implements HttpClientAdapter {
  _InstallationAdapter(this._replies);

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
