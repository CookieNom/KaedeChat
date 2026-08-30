import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/announcement_repository.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/announcements.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/guild/announcement_management_tab.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

void main() {
  final actor = KaedeUser.fromJson(<String, Object?>{
    'id': '7',
    'origin_domain': 'chat.example',
    'username': 'maple',
    'handle': 'maple@chat.example',
  });

  test('filters cross-guild targets and enforces publish authority', () {
    final source = _channel(
      id: '2',
      type: 5,
      permissions: Permission.viewChannel |
          Permission.readMessageHistory |
          Permission.sendMessages,
    );
    final target = _channel(
      id: '3',
      type: 0,
      permissions: Permission.manageWebhooks,
    );
    final denied = _channel(id: '4', type: 0, permissions: 0);
    final e2eeRequired = _channel(
      id: '7',
      type: 0,
      permissions: Permission.manageWebhooks,
      e2eeRequired: true,
    );
    final remoteTarget = _channel(
      id: '5',
      type: 0,
      permissions: Permission.administrator,
      domain: 'remote.example',
      guildId: '9',
    );
    final remoteSource = _channel(
      id: '6',
      type: 5,
      permissions: Permission.sendMessages,
      domain: 'remote.example',
      guildId: '9',
    );
    final localGuild = _guild('1', 'Local', <KaedeChannel>[
      source,
      target,
      denied,
      e2eeRequired,
    ]);
    final remoteGuild = _guild(
      '9',
      'Remote',
      <KaedeChannel>[remoteTarget, remoteSource],
      domain: 'remote.example',
    );
    final post = KaedeMessage.fromJson(_messageJson());

    expect(canReadAnnouncementChannel(localGuild, source, actor), isTrue);
    expect(
      announcementTargets(<KaedeGuild>[localGuild, remoteGuild], actor)
          .map((item) => item.ref.wire),
      <String>['3@chat.example', '5@remote.example'],
    );
    expect(
      canPublishAnnouncementMessage(localGuild, source, post, actor),
      isTrue,
    );
    expect(
      canPublishAnnouncementMessage(
        localGuild,
        source,
        post.copyWith(flags: messageFlagCrossposted),
        actor,
      ),
      isFalse,
    );
    expect(
      canPublishAnnouncementMessage(
        remoteGuild,
        remoteSource,
        KaedeMessage.fromJson(<String, Object?>{
          ..._messageJson(),
          'origin_domain': 'remote.example',
          'channel_id': '6',
          'channel_domain': 'remote.example',
        }),
        actor,
      ),
      isTrue,
    );
  });

  test('repository uses the full follower and crosspost contract', () async {
    final follow = _followJson();
    final adapter = _AnnouncementAdapter(<_Reply>[
      _Reply(jsonEncode(<Object?>[follow])),
      _Reply(jsonEncode(follow)),
      const _Reply('{}'),
      _Reply(jsonEncode(_messageJson(flags: messageFlagCrossposted))),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final source = EntityRef.parse('2@chat.example');
    final target = EntityRef.parse('3@remote.example');
    final message = EntityRef.parse('10@chat.example');

    final listed = await repository.announcementFollowers(source);
    final created = await repository.followAnnouncement(source, target);
    await repository.deleteAnnouncementFollow(source, created.ref);
    final published = await repository.publishAnnouncement(source, message);

    expect(listed.single.targetChannel, target);
    expect(created.federated, isTrue);
    expect(isPublishedAnnouncement(published), isTrue);
    expect(
      adapter.requests.map((request) => request.method),
      <String>['GET', 'POST', 'DELETE', 'POST'],
    );
    expect(
      adapter.requests.map((request) => request.path),
      <String>[
        '/api/v1/channels/2@chat.example/followers',
        '/api/v1/channels/2@chat.example/followers',
        '/api/v1/channels/2@chat.example/followers/42%40remote.example',
        '/api/v1/channels/2@chat.example/messages/10@chat.example/crosspost',
      ],
    );
    expect(
      adapter.requests[1].data,
      <String, Object?>{'target_channel_id': '3@remote.example'},
    );
  });

  test('announcement responses reject substituted lineage and ambiguous state',
      () async {
    final source = EntityRef.parse('2@chat.example');
    final target = EntityRef.parse('3@remote.example');
    final valid = _followJson();
    for (final hostile in <Map<String, Object?>>[
      <String, Object?>{...valid, 'source_channel_id': '20'},
      <String, Object?>{...valid, 'target_channel_id': '30'},
      <String, Object?>{...valid, 'ref': '43@remote.example'},
      <String, Object?>{...valid, 'active': 'true'},
      <String, Object?>{...valid, 'updated_at': '2026-08-27T11:00:00Z'},
    ]) {
      expect(
        () => AnnouncementFollow.fromJson(
          hostile,
          expectedSource: source,
          expectedTarget: target,
        ),
        throwsFormatException,
      );
    }

    final adapter = _AnnouncementAdapter(<_Reply>[
      _Reply(jsonEncode(<Object?>[
        <String, Object?>{...valid, 'id': '43', 'ref': '43@remote.example'},
        valid,
      ])),
      _Reply(jsonEncode(<String, Object?>{
        ..._messageJson(flags: messageFlagCrossposted),
        'channel_id': '9',
      })),
      _Reply(jsonEncode(_messageJson(flags: 0))),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    await expectLater(
        repository.announcementFollowers(source), throwsFormatException);
    await expectLater(
      repository.publishAnnouncement(
          source, EntityRef.parse('10@chat.example')),
      throwsFormatException,
    );
    await expectLater(
      repository.publishAnnouncement(
          source, EntityRef.parse('10@chat.example')),
      throwsFormatException,
    );
  });

  testWidgets('management UI explains missing source read permissions',
      (tester) async {
    final lockedGuild = _guild(
      '1',
      'Local',
      <KaedeChannel>[_channel(id: '2', type: 5, permissions: 0)],
    );
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = _AnnouncementAdapter(<_Reply>[]),
      ),
    );

    await tester.pumpWidget(MaterialApp(
      home: AnnouncementManagementTab(
        guild: lockedGuild,
        guilds: <KaedeGuild>[lockedGuild],
        currentUser: actor,
        repository: repository,
      ),
    ));

    expect(find.text('Followers are unavailable'), findsOneWidget);
    expect(
      find.textContaining('View Channel and Read Message History'),
      findsOneWidget,
    );
  });

  testWidgets('management UI exposes readable sources and eligible targets',
      (tester) async {
    final source = _channel(
      id: '2',
      type: 5,
      permissions: Permission.viewChannel | Permission.readMessageHistory,
    );
    final target = _channel(
      id: '3',
      type: 0,
      permissions: Permission.manageWebhooks,
    );
    final guild = _guild('1', 'Local', <KaedeChannel>[source, target]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()
          ..httpClientAdapter = _AnnouncementAdapter(
            <_Reply>[const _Reply('[]')],
          ),
      ),
    );

    await tester.pumpWidget(MaterialApp(
      home: AnnouncementManagementTab(
        guild: guild,
        guilds: <KaedeGuild>[guild],
        currentUser: actor,
        repository: repository,
      ),
    ));
    await tester.pumpAndSettle();

    expect(
      find.byKey(
        const ValueKey('announcement-source-picker-2@chat.example'),
      ),
      findsOneWidget,
    );
    expect(
      find.byKey(
        const ValueKey(
          'announcement-target-picker-2@chat.example-null',
        ),
      ),
      findsOneWidget,
    );
    expect(
      find.text('Destinations can be in another Kaede guild or instance.'),
      findsOneWidget,
    );
    expect(find.text('No follower channels yet'), findsOneWidget);
  });

  testWidgets('channel Follow surface fixes the source and hides management',
      (tester) async {
    final source = _channel(
      id: '2',
      type: 5,
      permissions: Permission.viewChannel | Permission.readMessageHistory,
    );
    final target = _channel(
      id: '3',
      type: 0,
      permissions: Permission.manageWebhooks,
    );
    final guild = _guild('1', 'Local', <KaedeChannel>[source, target]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()
          ..httpClientAdapter = _AnnouncementAdapter(
            <_Reply>[const _Reply('[]')],
          ),
      ),
    );

    await tester.pumpWidget(MaterialApp(
      home: AnnouncementManagementTab(
        guild: guild,
        guilds: <KaedeGuild>[guild],
        currentUser: actor,
        repository: repository,
        sourceChannel: source,
        createOnly: true,
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Follow #channel-2'), findsOneWidget);
    expect(
        find.byKey(const ValueKey('announcement-source-picker-2@chat.example')),
        findsNothing);
    expect(find.text('Follower channels'), findsNothing);
    expect(find.byKey(const ValueKey('announcement-follow-button')),
        findsOneWidget);
  });
}

KaedeChannel _channel({
  required String id,
  required int type,
  required int permissions,
  String domain = 'chat.example',
  String guildId = '1',
  bool e2eeRequired = false,
}) =>
    KaedeChannel.fromJson(<String, Object?>{
      'id': id,
      'origin_domain': domain,
      'guild_id': guildId,
      'guild_domain': domain,
      'type': type,
      'name': 'channel-$id',
      'position': int.parse(id),
      'permissions': '$permissions',
      'e2ee_required': e2eeRequired,
    });

KaedeGuild _guild(
  String id,
  String name,
  List<KaedeChannel> channels, {
  String domain = 'chat.example',
}) =>
    KaedeGuild.fromJson(<String, Object?>{
      'id': id,
      'origin_domain': domain,
      'name': name,
      'owner_id': '99',
      'owner_domain': domain,
      'permissions': '0',
      'unavailable': false,
      'channels': channels.map((channel) => channel.toJson()).toList(),
    });

Map<String, Object?> _followJson() => <String, Object?>{
      'id': '42',
      'ref': '42@remote.example',
      'source_channel_id': '2',
      'source_channel_domain': 'chat.example',
      'target_channel_id': '3',
      'target_channel_domain': 'remote.example',
      'creator_id': '7',
      'creator_domain': 'chat.example',
      'active': true,
      'federated': true,
      'generation': '1',
      'lifecycle_state': 'active',
      'name': null,
      'avatar_hash': null,
      'created_at': '2026-08-27T12:00:00Z',
      'updated_at': '2026-08-27T12:00:00Z',
    };

Map<String, Object?> _messageJson({int flags = 0}) => <String, Object?>{
      'id': '10',
      'origin_domain': 'chat.example',
      'channel_id': '2',
      'channel_domain': 'chat.example',
      'author_id': '7',
      'author_domain': 'chat.example',
      'author': <String, Object?>{
        'id': '7',
        'origin_domain': 'chat.example',
        'username': 'maple',
        'handle': 'maple@chat.example',
      },
      'content': 'News',
      'message_type': 0,
      'flags': flags,
      'created_at': '2026-08-27T12:00:00Z',
    };

final class _Reply {
  const _Reply(this.body);

  final String body;
}

final class _AnnouncementAdapter implements HttpClientAdapter {
  _AnnouncementAdapter(this._replies);

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
      200,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
