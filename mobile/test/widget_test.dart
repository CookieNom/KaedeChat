import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/voice/voice_room.dart';
import 'package:kaede_mobile/src/platform/notification_policy.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

void main() {
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

  test('attachment media URL includes the composite home reference', () {
    expect(
      attachmentMediaPath(EntityRef.parse('79044282979201024@kaede.chat')),
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
