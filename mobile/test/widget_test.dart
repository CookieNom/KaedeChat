import 'dart:convert';

import 'package:cryptography/cryptography.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_navigation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/voice/voice_room.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/platform/notification_policy.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:livekit_client/livekit_client.dart';

void main() {
  group('message reaction recents', () {
    test('ranks frequently used emoji and breaks ties by recency', () {
      expect(
        rankRecentReactions(<String>['👍', '🔥', '👍', '😂', '🔥', '🔥']),
        <String>['🔥', '👍', '😂'],
      );
      expect(
        rankRecentReactions(<String>['👍', '😂', '🔥']),
        <String>['🔥', '😂', '👍'],
      );
    });

    test('uses a compact useful default for a new user', () {
      expect(rankRecentReactions(const <String>[]), hasLength(4));
    });
  });

  group('message search models', () {
    test('parses encryption policy and bounded search coverage', () {
      final channel = KaedeChannel.fromJson(<String, Object?>{
        'id': '10',
        'origin_domain': 'remote.example',
        'guild_id': '20',
        'guild_domain': 'remote.example',
        'type': 0,
        'position': 0,
        'encryption_mode': 'e2ee',
        'search_available': false,
      });
      final page = MessageSearchPage.fromJson(<String, Object?>{
        'results': <Object?>[],
        'coverage': <String, Object?>{
          'local': 'cached',
          'authority': 'unavailable',
        },
        'encrypted_channel_refs': <Object?>['10@remote.example'],
        'indexing': true,
      });

      expect(channel.encryptionMode, 'e2ee');
      expect(channel.searchAvailable, isFalse);
      expect(page.localCoverage, 'cached');
      expect(page.encryptedChannelRefs, <EntityRef>[channel.ref]);
      expect(page.indexing, isTrue);
    });
  });

  group('group direct-message models', () {
    test('round-trips group ownership and recipients', () {
      final channel = KaedeChannel.fromJson(<String, Object?>{
        'id': '10',
        'origin_domain': 'alpha.example',
        'guild_id': null,
        'guild_domain': null,
        'type': 1,
        'name': 'Weekend plans',
        'position': 0,
        'conversation_type': 'group',
        'owner_id': '1',
        'owner_domain': 'alpha.example',
        'recipients': <Object?>[
          <String, Object?>{
            'id': '2',
            'origin_domain': 'beta.example',
            'username': 'turtle',
            'handle': 'turtle@beta.example',
            'display_name': 'Turtle',
            'avatar_hash': null,
          },
        ],
      });

      expect(channel.conversationType, 'group');
      expect(
          channel.ownerRef, EntityRef(Snowflake('1'), Domain('alpha.example')));
      expect(channel.recipients.single.handle, 'turtle@beta.example');
      expect(channel.toJson()['conversation_type'], 'group');
    });

    test('retains membership notice types across local reconciliation', () {
      final message = KaedeMessage.fromJson(<String, Object?>{
        'id': '11',
        'origin_domain': 'alpha.example',
        'channel_id': '10',
        'channel_domain': 'alpha.example',
        'author_id': '1',
        'author_domain': 'alpha.example',
        'content': 'Alice added Bob to the group.',
        'message_type': 3,
        'created_at': '2026-08-16T12:00:00Z',
      });

      expect(message.messageType, 3);
      expect(message.copyWith().messageType, 3);
      expect(message.toJson()['message_type'], 3);
    });
  });

  group('federated profile refresh', () {
    test(
        'resolved profile cache immediately labels an otherwise unknown mention',
        () {
      final reference = EntityRef(Snowflake('42'), Domain('remote.example'));
      final resolved = KaedeUser(
        ref: reference,
        username: 'maple',
        handle: 'maple@remote.example',
        displayName: 'Maple',
      );
      final state = MobileState(
          userProfiles: <EntityRef, KaedeUser>{reference: resolved});

      final rendered = renderMentionLabels('Hello <@${reference.wire}>', state);

      expect(rendered, contains('[@Maple]'));
      expect(rendered, isNot(contains('42@remote.example]')));
    });

    test('unresolved profiles never expose the history placeholder as a name',
        () {
      final user = KaedeUser.fromJson(<String, Object?>{
        'id': '42',
        'origin_domain': 'remote.example',
        'username': 'history_deadbeef',
        'handle': 'history_deadbeef@remote.example',
        'profile_resolved': false,
      });

      expect(user.name, 'Remote user · remote.example');
      expect(user.name, isNot(contains('history_')));
    });

    test('guild settings roster overlays a live composite profile update', () {
      final reference = EntityRef(Snowflake('42'), Domain('remote.example'));
      final placeholder = KaedeUser(
        ref: reference,
        username: 'history_deadbeef',
        handle: 'history_deadbeef@remote.example',
        profileResolved: false,
      );
      final resolved = KaedeUser(
        ref: reference,
        username: 'maple',
        handle: 'maple@remote.example',
        displayName: 'Maple',
      );
      final snapshot = GuildMember(
        user: placeholder,
        roleIds: const <String>['7'],
        nickname: 'Captain',
        timeoutUntil: DateTime.utc(2026, 8, 13),
      );

      final overlaid = overlayGuildMemberProfile(
        snapshot,
        <EntityRef, KaedeUser>{reference: resolved},
      );

      expect(overlaid.user, same(resolved));
      expect(overlaid.nickname, 'Captain');
      expect(overlaid.roleIds, const <String>['7']);
      expect(overlaid.timeoutUntil, DateTime.utc(2026, 8, 13));
    });

    test('guild roster profile overlay never matches a snowflake alone', () {
      final placeholder = KaedeUser(
        ref: EntityRef(Snowflake('42'), Domain('remote.example')),
        username: 'history_deadbeef',
        handle: 'history_deadbeef@remote.example',
        profileResolved: false,
      );
      final otherHome = KaedeUser(
        ref: EntityRef(Snowflake('42'), Domain('other.example')),
        username: 'maple',
        handle: 'maple@other.example',
      );
      final snapshot = GuildMember(user: placeholder, roleIds: const []);

      final overlaid = overlayGuildMemberProfile(
        snapshot,
        <EntityRef, KaedeUser>{otherHome.ref: otherHome},
      );

      expect(overlaid, same(snapshot));
      expect(overlaid.user, same(placeholder));
    });
  });

  group('automatic older-history paging', () {
    test('loads only near the oldest edge of a reversed conversation', () {
      expect(
        shouldAutomaticallyLoadEarlier(
          pixels: 1685,
          maxScrollExtent: 2000,
          hasEarlier: true,
          loading: false,
          hasError: false,
        ),
        isTrue,
      );
      expect(
        shouldAutomaticallyLoadEarlier(
          pixels: 1200,
          maxScrollExtent: 2000,
          hasEarlier: true,
          loading: false,
          hasError: false,
        ),
        isFalse,
      );
    });

    test('fills a short viewport but stops for loading, errors, or completion',
        () {
      expect(
        shouldAutomaticallyLoadEarlier(
          pixels: 0,
          maxScrollExtent: 0,
          hasEarlier: true,
          loading: false,
          hasError: false,
        ),
        isTrue,
      );
      for (final state in <({bool hasEarlier, bool loading, bool hasError})>[
        (hasEarlier: false, loading: false, hasError: false),
        (hasEarlier: true, loading: true, hasError: false),
        (hasEarlier: true, loading: false, hasError: true),
      ]) {
        expect(
          shouldAutomaticallyLoadEarlier(
            pixels: 2000,
            maxScrollExtent: 2000,
            hasEarlier: state.hasEarlier,
            loading: state.loading,
            hasError: state.hasError,
          ),
          isFalse,
        );
      }
    });
  });

  group('user-facing errors', () {
    test('classifies network failures with recovery guidance', () {
      final request = RequestOptions(path: '/api/v1/guilds');
      final error = KaedeException.fromDio(DioException(
        requestOptions: request,
        type: DioExceptionType.connectionTimeout,
      ));

      expect(error.code, 'NETWORK_ERROR');
      expect(error.message, contains('too long to connect'));
      expect(error.message, contains('Check your connection'));
      expect(error.message, isNot(contains('DioException')));
    });

    test('hides server internals and includes a safe support reference', () {
      final request = RequestOptions(path: '/api/v1/guilds/1');
      final error = KaedeException.fromDio(DioException(
        requestOptions: request,
        type: DioExceptionType.badResponse,
        response: Response<Object?>(
          requestOptions: request,
          statusCode: 500,
          data: <String, Object?>{
            'code': 'INTERNAL_SERVER_ERROR',
            'message': 'SQLAlchemy MissingGreenlet at /srv/app/secrets.py',
            'trace_id': 'trace-a1b2c3',
          },
        ),
      ));
      final message = userFacingError(
        error,
        summary: 'Could not save the guild',
      );

      expect(message, contains('server ran into an unexpected problem'));
      expect(message, contains('Reference: trace-a1b2c3'));
      expect(message, isNot(contains('SQLAlchemy')));
      expect(message, isNot(contains('/srv/app')));
      expect(message, isNot(contains('INTERNAL_SERVER_ERROR')));
    });

    test('explains validation fields instead of showing raw payloads', () {
      final request = RequestOptions(path: '/api/v1/users/@me');
      final error = KaedeException.fromDio(DioException(
        requestOptions: request,
        type: DioExceptionType.badResponse,
        response: Response<Object?>(
          requestOptions: request,
          statusCode: 422,
          data: <String, Object?>{
            'detail': <Object?>[
              <String, Object?>{
                'loc': <Object?>['body', 'display_name'],
                'msg': 'Field required',
                'type': 'missing',
              },
            ],
          },
        ),
      ));

      expect(error.message, 'Check display name: field required.');
      expect(error.message, isNot(contains('VALIDATION')));
    });

    test('prefers the safe validation summary and accepts current issue keys',
        () {
      final request = RequestOptions(path: '/api/v1/users/@me');
      KaedeException parse(Map<String, Object?> data) =>
          KaedeException.fromDio(DioException(
            requestOptions: request,
            type: DioExceptionType.badResponse,
            response: Response<Object?>(
              requestOptions: request,
              statusCode: 422,
              data: data,
            ),
          ));
      final issues = <Object?>[
        <String, Object?>{
          'location': <Object?>['body', 'display_name'],
          'message': 'Field required',
          'type': 'missing',
        },
      ];

      expect(
        parse(<String, Object?>{
          'code': 'VALIDATION_ERROR',
          'message': 'The display name field is required.',
          'errors': issues,
        }).message,
        'The display name field is required.',
      );
      expect(
        parse(<String, Object?>{'errors': issues}).message,
        'Check display name: field required.',
      );
      expect(
        parse(<String, Object?>{
          'detail': <String, Object?>{
            'code': 'VALIDATION_ERROR',
            'errors': <Object?>[
              <String, Object?>{
                'loc': <Object?>['body', 'display_name'],
                'msg': 'Field required',
                'type': 'missing',
              },
            ],
          },
        }).message,
        'Check display name: field required.',
      );
    });

    test('formats retry timing and upload limits from server extensions', () {
      final request = RequestOptions(path: '/api/v1/attachments');
      final rateLimit = KaedeException.fromDio(DioException(
        requestOptions: request,
        type: DioExceptionType.badResponse,
        response: Response<Object?>(
          requestOptions: request,
          statusCode: 429,
          data: <String, Object?>{
            'code': 'SLOWMODE_RATE_LIMITED',
            'retry_after_ms': 2500,
          },
        ),
      ));
      final tooLarge = KaedeException.fromDio(DioException(
        requestOptions: request,
        type: DioExceptionType.badResponse,
        response: Response<Object?>(
          requestOptions: request,
          statusCode: 413,
          data: <String, Object?>{
            'code': 'ATTACHMENT_TOO_LARGE',
            'max_bytes': 8 * 1024 * 1024,
          },
        ),
      ));

      expect(rateLimit.message, 'Slow mode is active. Try again in 3 seconds.');
      expect(tooLarge.message, contains('8 MiB limit'));

      final voiceRetry = KaedeException.fromDio(DioException(
        requestOptions: request,
        type: DioExceptionType.badResponse,
        response: Response<Object?>(
          requestOptions: request,
          statusCode: 503,
          data: <String, Object?>{
            'code': 'VOICE_HOME_UNREACHABLE',
            'retry_after_ms': 2500,
          },
        ),
      ));
      expect(voiceRetry.message, contains('3 seconds'));
      expect(voiceRetry.message, isNot(contains('a moment')));
    });

    test('describes federation storage limits as an instance problem', () {
      final request = RequestOptions(path: '/api/v1/channels/1/messages');
      KaedeException parse(String code) => KaedeException.fromDio(DioException(
            requestOptions: request,
            type: DioExceptionType.badResponse,
            response: Response<Object?>(
              requestOptions: request,
              statusCode: 507,
              data: <String, Object?>{'code': code},
            ),
          ));

      expect(
        parse('FEDERATED_DM_STORAGE_QUOTA_EXCEEDED').message,
        contains('This instance could not retain more direct-message data'),
      );
      expect(
        parse('KAED_FED_REPLICA_QUOTA_EXCEEDED').message,
        contains('guild’s local replica reached its cache limit'),
      );
      expect(
        parse('FEDERATED_DM_HISTORY_UNAVAILABLE').message,
        contains('recent messages are still available'),
      );
      expect(
        parse('FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED').message,
        contains('cannot cache another remote account'),
      );
      expect(
        parse('FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED').message,
        contains('cannot cache another remote server'),
      );
      expect(
        parse('FEDERATION_OUTBOX_CAPACITY_EXCEEDED').message,
        contains('Nothing was saved'),
      );
      expect(
        parse('KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED').message,
        contains('friend request'),
      );
      expect(
        parse('FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE').message,
        contains('retry automatically'),
      );
      expect(
        parse('FEDERATED_GUILD_HISTORY_LIMIT_REACHED').message,
        contains('Recent messages and new activity remain available'),
      );
      expect(
        parse('FEDERATED_GUILD_HISTORY_REJECTED').message,
        contains('could not be safely imported'),
      );
    });

    test('does not expose raw exception prefixes or local secrets', () {
      final message = userFacingError(
        StateError('token=top-secret at /data/user/0/kaede'),
      );

      expect(message, contains('Something unexpected went wrong'));
      expect(message, isNot(contains('Bad state')));
      expect(message, isNot(contains('top-secret')));
      expect(message, isNot(contains('/data/user')));
      expect(
        userFacingError(const UserInputException('Choose an image.')),
        'Choose an image.',
      );
      expect(
        userFacingError('MISSING_PERMISSIONS'),
        'You do not have permission to do that.',
      );
      expect(
        userFacingError('UNRECOGNIZED_INTERNAL_CODE'),
        isNot(contains('UNRECOGNIZED_INTERNAL_CODE')),
      );
      expect(
        userFacingError('Failed at /home/user: token=top-secret'),
        isNot(contains('/home/user')),
      );
      expect(
        userFacingError('Failed at /home/user: token=top-secret'),
        isNot(contains('top-secret')),
      );
    });

    test('formats realtime errors and voice disconnect reasons', () {
      final gateway = userFacingGatewayError(
        <String, Object?>{
          'code': 'DM_PRIVACY_REJECTED',
          'reason': 'DM_PRIVACY_REJECTED',
          'trace_id': 'gateway-1234',
        },
        fallback: 'The direct message could not be opened.',
      );

      expect(gateway, contains('privacy settings'));
      expect(gateway, contains('Reference: gateway-1234'));
      expect(gateway, isNot(contains('DM_PRIVACY_REJECTED')));
      expect(
        voiceDisconnectMessage(DisconnectReason.participantRemoved),
        'A moderator disconnected you from voice.',
      );
      expect(
        voiceDisconnectMessage(DisconnectReason.reconnectAttemptsExceeded),
        contains('connection to voice was lost'),
      );
    });
  });

  group('scanned profile media', () {
    test('accepts supported picker types without mislabeling unknown images',
        () {
      expect(imageUploadContentType('avatar.PNG'), 'image/png');
      expect(imageUploadContentType('avatar.jpeg'), 'image/jpeg');
      expect(imageUploadContentType('avatar', reportedType: 'image/webp'),
          'image/webp');
      expect(imageUploadContentType('avatar.heic'), isNull);
      expect(
        imageUploadContentType('avatar.heic', reportedType: 'image/heic'),
        isNull,
      );
    });

    test('waits for a clean scan and repeats the binding commit', () async {
      var commits = 0;
      var polls = 0;
      final result = await commitScannedMedia(
        commit: () async {
          commits += 1;
          return <String, Object?>{
            'scan_status': commits == 1 ? 'pending' : 'clean',
          };
        },
        status: () async {
          polls += 1;
          return <String, Object?>{
            'scan_status': polls < 2 ? 'pending' : 'clean',
          };
        },
        pollInterval: Duration.zero,
      );

      expect(commits, 2);
      expect(polls, 2);
      expect(result['scan_status'], 'clean');
    });

    test('does not repeat a commit that already bound clean media', () async {
      var commits = 0;
      var polls = 0;
      await commitScannedMedia(
        commit: () async {
          commits += 1;
          return <String, Object?>{'scan_status': 'clean'};
        },
        status: () async {
          polls += 1;
          return <String, Object?>{'scan_status': 'clean'};
        },
        pollInterval: Duration.zero,
      );

      expect(commits, 1);
      expect(polls, 0);
    });

    test('surfaces rejected and timed-out processing', () async {
      await expectLater(
        commitScannedMedia(
          commit: () async => <String, Object?>{'scan_status': 'pending'},
          status: () async => <String, Object?>{'scan_status': 'infected'},
          pollInterval: Duration.zero,
        ),
        throwsA(isA<KaedeException>().having(
          (error) => error.code,
          'code',
          'MEDIA_PROCESSING_REJECTED',
        )),
      );
      await expectLater(
        commitScannedMedia(
          commit: () async => <String, Object?>{'scan_status': 'pending'},
          status: () async => <String, Object?>{'scan_status': 'pending'},
          pollInterval: Duration.zero,
          maxPollAttempts: 2,
        ),
        throwsA(isA<KaedeException>().having(
          (error) => error.code,
          'code',
          'MEDIA_PROCESSING_TIMEOUT',
        )),
      );
    });
  });

  group('notification preview preference', () {
    test('defaults on but preserves an explicit opt-out', () {
      expect(notificationPreviewsEnabled(const <String, bool>{}), isTrue);
      expect(
        notificationPreviewsEnabled(
          const <String, bool>{'show_notification_previews': true},
        ),
        isTrue,
      );
      expect(
        notificationPreviewsEnabled(
          const <String, bool>{'show_notification_previews': false},
        ),
        isFalse,
      );
    });
  });

  group('wire identifiers', () {
    test('federated references retain their home instance', () {
      final reference = EntityRef.parse('76423789306458112@Chat.Example.');
      expect(reference.id.value, '76423789306458112');
      expect(reference.domain.value, 'chat.example');
      expect(reference.toString(), '76423789306458112@chat.example');
    });

    test('local shorthand requires an explicit local domain', () {
      expect(
        EntityRef.parse('76423789306458112', localDomain: Domain('kaede.chat')),
        EntityRef.parse('76423789306458112@kaede.chat'),
      );
      expect(() => EntityRef.parse('76423789306458112'), throwsFormatException);
    });

    test('decodes structured references used by typed API payloads', () {
      expect(
        EntityRef.fromJson(<String, Object?>{
          'id': '79044282979201024',
          'origin_domain': 'Kaede.Chat',
        }),
        EntityRef.parse('79044282979201024@kaede.chat'),
      );
      expect(
        EntityRef.fromJson('79044282979201024@kaede.chat'),
        EntityRef.parse('79044282979201024@kaede.chat'),
      );
    });

    test('decodes rolling DM history and replica synchronization status', () {
      final dm = KaedeChannel.fromJson(<String, Object?>{
        'id': '79044282979201024',
        'origin_domain': 'kaede.chat',
        'type': 1,
        'position': 0,
        'permissions': '0',
        'history_truncated': true,
        'history_retention': 'rolling_replica_cache',
        'oldest_available_message_ref': <String, Object?>{
          'id': '79044282979201025',
          'origin_domain': 'remote.example',
        },
        'history_degraded_code': 'FEDERATED_DM_HISTORY_TRUNCATED',
      });
      expect(dm.historyTruncated, isTrue);
      expect(dm.historyRetention, 'rolling_replica_cache');
      expect(
        dm.oldestAvailableMessageRef,
        EntityRef.parse('79044282979201025@remote.example'),
      );

      final historyMessage = KaedeMessage.fromJson(<String, Object?>{
        'id': '79044282979201025',
        'origin_domain': 'remote.example',
        'channel_id': '79044282979201024',
        'channel_domain': 'kaede.chat',
        'author_id': '79044282979201027',
        'author_domain': 'remote.example',
        'created_at': '2026-08-12T12:00:00Z',
        'history_page_error_code': 'FEDERATED_DM_HISTORY_UNAVAILABLE',
        'history_page_retry_after_ms': 5000,
      });
      expect(historyMessage.historyPageErrorCode,
          'FEDERATED_DM_HISTORY_UNAVAILABLE');
      expect(historyMessage.historyPageRetryAfterMs, 5000);

      final guild = KaedeGuild.fromJson(<String, Object?>{
        'id': '79044282979201026',
        'origin_domain': 'remote.example',
        'name': 'Remote guild',
        'owner_id': '79044282979201027',
        'permissions': '0',
        'unavailable': false,
        'sync_status': 'quota_paused',
        'sync_error_code': 'KAED_FED_REPLICA_QUOTA_EXCEEDED',
        'history_sync_status': 'retrying',
        'history_sync_error_code': 'KAED_FED_HISTORY_CAPACITY',
        'history_sync_retry_after_ms': 60000,
      });
      expect(guild.syncStatus, 'quota_paused');
      expect(guild.syncErrorCode, 'KAED_FED_REPLICA_QUOTA_EXCEEDED');
      expect(guild.historySyncStatus, 'retrying');
      expect(guild.historySyncErrorCode, 'KAED_FED_HISTORY_CAPACITY');
      expect(guild.historySyncRetryAfterMs, 60000);
    });

    test('rejects non-canonical snowflakes and unsafe host input', () {
      for (final value in <String>[
        '0',
        '01',
        '-1',
        '9223372036854775808',
      ]) {
        expect(() => Snowflake(value), throwsFormatException, reason: value);
      }
      for (final value in <String>[
        'https://kaede.chat',
        'kaede.chat:443',
        'user@kaede.chat',
        '-bad.example',
        'bad_.example',
      ]) {
        expect(() => Domain(value), throwsFormatException, reason: value);
      }
    });
  });

  group('conversation restoration', () {
    final dm = KaedeChannel(
      ref: EntityRef.parse('10@home.example'),
      type: ChannelType.dm,
      position: 0,
      permissions: BigInt.zero,
    );
    final localText = KaedeChannel(
      ref: EntityRef.parse('10@remote.example'),
      type: ChannelType.text,
      position: 2,
      permissions: BigInt.zero,
    );
    final guild = KaedeGuild(
      ref: EntityRef.parse('20@home.example'),
      name: 'Guild',
      ownerRef: EntityRef.parse('30@home.example'),
      permissions: BigInt.zero,
      unavailable: false,
      channels: <KaedeChannel>[localText],
    );

    test('uses the exact saved composite reference', () {
      expect(
        resolveInitialConversation(
          saved: <String, Object?>{
            'id': '10',
            'origin_domain': 'remote.example',
          },
          dms: <KaedeChannel>[dm],
          guilds: <KaedeGuild>[guild],
        )?.ref,
        localText.ref,
      );
    });

    test('falls back deterministically when saved channel is inaccessible', () {
      expect(
        resolveInitialConversation(
          saved: '999@home.example',
          dms: <KaedeChannel>[dm],
          guilds: <KaedeGuild>[guild],
        )?.ref,
        dm.ref,
      );
    });
  });

  test('REST navigation refresh preserves live guild history warning', () {
    KaedeGuild guild({String? historyStatus, String? historyCode}) =>
        KaedeGuild(
          ref: EntityRef.parse('20@remote.example'),
          name: 'Remote guild',
          ownerRef: EntityRef.parse('30@remote.example'),
          permissions: BigInt.zero,
          unavailable: false,
          historySyncStatus: historyStatus,
          historySyncErrorCode: historyCode,
          historySyncRetryAfterMs: historyStatus == null ? null : 60000,
        );

    final refreshed = preserveGuildHistorySync(
      <KaedeGuild>[
        guild(
          historyStatus: 'retrying',
          historyCode: 'KAED_FED_HISTORY_CAPACITY',
        ),
      ],
      <KaedeGuild>[guild()],
    );

    expect(refreshed.single.historySyncStatus, 'retrying');
    expect(
      refreshed.single.historySyncErrorCode,
      'KAED_FED_HISTORY_CAPACITY',
    );
    expect(refreshed.single.historySyncRetryAfterMs, 60000);
  });

  group('guild navigation', () {
    KaedeGuild guild(String ref, String name) => KaedeGuild(
          ref: EntityRef.parse(ref),
          name: name,
          ownerRef: EntityRef.parse('30@home.example'),
          permissions: BigInt.zero,
          unavailable: false,
        );

    final guilds = <KaedeGuild>[
      guild('10@home.example', 'Local'),
      guild('10@remote.example', 'Remote'),
      guild('20@home.example', 'New'),
    ];

    test('preserves composite references and appends newly joined guilds', () {
      final navigation = GuildNavigation.fromJson(<String, Object?>{
        'items': <Object?>[
          <String, Object?>{
            'kind': 'group',
            'id': 'friends',
            'name': 'Friends',
            'guilds': <String>['10@remote.example', '99@gone.example'],
            'collapsed': true,
          },
          <String, Object?>{'kind': 'guild', 'guild': '10@home.example'},
        ],
      });

      final reconciled = reconcileGuildNavigation(navigation, guilds);
      expect(
        reconciled.toJson(),
        <String, Object?>{
          'items': <Object?>[
            <String, Object?>{
              'kind': 'group',
              'id': 'friends',
              'name': 'Friends',
              'guilds': <String>['10@remote.example'],
              'collapsed': true,
            },
            <String, Object?>{'kind': 'guild', 'guild': '10@home.example'},
            <String, Object?>{'kind': 'guild', 'guild': '20@home.example'},
          ],
        },
      );
    });

    test('creates groups and reorders top-level items without losing guilds',
        () {
      var navigation =
          reconcileGuildNavigation(const GuildNavigation(), guilds);
      navigation = createGuildNavigationGroup(
        navigation,
        'group_1',
        'Favorites',
        <EntityRef>[guilds[0].ref, guilds[1].ref],
      );
      navigation = reorderGuildNavigation(navigation, 1, 0);

      expect(navigation.items.first, isA<GuildNavigationGuildItem>());
      final group = navigation.items.last as GuildNavigationGroupItem;
      expect(group.guilds, <EntityRef>[guilds[0].ref, guilds[1].ref]);
      expect(
        ungroupGuildNavigation(navigation, group.id)
            .items
            .whereType<GuildNavigationGuildItem>()
            .map((item) => item.guild)
            .toSet(),
        guilds.map((guild) => guild.ref).toSet(),
      );
    });
  });

  test('attachment media URL includes the composite home reference', () {
    expect(
      attachmentMediaPath(EntityRef.parse('79044282979201024@kaede.chat')),
      '/media/kaede.chat/79044282979201024/original',
    );
    expect(
      attachmentMediaPath(
        EntityRef.parse('79044282979201024@kaede.chat'),
        historyMediaUrl: '/api/v1/dms/history/media/token',
      ),
      '/api/v1/dms/history/media/token',
    );
    const expired =
        '/api/v1/dms/43@home.example/history-media/50@remote.example/79044282979201024@kaede.chat/original?expires=1&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO';
    expect(
      attachmentMediaPath(
        EntityRef.parse('79044282979201024@kaede.chat'),
        historyMediaUrl: expired,
      ),
      expired,
    );
    expect(
      attachmentMediaPath(
        EntityRef.parse('79044282979201024@kaede.chat'),
        historyMediaUrl: '//evil.example/media',
      ),
      '/media/kaede.chat/79044282979201024/original',
    );
  });

  test('attachment status URL uses only the upload snowflake', () {
    expect(
      attachmentStatusPath(
        EntityRef.parse('79044282979201024@remote.example'),
      ),
      '/api/v1/attachments/79044282979201024',
    );
  });

  group('message composition', () {
    test('sends attachment ticket snowflakes without composite domains', () {
      expect(
        messageAttachmentIds(<EntityRef>[
          EntityRef.parse('42@home.example'),
          EntityRef.parse('73@remote.example'),
        ]),
        <String>['42', '73'],
      );
    });

    test('extracts unique composite mentions', () {
      expect(
        mentionReferences(
          'hello <@42@home.example> and <@73@remote.example> '
          '<@42@home.example>',
        ),
        <EntityRef>[
          EntityRef.parse('42@home.example'),
          EntityRef.parse('73@remote.example'),
        ],
      );
    });

    test('recognizes cacheable direct media previews', () {
      expect(
        previewMediaUrl('look https://static.example/cat.webp'),
        Uri.parse('https://static.example/cat.webp'),
      );
      expect(previewMediaUrl('https://example.test/page'), isNull);
    });
  });

  group('voice participant labels', () {
    test('prefers the LiveKit display name over an opaque identity', () {
      expect(
        voiceParticipantLabel(
          liveName: 'Turtle',
          identity: '79044282979201024@kaede.chat',
          knownName: 'Cached Turtle',
        ),
        'Turtle',
      );
    });

    test('uses a cached profile before falling back to the snowflake', () {
      expect(
        voiceParticipantLabel(
          liveName: '',
          identity: '79044282979201024@kaede.chat',
          knownName: 'Turtle',
        ),
        'Turtle',
      );
    });
  });

  test('generated protocol includes security-sensitive events and high bits',
      () {
    expect(protocolVersion, 1);
    expect(eventNames, contains('CHANNEL_ACCESS_REVOKED'));
    expect(eventNames, contains('VOICE_STATE_UPDATE'));
    expect(Permission.moderateMembers, 1 << 40);
    expect(Permission.banInstances, 1 << 41);
  });

  group('notification destinations', () {
    test('accepts only the current content-free FCM wake payload', () {
      final wake = OpaquePushWake.parse(<String, dynamic>{
        'sync_version': '1',
        'event_token': 'a' * 43,
      });

      expect(wake?.eventToken, 'a' * 43);
      for (final payload in <Map<String, dynamic>>[
        <String, dynamic>{},
        <String, dynamic>{
          'sync_version': '1',
          'event_token': 'short',
        },
        <String, dynamic>{
          'sync_version': '2',
          'event_token': 'a' * 43,
        },
        <String, dynamic>{
          'sync_version': '1',
          'event_token': 'a' * 43,
          'channel_ref': '42@chat.example',
        },
        <String, dynamic>{
          'kind': 'direct_message',
          'channel_ref': '42@chat.example',
          'message_ref': '73@remote.example',
        },
      ]) {
        expect(OpaquePushWake.parse(payload), isNull, reason: '$payload');
      }
    });

    test('accepts only an exact content-free relay wake payload', () {
      final wake = OpaquePushWake.parse(<String, dynamic>{
        'sync_version': '2',
        'route_id': 'r' * 43,
        'event_token': 'e' * 43,
        'delivery_id': 'd' * 43,
        'expires_at': '2000000000',
        'wake_mac': 'm' * 43,
      });

      expect(wake?.version, 2);
      expect(wake?.routeId, 'r' * 43);
      expect(wake?.deliveryId, 'd' * 43);
      expect(wake?.wakeMac, 'm' * 43);
      expect(
        OpaquePushWake.parse(<String, dynamic>{
          'sync_version': '2',
          'route_id': 'r' * 43,
          'event_token': 'e' * 43,
          'delivery_id': 'd' * 43,
          'expires_at': '2000000000',
          'wake_mac': 'm' * 43,
          'message_ref': '42@private.example',
        }),
        isNull,
      );
    });

    test('requires the configured transport and device MAC before redemption',
        () async {
      final legacy = OpaquePushWake.parse(<String, dynamic>{
        'sync_version': '1',
        'event_token': 'e' * 43,
      })!;
      expect(
        await authenticatePushWake(
          legacy,
          null,
          configuredTransport: 'relay',
        ),
        isFalse,
      );
      expect(
        await authenticatePushWake(
          legacy,
          null,
          configuredTransport: 'direct_fcm',
        ),
        isTrue,
      );

      final secretBytes = List<int>.generate(32, (index) => index);
      final secret = base64UrlEncode(secretBytes).replaceAll('=', '');
      const expiresAt = 2000000000;
      final canonical = utf8.encode(
        '2\n${'r' * 43}\n${'e' * 43}\n${'d' * 43}\n$expiresAt',
      );
      final calculated = await Hmac.sha256().calculateMac(
        canonical,
        secretKey: SecretKey(secretBytes),
      );
      final mac = base64UrlEncode(calculated.bytes).replaceAll('=', '');
      final relayWake = OpaquePushWake.parse(<String, dynamic>{
        'sync_version': '2',
        'route_id': 'r' * 43,
        'event_token': 'e' * 43,
        'delivery_id': 'd' * 43,
        'expires_at': '$expiresAt',
        'wake_mac': mac,
      })!;
      final state = RelayPushState(
        home: Domain('home.example'),
        relayUrl: Uri.parse('https://push.example'),
        relayOrigin: Domain('example'),
        subscriptionId: 'kps_${'s' * 40}',
        routeId: 'r' * 43,
        wakeSecret: secret,
        managementSecret: 'm' * 43,
      );
      expect(
        await authenticatePushWake(
          relayWake,
          state,
          configuredTransport: 'relay',
          nowEpochSeconds: expiresAt - 60,
        ),
        isTrue,
      );
      expect(
        await authenticatePushWake(
          relayWake,
          state,
          configuredTransport: 'relay',
          nowEpochSeconds: expiresAt + 1,
        ),
        isFalse,
      );
    });

    test('parses notification content only after authenticated redemption', () {
      final envelope = PushNotificationEnvelope.parse(<String, Object?>{
        'kind': 'mention',
        'title': 'Turtle in General',
        'body': 'Hello',
        'channel_ref': '42@chat.example',
        'message_ref': '73@remote.example',
        'sender_name': 'Turtle',
        'sender_ref': '9@remote.example',
        'sender_avatar_hash': 'a' * 64,
        'sent_at': '2026-08-11T11:42:00Z',
      });

      expect(envelope?.kind, NotificationKind.mention);
      expect(envelope?.destination.channel.wire, '42@chat.example');
      expect(envelope?.destination.message?.wire, '73@remote.example');
      expect(envelope?.senderName, 'Turtle');
      expect(envelope?.senderRef?.wire, '9@remote.example');
      expect(
        envelope?.senderAvatarUri.toString(),
        'https://remote.example/media/assets/${'a' * 64}/thumbnail_128?v=2',
      );
      expect(envelope?.sentAt, DateTime.utc(2026, 8, 11, 11, 42));
      expect(
        PushNotificationEnvelope.parse(<String, Object?>{
          'kind': 'mention',
          'title': 'Turtle in General',
          'body': 'Hello',
          'channel_ref': '42@chat.example',
        }),
        isNull,
      );
      expect(
        PushNotificationEnvelope.parse(<String, Object?>{
          'kind': 'mention',
          'title': 'Turtle in General',
          'body': 'Hello',
          'channel_ref': '42@chat.example',
          'message_ref': '73@remote.example',
          'sender_name': 'Turtle',
          'sender_ref': 'invalid',
          'sender_avatar_hash': '../avatar.png',
        }),
        isNull,
      );
    });

    test('notification IDs are deterministic and platform-safe', () {
      expect(stableNotificationId('73@remote.example'), 1372435255);
      expect(stableNotificationId('73@remote.example'),
          stableNotificationId('73@remote.example'));
      expect(stableNotificationId('74@remote.example'), isNot(1959121094));
      expect(
        stableNotificationId('73@remote.example'),
        inInclusiveRange(0, 0x7fffffff),
      );
    });

    test('round trips a composite channel and message identity', () {
      final destination = PushDestination(
        channel: EntityRef.parse('42@chat.example'),
        message: EntityRef.parse('73@remote.example'),
      );

      expect(PushDestination.parse(destination.encode())?.channel,
          destination.channel);
      expect(PushDestination.parse(destination.encode())?.message,
          destination.message);
      expect(
        PushDestination.parse(<String, String>{
          'channel_ref': destination.channel.wire,
          'message_ref': destination.message!.wire,
        })?.message,
        destination.message,
      );
      expect(
        PushDestination.fromUri(destination.toUri())?.channel,
        destination.channel,
      );
      expect(
        PushDestination.parse(destination.toUri().toString())?.message,
        destination.message,
      );
    });

    test('rejects malformed, local-only, and partial destinations', () {
      for (final value in <Object?>[
        null,
        '',
        '{}',
        '{not-json}',
        '42',
        <String, String>{'message_ref': '73@remote.example'},
        <String, String>{
          'channel_ref': '42@chat.example',
          'message_ref': 'bad',
        },
        Uri.parse('https://example.test/open?channel_ref=42@chat.example'),
        Uri.parse('kaede://other/open?channel_ref=42@chat.example'),
      ]) {
        expect(PushDestination.parse(value), isNull, reason: '$value');
      }
    });
  });

  group('read badges', () {
    test('acknowledges only when the selected conversation pane is visible',
        () {
      final selected = EntityRef.parse('42@chat.example');

      expect(
        shouldAcknowledgeVisibleChannel(
          appActive: true,
          conversationPaneVisible: false,
          selectedChannel: selected,
          channel: selected,
        ),
        isFalse,
      );
      expect(
        shouldAcknowledgeVisibleChannel(
          appActive: true,
          conversationPaneVisible: true,
          selectedChannel: selected,
          channel: selected,
        ),
        isTrue,
      );
      expect(
        shouldAcknowledgeVisibleChannel(
          appActive: true,
          conversationPaneVisible: true,
          selectedChannel: selected,
          channel: EntityRef.parse('43@chat.example'),
        ),
        isFalse,
      );
    });

    test('decodes unread booleans and mention counters', () {
      final channel = EntityRef.parse('42@chat.example');
      final badges = decodeReadBadgeSnapshot(<Map<String, Object?>>[
        <String, Object?>{
          'channel_id': channel.id.value,
          'channel_domain': channel.domain.value,
          'unread': true,
          'mention_count': 3,
        },
      ]);

      expect(badges.unread[channel], 1);
      expect(badges.mentions[channel], 3);
    });

    test('keeps equal snowflakes from different instances separate', () {
      final first = EntityRef.parse('42@one.example');
      final second = EntityRef.parse('42@two.example');
      final badges = decodeReadBadgeSnapshot(<Map<String, Object?>>[
        <String, Object?>{
          'channel_id': first.id.value,
          'channel_domain': first.domain.value,
          'unread_count': 2,
          'mention_count': 0,
        },
        <String, Object?>{
          'channel_id': second.id.value,
          'channel_domain': second.domain.value,
          'unread': false,
          'mention_count': 4,
        },
      ]);

      expect(badges.unread[first], 2);
      expect(badges.unread.containsKey(second), isFalse);
      expect(badges.mentions[second], 4);
    });

    test(
        'successful acknowledgement recovery replaces only its channel with the authoritative badge',
        () {
      final acknowledged = EntityRef.parse('42@chat.example');
      final newer = EntityRef.parse('43@chat.example');
      final cleared = reconcileAuthoritativeChannelBadge(
        currentUnread: <EntityRef, int>{acknowledged: 1, newer: 2},
        currentMentions: <EntityRef, int>{acknowledged: 1, newer: 1},
        authoritative: const ReadBadgeSnapshot(
          unread: <EntityRef, int>{},
          mentions: <EntityRef, int>{},
        ),
        channel: acknowledged,
      );

      expect(cleared.unread.containsKey(acknowledged), isFalse);
      expect(cleared.mentions.containsKey(acknowledged), isFalse);
      expect(cleared.unread[newer], 2);
      expect(cleared.mentions[newer], 1);

      final newerServerMessage = reconcileAuthoritativeChannelBadge(
        currentUnread: cleared.unread,
        currentMentions: cleared.mentions,
        authoritative: ReadBadgeSnapshot(
          unread: <EntityRef, int>{acknowledged: 1},
          mentions: const <EntityRef, int>{},
        ),
        channel: acknowledged,
      );
      expect(newerServerMessage.unread[acknowledged], 1);
      expect(newerServerMessage.unread[newer], 2);
    });

    test('keeps composite guild notification preferences distinct', () {
      final levels = decodeGuildNotificationLevels(<Map<String, Object?>>[
        <String, Object?>{
          'guild_id': '42',
          'guild_domain': 'one.example',
          'level': 'all',
        },
        <String, Object?>{
          'guild_id': '42',
          'guild_domain': 'two.example',
          'level': 'none',
        },
      ]);

      expect(levels['42@one.example'], 'all');
      expect(levels['42@two.example'], 'none');
    });

    test('health and degraded warnings clear independently on recovery', () {
      const initial = MobileState(
        phase: SessionPhase.ready,
        gatewayHealth: GatewayHealth(
          GatewayConnectionPhase.reconnecting,
          message: 'Retrying realtime updates…',
        ),
        degradedWarnings: <DegradedFeature, String>{
          DegradedFeature.readStates: 'Unread markers are incomplete.',
        },
        gatewayProtocolWarning: 'Invalid realtime data.',
        pushWarning: 'Notifications need attention.',
      );
      final recovered = initial.copyWith(
        gatewayHealth: const GatewayHealth(GatewayConnectionPhase.connected),
        degradedWarnings: const <DegradedFeature, String>{},
        clearGatewayProtocolWarning: true,
        clearPushWarning: true,
      );

      expect(recovered.gatewayHealth.isConnected, isTrue);
      expect(recovered.degradedWarnings, isEmpty);
      expect(recovered.gatewayProtocolWarning, isNull);
      expect(recovered.pushWarning, isNull);
    });

    test('push relay migration requires the same enabled installation', () {
      final devices = <Map<String, Object?>>[
        <String, Object?>{'id': 'old-device', 'enabled': true},
        <String, Object?>{'id': 'this-device', 'enabled': false},
      ];

      expect(hasRegisteredPushInstallation(devices, 'this-device'), isFalse);
      expect(hasRegisteredPushInstallation(devices, 'old-device'), isTrue);
      expect(hasRegisteredPushInstallation(devices, 'unknown-device'), isFalse);
    });
  });

  group('local message notification policy', () {
    LocalMessageNotificationDecision decide({
      bool own = false,
      bool dnd = false,
      bool visible = false,
      bool dm = false,
      bool mentioned = false,
      bool dmEnabled = true,
      bool mentionsEnabled = true,
      String guildLevel = 'mentions',
    }) =>
        decideLocalMessageNotification(
          authoredByCurrentUser: own,
          doNotDisturb: dnd,
          conversationIsVisible: visible,
          isDirectMessage: dm,
          mentionsCurrentUser: mentioned,
          directMessagesEnabled: dmEnabled,
          mentionsEnabled: mentionsEnabled,
          guildNotificationLevel: guildLevel,
        );

    test('suppresses own, visible, and do-not-disturb messages', () {
      expect(
          decide(own: true, dm: true), LocalMessageNotificationDecision.none);
      expect(decide(visible: true, dm: true),
          LocalMessageNotificationDecision.none);
      expect(
          decide(dnd: true, dm: true), LocalMessageNotificationDecision.none);
    });

    test('honors direct-message and mention category switches', () {
      expect(decide(dm: true), LocalMessageNotificationDecision.directMessage);
      expect(decide(dm: true, dmEnabled: false),
          LocalMessageNotificationDecision.none);
      expect(decide(dm: true, dmEnabled: false, mentioned: true),
          LocalMessageNotificationDecision.none);
      expect(decide(mentioned: true), LocalMessageNotificationDecision.mention);
      expect(decide(mentioned: true, mentionsEnabled: false),
          LocalMessageNotificationDecision.none);
    });

    test('notifies ordinary guild messages only for all messages', () {
      expect(decide(guildLevel: 'all'),
          LocalMessageNotificationDecision.guildMessage);
      expect(decide(guildLevel: 'mentions'),
          LocalMessageNotificationDecision.none);
      expect(decide(guildLevel: 'none'), LocalMessageNotificationDecision.none);
    });
  });
}
