import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/thread_permissions.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/features/chat/forum_channel_view.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

void main() {
  group('forum and thread models', () {
    test('decodes every Discord thread/forum channel type and flat fields', () {
      expect(channelType(10), ChannelType.announcementThread);
      expect(channelType(11), ChannelType.publicThread);
      expect(channelType(12), ChannelType.privateThread);
      expect(channelType(15), ChannelType.forum);
      expect(channelType(3), ChannelType.groupDm);

      final forum = KaedeChannel.fromJson(_channelJson(
        id: '15',
        type: 15,
        extra: <String, Object?>{
          'flags': 16,
          'e2ee_required': true,
          'default_auto_archive_duration': 4320,
          'default_thread_rate_limit_per_user': 30,
          'default_sort_order': 1,
          'default_forum_layout': 2,
          'default_reaction_emoji': <String, Object?>{
            'emoji_id': null,
            'emoji_name': '👍',
          },
          'available_tags': <Object?>[
            <String, Object?>{
              'id': '7',
              'name': 'Solved',
              'moderated': true,
              'emoji_name': '✅',
            },
          ],
        },
      ));

      expect(forum.isForum, isTrue);
      expect(forum.e2eeRequired, isTrue);
      expect(forum.defaultAutoArchiveDuration, 4320);
      expect(forum.defaultThreadRateLimitPerUser, 30);
      expect(forum.availableTags.single.name, 'Solved');
      expect(forum.availableTags.single.moderated, isTrue);

      final thread = KaedeChannel.fromJson(_channelJson(
        id: '11',
        type: 11,
        extra: <String, Object?>{
          'parent_id': '15',
          'parent_domain': 'chat.example',
          'created_at': '2026-08-20T09:30:00Z',
          'archived': true,
          'locked': true,
          'invitable': false,
          'auto_archive_duration': 1440,
          'message_count': 4,
          'member_count': 2,
          'applied_tag_ids': <String>['7'],
          'starter_message': _messageJson('111', channelId: '11'),
        },
      ));

      expect(thread.isThread, isTrue);
      expect(thread.archived, isTrue);
      expect(thread.locked, isTrue);
      expect(thread.invitable, isFalse);
      expect(thread.appliedTagIds, <String>['7']);
      expect(thread.createdAt, DateTime.utc(2026, 8, 20, 9, 30));
      expect(thread.copyWith().createdAt, thread.createdAt);
      expect(thread.toJson()['created_at'], '2026-08-20T09:30:00.000Z');
      expect(thread.copyWith(clearCreatedAt: true).createdAt, isNull);
      expect(thread.starterMessage?.content, 'message-111');
      expect(thread.toJson()['type'], 11);
    });

    test('decodes thread membership and message thread projections', () {
      final page = ThreadPage.fromJson(<String, Object?>{
        'threads': <Object?>[
          _channelJson(id: '11', type: 11),
        ],
        'members': <Object?>[
          <String, Object?>{
            'id': '11',
            'thread_domain': 'chat.example',
            'user_id': '42',
            'user_domain': 'chat.example',
            'join_timestamp': '2026-08-24T10:00:00Z',
            'flags': 1,
            'notification_level': 'mentions',
          },
        ],
        'has_more': true,
        'next_cursor': 'opaque-next-page',
      });
      final projected = KaedeMessage.fromJson(_messageJson(
        '20',
        channelId: '3',
        extra: <String, Object?>{
          'message_type': 18,
          'thread': _channelJson(id: '11', type: 11),
        },
      ));

      expect(page.hasMore, isTrue);
      expect(page.nextCursor, 'opaque-next-page');
      expect(page.members.single.userRef.wire, '42@chat.example');
      expect(page.members.single.notificationLevel, 'mentions');
      expect(page.members.single.toJson()['notification_level'], 'mentions');
      expect(page.members.single.toJson().containsKey('muted'), isFalse);
      expect(projected.messageType, 18);
      expect(projected.thread?.ref.wire, '11@chat.example');
      expect(projected.copyWith().thread?.ref, projected.thread?.ref);
      expect(
        (projected.toJson()['thread'] as Map<String, Object?>)['type'],
        11,
      );
    });

    test('channel updates activate E2EE without dropping thread access data',
        () {
      final current = KaedeChannel.fromJson(_channelJson(
        id: '11',
        type: 11,
        extra: <String, Object?>{
          'parent_id': '15',
          'parent_domain': 'chat.example',
          'permissions': '274877910016',
          'encryption_mode': 'e2ee',
          'encryption_state': 'required',
          'encryption_policy_generation': '1',
          'starter_message': _messageJson('11', channelId: '11'),
          'member': <String, Object?>{
            'id': '11',
            'thread_domain': 'chat.example',
            'user_id': '42',
            'user_domain': 'chat.example',
            'join_timestamp': '2026-08-24T10:00:00Z',
            'notification_level': 'all',
          },
        },
      ));

      final activated = mergeThreadChannelUpdate(
        current,
        <String, Object?>{
          'id': '11',
          'origin_domain': 'chat.example',
          'type': 11,
          'encryption_mode': 'e2ee',
          'encryption_state': 'active',
          'encryption_policy_generation': '2',
          'encryption_protocol': 'mls10',
          'encryption_suite': 'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519',
          'encryption_group_id': 'thread-group',
          'encryption_epoch': '0',
          'encryption_activated_at': '2026-08-24T10:01:00Z',
          // Broadcast updates are not authoritative for these projections.
          'permissions': '0',
          'member': null,
          'starter_message': null,
        },
      );

      expect(activated.encryptionState, 'active');
      expect(activated.encryptionPolicyGeneration, 2);
      expect(activated.encryptionGroupId, 'thread-group');
      expect(activated.permissions, current.permissions);
      expect(activated.member?.userRef, current.member?.userRef);
      expect(activated.member?.notificationLevel, 'all');
      expect(activated.starterMessage?.content, 'message-11');

      final revoked = applyThreadPermissionUpdate(activated, '0');
      final granted = applyThreadPermissionUpdate(
        revoked,
        Permission.viewChannel | Permission.sendMessagesInThreads,
      );
      expect(revoked.permissions, BigInt.zero);
      expect(granted.allows(Permission.viewChannel), isTrue);
      expect(granted.allows(Permission.sendMessagesInThreads), isTrue);
      expect(granted.member?.notificationLevel, 'all');
      expect(granted.starterMessage?.content, 'message-11');
    });

    test('required thread projections stay fail-closed until E2EE is active',
        () {
      KaedeChannel required({
        String mode = 'plaintext',
        String state = 'plaintext',
      }) =>
          KaedeChannel(
            ref: EntityRef.parse('11@chat.example'),
            type: ChannelType.publicThread,
            guildRef: EntityRef.parse('1@chat.example'),
            position: 0,
            permissions: BigInt.from(Permission.sendMessagesInThreads),
            e2eeRequired: true,
            encryptionMode: mode,
            encryptionState: state,
          );

      expect(channelEncryptionPaused(required()), isTrue);
      expect(
        channelEncryptionPaused(required(mode: 'e2ee', state: 'pending')),
        isTrue,
      );
      expect(
        channelEncryptionPaused(required(mode: 'e2ee', state: 'active')),
        isFalse,
      );
      expect(
        channelEncryptionPaused(KaedeChannel(
          ref: EntityRef.parse('12@chat.example'),
          type: ChannelType.publicThread,
          position: 0,
          permissions: BigInt.zero,
        )),
        isFalse,
      );
    });

    test('decodes and renders Discord type-21 nested thread sources', () {
      final wrapper = KaedeMessage.fromThreadStarterJson(
        _messageJson(
          '90',
          channelId: '11',
          extra: <String, Object?>{
            'content': null,
            'message_type': 21,
            'attachments': const <Object?>[],
            'referenced_message': _messageJson(
              '90',
              channelId: '3',
              extra: const <String, Object?>{'content': 'Source body'},
            ),
          },
        ),
        thread: EntityRef.parse('11@chat.example'),
        parent: EntityRef.parse('3@chat.example'),
      );

      expect(wrapper.content, isNull);
      expect(wrapper.contentUnavailable, isFalse);
      expect(wrapper.referencedMessage?.channelRef.wire, '3@chat.example');
      expect(wrapper.referencedMessage?.content, 'Source body');
      expect(threadStarterDisplayMessage(wrapper).content, 'Source body');

      final roundTripped = KaedeMessage.fromJson(wrapper.toJson());
      expect(roundTripped.referencedMessage?.content, 'Source body');
    });

    test('renders a redacted or missing type-21 source as unavailable', () {
      final redacted = KaedeMessage.fromJson(_messageJson(
        '90',
        channelId: '11',
        extra: <String, Object?>{
          'content': null,
          'message_type': 21,
          'referenced_message': _messageJson(
            '90',
            channelId: '3',
            extra: const <String, Object?>{
              'content': null,
              'content_unavailable': true,
            },
          ),
        },
      ));
      final missing = KaedeMessage.fromJson(_messageJson(
        '91',
        channelId: '11',
        extra: const <String, Object?>{
          'content': null,
          'message_type': 21,
        },
      ));

      expect(threadStarterDisplayMessage(redacted).contentUnavailable, isTrue);
      expect(threadStarterDisplayMessage(missing).contentUnavailable, isTrue);
    });

    test('adds a detached source once and deduplicates native starters', () {
      final starter =
          KaedeMessage.fromJson(_messageJson('90', channelId: '11'));
      final reply = KaedeMessage.fromJson(_messageJson('91', channelId: '11'));
      final thread = KaedeChannel.fromJson(_channelJson(
        id: '11',
        type: 11,
        extra: <String, Object?>{'starter_message': starter.toJson()},
      ));

      expect(
        threadTimelineMessages(thread, <KaedeMessage>[reply])
            .map((message) => message.ref.id.value),
        <String>['90', '91'],
      );
      expect(
        threadTimelineMessages(thread, <KaedeMessage>[starter, reply]).length,
        2,
      );
    });

    test('tolerates a partial retained-history thread source', () {
      final thread = KaedeChannel.fromJson(_channelJson(
        id: '11',
        type: 11,
        extra: const <String, Object?>{
          'parent_id': '3',
          'parent_domain': 'chat.example',
          'starter_message': <String, Object?>{
            'content_unavailable': true,
          },
        },
      ));

      final starter = thread.starterMessage!;
      expect(starter.contentUnavailable, isTrue);
      expect(starter.createdAtAvailable, isFalse);
      expect(starter.author, isNull);
      expect(starter.channelRef, EntityRef.parse('3@chat.example'));
      expect(threadTimelineMessages(thread, const <KaedeMessage>[]),
          <KaedeMessage>[starter]);

      final roundTripped = KaedeChannel.fromJson(thread.toJson());
      expect(roundTripped.createdAt, isNull);
      expect(roundTripped.starterMessage?.createdAtAvailable, isFalse);
    });
  });

  group('Discord permission splits', () {
    KaedeChannel channel(
      ChannelType type,
      int permissions, {
      bool archived = false,
      bool invitable = true,
      bool e2eeRequired = false,
      ThreadMember? member,
      EntityRef? ownerRef,
    }) =>
        KaedeChannel(
          ref: EntityRef.parse('15@chat.example'),
          type: type,
          guildRef: EntityRef.parse('1@chat.example'),
          position: 0,
          permissions: BigInt.from(permissions),
          archived: archived,
          invitable: invitable,
          e2eeRequired: e2eeRequired,
          member: member,
          ownerRef: ownerRef,
        );

    test('forum new posts require view and send but ignore create-thread', () {
      final send = Permission.sendMessages;
      final view = Permission.viewChannel;
      const create = Permission.createPublicThreads;

      expect(canCreateForumPost(channel(ChannelType.forum, send)), isFalse);
      expect(
          canCreateForumPost(channel(ChannelType.forum, view | send)), isTrue);
      expect(canCreateForumPost(channel(ChannelType.forum, view | create)),
          isFalse);
    });

    test('public/private creation and replies use independent grants', () {
      const public = Permission.createPublicThreads;
      const private = Permission.createPrivateThreads;
      const replies = Permission.sendMessagesInThreads;
      const manage = Permission.manageThreads;
      final view = Permission.viewChannel;

      expect(canCreatePublicThread(channel(ChannelType.text, public)), isFalse);
      expect(
          canCreatePrivateThread(channel(ChannelType.text, private)), isFalse);
      expect(
        canCreatePublicThread(channel(ChannelType.text, view | public)),
        isTrue,
      );
      expect(
        canCreatePrivateThread(channel(ChannelType.text, view | private)),
        isTrue,
      );
      expect(
        hasSendMessagesInThreads(channel(ChannelType.publicThread, replies)),
        isTrue,
      );
      expect(
        canManageThreads(channel(ChannelType.publicThread, manage)),
        isTrue,
      );
      expect(
          canCreatePublicThread(channel(ChannelType.text, replies)), isFalse);
    });

    test(
        'message-scoped thread creation remains available in encrypted parents',
        () {
      final permissions =
          Permission.viewChannel | Permission.createPublicThreads;
      final plaintext = channel(ChannelType.text, permissions);
      final encrypted = plaintext.copyWith(encryptionMode: 'e2ee');

      expect(canStartThreadFromMessage(plaintext), isTrue);
      expect(canStartThreadFromMessage(encrypted), isTrue);
      expect(deferThreadStarterUntilE2eeActive(plaintext), isFalse);
      expect(deferThreadStarterUntilE2eeActive(encrypted), isTrue);
      expect(
        deferThreadStarterUntilE2eeActive(
          channel(
            ChannelType.forum,
            permissions,
            e2eeRequired: true,
          ),
        ),
        isTrue,
      );
    });

    test('adding members follows public and private thread rules', () {
      const send = Permission.sendMessagesInThreads;
      const manage = Permission.manageThreads;
      final membership = ThreadMember(
        threadRef: EntityRef.parse('15@chat.example'),
        userRef: EntityRef.parse('42@chat.example'),
        joinTimestamp: DateTime.utc(2026, 8, 24),
      );

      expect(
          canAddThreadMember(channel(ChannelType.publicThread, send)), isTrue);
      expect(
        canAddThreadMember(channel(ChannelType.announcementThread, send)),
        isTrue,
      );
      expect(canAddThreadMember(channel(ChannelType.publicThread, manage)),
          isFalse);
      expect(
        canAddThreadMember(
          channel(ChannelType.publicThread, send, archived: true),
        ),
        isFalse,
      );
      expect(
        canAddThreadMember(channel(
          ChannelType.privateThread,
          send,
          member: membership,
        )),
        isTrue,
      );
      expect(
        canAddThreadMember(channel(ChannelType.privateThread, send)),
        isFalse,
      );
      expect(
        canAddThreadMember(channel(
          ChannelType.privateThread,
          send,
          invitable: false,
          member: membership,
        )),
        isFalse,
      );
      expect(
        canAddThreadMember(channel(
          ChannelType.privateThread,
          manage,
          invitable: false,
        )),
        isFalse,
      );
      expect(
        canAddThreadMember(channel(
          ChannelType.privateThread,
          manage | send,
          invitable: false,
        )),
        isTrue,
      );
    });

    test('mobile honors command, pin, slowmode, and reaction splits', () {
      final denied = channel(ChannelType.text, 0);
      final granted = channel(
        ChannelType.text,
        Permission.useApplicationCommands |
            Permission.pinMessages |
            Permission.bypassSlowmode |
            Permission.addReactions,
      );

      expect(canUseApplicationCommands(denied), isFalse);
      expect(canPinMessages(denied), isFalse);
      expect(canBypassSlowmode(denied), isFalse);
      expect(canAddMessageReaction(denied, emojiExists: false), isFalse);
      expect(canAddMessageReaction(denied, emojiExists: true), isTrue);

      expect(canUseApplicationCommands(granted), isTrue);
      expect(canPinMessages(granted), isTrue);
      expect(canBypassSlowmode(granted), isTrue);
      expect(canAddMessageReaction(granted, emojiExists: false), isTrue);
      expect(
        canPinMessages(
          channel(ChannelType.text, Permission.pinMessages, archived: true),
        ),
        isFalse,
      );
      expect(
        canAddMessageReaction(
          channel(ChannelType.text, Permission.addReactions, archived: true),
          emojiExists: true,
        ),
        isFalse,
      );
    });

    test('pin eligibility matches direct and guild channel types', () {
      KaedeChannel direct(ChannelType type) => KaedeChannel(
            ref: EntityRef.parse('15@chat.example'),
            type: type,
            guildRef: null,
            position: 0,
            permissions: BigInt.zero,
          );

      expect(canPinMessages(direct(ChannelType.dm)), isTrue);
      expect(canPinMessages(direct(ChannelType.groupDm)), isTrue);
      expect(canPinMessages(direct(ChannelType.text)), isFalse);

      for (final type in <ChannelType>[
        ChannelType.text,
        ChannelType.announcement,
        ChannelType.announcementThread,
        ChannelType.publicThread,
        ChannelType.privateThread,
        ChannelType.forum,
        ChannelType.tracker,
      ]) {
        expect(canPinMessages(channel(type, Permission.pinMessages)), isTrue);
      }
      for (final type in <ChannelType>[
        ChannelType.voice,
        ChannelType.stage,
        ChannelType.category,
      ]) {
        expect(canPinMessages(channel(type, Permission.pinMessages)), isFalse);
      }
    });

    test('voice messages depend on permissions, not guild member count', () {
      final permitted = Permission.attachFiles | Permission.sendVoiceMessages;
      expect(canSendVoiceMessage(channel(ChannelType.text, permitted)), isTrue);
      expect(
        canSendVoiceMessage(channel(ChannelType.text, Permission.attachFiles)),
        isFalse,
      );
      expect(
        canSendVoiceMessage(
          channel(ChannelType.text, permitted, archived: true),
        ),
        isFalse,
      );

      KaedeChannel direct(ChannelType type) => KaedeChannel(
            ref: EntityRef.parse('16@chat.example'),
            type: type,
            guildRef: null,
            position: 0,
            permissions: BigInt.zero,
          );
      expect(canSendVoiceMessage(direct(ChannelType.dm)), isTrue);
      expect(canSendVoiceMessage(direct(ChannelType.groupDm)), isTrue);
    });

    test('only managers and private-thread creators can remove members', () {
      const manage = Permission.manageThreads;
      final creator = EntityRef.parse('42@chat.example');
      final stranger = EntityRef.parse('43@chat.example');

      expect(
        canRemoveThreadMember(
          channel(ChannelType.publicThread, manage),
          stranger,
        ),
        isTrue,
      );
      expect(
        canRemoveThreadMember(
          channel(ChannelType.privateThread, 0, ownerRef: creator),
          creator,
        ),
        isTrue,
      );
      expect(
        canRemoveThreadMember(
          channel(ChannelType.privateThread, 0, ownerRef: creator),
          stranger,
        ),
        isFalse,
      );
      expect(
        canRemoveThreadMember(channel(ChannelType.privateThread, 0), null),
        isFalse,
      );
      expect(
        canRemoveThreadMember(
          channel(
            ChannelType.privateThread,
            manage,
            archived: true,
            ownerRef: creator,
          ),
          creator,
        ),
        isFalse,
      );
    });

    test('matches aggregate removals by exact federated user identity', () {
      final user = EntityRef.parse('42@users.example');
      expect(
        threadMembersUpdateRemovesUser(<String, Object?>{
          'removed_member_refs': <Object?>[
            <String, Object?>{'id': '42', 'origin_domain': 'users.example'},
          ],
        }, user),
        isTrue,
      );
      expect(
        threadMembersUpdateRemovesUser(<String, Object?>{
          'removed_member_ids': <String>['42'],
          'removed_member_refs': <Object?>[
            <String, Object?>{'id': '42', 'origin_domain': 'other.example'},
          ],
        }, user),
        isFalse,
      );
      expect(
        threadMembersUpdateRemovesUser(<String, Object?>{
          'removed_member_ids': <String>['42'],
        }, user),
        isTrue,
      );
    });
  });

  group('forum view behavior', () {
    test('post draft accepts text or an attachment within Discord limits', () {
      expect(
        canSubmitForumPost(
          title: 'Screenshot',
          content: '',
          attachmentCount: 1,
          requiresTag: false,
          selectedTagCount: 0,
        ),
        isTrue,
      );
      expect(
        canSubmitForumPost(
          title: 'Text post',
          content: List<String>.filled(2000, 'm').join(),
          attachmentCount: 0,
          requiresTag: true,
          selectedTagCount: 1,
        ),
        isTrue,
      );
      expect(
        canSubmitForumPost(
          title: 'Too long',
          content: List<String>.filled(2001, 'm').join(),
          attachmentCount: 0,
          requiresTag: false,
          selectedTagCount: 0,
        ),
        isFalse,
      );
      expect(
        canSubmitForumPost(
          title: 'Empty',
          content: '   ',
          attachmentCount: 0,
          requiresTag: false,
          selectedTagCount: 0,
        ),
        isFalse,
      );
    });

    test('keeps the unified server order across pagination', () {
      final current = <KaedeChannel>[
        _post('2', pinned: true, createdAt: '2026-08-23T09:00:00Z'),
        _post('1', createdAt: '2026-08-24T09:00:00Z'),
      ];
      final next = <KaedeChannel>[
        _post('3', archived: true, createdAt: '2026-08-22T09:00:00Z'),
      ];

      expect(
        mergeForumPostPages(current, next).map((post) => post.ref.id.value),
        <String>['2', '1', '3'],
      );
    });

    test('does not re-sort a globally ordered date-posted page', () {
      final posts = mergeForumPostPages(
        <KaedeChannel>[
          _post(
            '2',
            archived: true,
            createdAt: '2026-08-24T09:00:00Z',
            starterCreatedAt: '2026-08-20T09:00:00Z',
          ),
          _post(
            '1',
            createdAt: '2026-08-22T09:00:00Z',
            starterCreatedAt: '2026-08-25T09:00:00Z',
          ),
        ],
        const <KaedeChannel>[],
      );

      expect(posts.map((post) => post.ref.id.value), <String>['2', '1']);
    });

    test('forum feed revision changes only for the selected parent', () {
      final forum = EntityRef.parse('15@chat.example');
      final first = _post('1', createdAt: '2026-08-24T09:00:00Z');
      final changed = first.copyWith(messageCount: 2, version: 'v2');
      final editedStarter = first.copyWith(
        starterMessage: first.starterMessage?.copyWith(
          content: 'Edited preview',
          editedAt: DateTime.utc(2026, 8, 24, 10),
        ),
      );
      final unrelated = KaedeChannel.fromJson(_channelJson(
        id: '9',
        type: 11,
        extra: <String, Object?>{
          'parent_id': '99',
          'parent_domain': 'chat.example',
        },
      ));

      expect(
        forumThreadFeedRevision(<KaedeChannel>[first], forum),
        isNot(forumThreadFeedRevision(<KaedeChannel>[changed], forum)),
      );
      expect(
        forumThreadFeedRevision(<KaedeChannel>[first], forum),
        isNot(
          forumThreadFeedRevision(<KaedeChannel>[editedStarter], forum),
        ),
      );
      expect(
        forumThreadFeedRevision(<KaedeChannel>[first], forum),
        forumThreadFeedRevision(<KaedeChannel>[first, unrelated], forum),
      );
    });

    testWidgets('custom forum tag IDs render through the emoji asset widget',
        (tester) async {
      const tag = ForumTag(id: '7', name: 'Solved', emojiId: '88');
      expect(
        forumTagCustomEmojiRef(tag, Domain('chat.example'))?.wire,
        '88@chat.example',
      );

      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ForumTagLabel(
            tag: tag,
            originDomain: Domain('chat.example'),
          ),
        ),
      ));
      expect(find.byType(CustomEmojiImage), findsOneWidget);
      expect(find.text('Solved'), findsOneWidget);
    });

    test('forum draft keeps required tags, defaults, and custom reaction', () {
      final draft = GuildChannelDraft(
        name: 'support',
        topic: 'Search before posting.',
        type: ChannelType.forum,
        slowModeSeconds: 30,
        flags: 16,
        availableTags: const <ForumTag>[
          ForumTag(id: '7', name: 'Solved', moderated: true),
        ],
        defaultReactionEmoji: const <String, Object?>{
          'emoji_id': '99',
          'emoji_name': 'support',
        },
        defaultThreadRateLimitPerUser: 10,
        defaultAutoArchiveDuration: 4320,
        defaultSortOrder: 1,
        defaultForumLayout: 2,
        e2eeRequired: true,
      );

      expect(draft.json['type'], 15);
      expect(draft.json['flags'], 16);
      expect(draft.json['e2ee_required'], isTrue);
      expect(
        draft.json['default_reaction_emoji'],
        <String, Object?>{'emoji_id': '99'},
      );
      expect(
        ForumTag(
          id: '7',
          name: 'Solved',
          emojiId: '88',
          emojiName: '✅',
        ).toJson(),
        <String, Object?>{
          'id': '7',
          'name': 'Solved',
          'moderated': false,
          'emoji_id': '88',
        },
      );
    });
  });

  group('native /thread command', () {
    test('requires name and first-message options', () {
      final parsed = parseNativeThreadCommand(
        '/thread name:"release notes" message:"Ship it today"',
      );

      expect(parsed?.name, 'release notes');
      expect(parsed?.message, 'Ship it today');
      expect(parseNativeThreadCommand('/thread name:test'), isNull);
      expect(
          parseNativeThreadCommand('/thread message:test name:test'), isNull);
    });

    test('enforces Discord title and message limits', () {
      expect(
        parseNativeThreadCommand(
          '/thread name:${List<String>.filled(101, 'n').join()} message:hello',
        ),
        isNull,
      );
      expect(
        parseNativeThreadCommand(
          '/thread name:test message:${List<String>.filled(4001, 'm').join()}',
        ),
        isNull,
      );
    });
  });

  group('thread REST contract', () {
    test('creates an inherited-E2EE child without a plaintext starter',
        () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_channelJson(
        id: '11',
        type: 11,
      )));
      final repository = _repository(adapter);

      await repository.createThread(
        parent: EntityRef.parse('3@chat.example'),
        name: 'Encrypted child',
        type: 11,
      );

      final data = adapter.request!.data as Map<String, Object?>;
      expect(data.containsKey('message'), isFalse);
    });

    test('reserves and claims an encrypted forum starter without plaintext',
        () async {
      final reservationAdapter =
          _RecordingJsonAdapter(jsonEncode(<String, Object?>{
        ..._channelJson(
          id: '11',
          type: 11,
          extra: <String, Object?>{
            'e2ee_required': true,
            'encryption_state': 'plaintext',
          },
        ),
        'starter_message': null,
        'message': null,
        'starter_reservation': <String, Object?>{
          'client_nonce': 'forum-reservation-1',
          'claimed': false,
        },
      }));
      final repository = _repository(reservationAdapter);

      final reserved = await repository.reserveEncryptedForumThread(
        parent: EntityRef.parse('15@chat.example'),
        name: 'Encrypted post',
        clientNonce: 'forum-reservation-1',
        appliedTagIds: const <String>['7'],
      );
      expect(reserved.channel.ref.wire, '11@chat.example');
      expect(reserved.claimed, isFalse);
      final reservationData =
          reservationAdapter.request!.data as Map<String, Object?>;
      expect(
          reservationData['starter_reservation_nonce'], 'forum-reservation-1');
      expect(reservationData.containsKey('message'), isFalse);

      final claimAdapter = _RecordingJsonAdapter(
        jsonEncode(_messageJson('11', channelId: '11')),
      );
      await _repository(claimAdapter).claimEncryptedForumStarter(
        thread: EntityRef.parse('11@chat.example'),
        clientNonce: 'forum-reservation-1',
        e2ee: <String, Object?>{'rich_payload_digest': 'digest'},
        attachments: <EntityRef>[EntityRef.parse('80@chat.example')],
        mentionUsers: <EntityRef>[EntityRef.parse('42@remote.example')],
      );
      expect(
        claimAdapter.request?.path,
        '/api/v1/channels/11@chat.example/starter',
      );
      final claimData = claimAdapter.request!.data as Map<String, Object?>;
      expect(claimData['content'], isNull);
      expect(claimData['client_nonce'], 'forum-reservation-1');
      expect(claimData['attachment_ids'], <String>['80']);
      expect(claimData['mention_user_ids'], <String>['42@remote.example']);
    });

    test('creates a canonical atomic starter payload', () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_channelJson(
        id: '11',
        type: 11,
      )));
      final repository = _repository(adapter);

      await repository.createThread(
        parent: EntityRef.parse('3@chat.example'),
        name: ' Test ',
        content: ' First message ',
        type: 11,
        autoArchiveDuration: 1440,
        rateLimitPerUser: 10,
        attachments: <EntityRef>[EntityRef.parse('80@chat.example')],
        mentionUsers: <EntityRef>[EntityRef.parse('42@remote.example')],
        nonce: 'nonce-1',
      );

      expect(adapter.request?.method, 'POST');
      expect(
        adapter.request?.path,
        '/api/v1/channels/3@chat.example/threads',
      );
      final data = adapter.request!.data as Map<String, Object?>;
      expect(data['name'], 'Test');
      final message = data['message'] as Map<String, Object?>;
      expect(message['content'], 'First message');
      expect(message['attachment_ids'], <String>['80']);
      expect(message['mention_user_ids'], <String>['42@remote.example']);
      expect(message['client_nonce'], 'nonce-1');
    });

    test('encodes title search and OR tags as repeated query keys', () async {
      final adapter = _RecordingJsonAdapter(
        '{"threads":[],"members":[],"has_more":false}',
      );
      final repository = _repository(adapter);

      await repository.threads(
        EntityRef.parse('15@chat.example'),
        includeArchived: true,
        tagIds: const <String>['7', '8'],
        query: ' outage ',
        sortOrder: 0,
      );

      expect(adapter.request?.method, 'GET');
      expect(adapter.request?.queryParameters['query'], 'outage');
      expect(adapter.request?.uri.queryParametersAll['tag_id'],
          <String>['7', '8']);
      expect(adapter.request?.queryParameters['sort_order'], 0);
      expect(adapter.request?.queryParameters['include_archived'], isTrue);
      expect(adapter.request?.queryParameters.containsKey('archived'), isFalse);
    });

    test('passes opaque thread cursors unchanged and suppresses legacy before',
        () async {
      final adapter = _RecordingJsonAdapter(
        '{"threads":[],"members":[],"has_more":false}',
      );
      final repository = _repository(adapter);

      await repository.threads(
        EntityRef.parse('15@chat.example'),
        cursor: 'opaque.cursor/+==',
        before: DateTime.utc(2026, 8, 24),
      );

      expect(adapter.request?.queryParameters['cursor'], 'opaque.cursor/+==');
      expect(adapter.request?.queryParameters.containsKey('before'), isFalse);
    });

    test('retains timestamp before for older thread-list servers', () async {
      final adapter = _RecordingJsonAdapter(
        '{"threads":[],"members":[],"has_more":false}',
      );
      final repository = _repository(adapter);

      await repository.threads(
        EntityRef.parse('15@chat.example'),
        before: DateTime.utc(2026, 8, 24),
      );

      expect(adapter.request?.queryParameters['before'],
          '2026-08-24T00:00:00.000Z');
      expect(adapter.request?.queryParameters.containsKey('cursor'), isFalse);
    });

    test('creates a thread from an existing message without a starter body',
        () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_channelJson(
        id: '11',
        type: 11,
      )));
      final repository = _repository(adapter);

      await repository.createThreadFromMessage(
        parent: EntityRef.parse('3@chat.example'),
        message: EntityRef.parse('9@chat.example'),
        name: 'Source discussion',
      );

      expect(
        adapter.request?.path,
        '/api/v1/channels/3@chat.example/messages/9@chat.example/threads',
      );
      expect(adapter.request?.data,
          <String, Object?>{'name': 'Source discussion'});
    });

    test('round-trips thread notification preference when joining', () async {
      final adapter = _RecordingJsonAdapter('{}');
      final repository = _repository(adapter);

      await repository.joinThread(
        EntityRef.parse('11@chat.example'),
        notificationLevel: 'all',
      );

      expect(adapter.request?.method, 'PUT');
      expect(
        adapter.request?.path,
        '/api/v1/channels/11@chat.example/thread-members/@me',
      );
      expect(adapter.request?.data, <String, Object?>{
        'flags': 0,
        'notification_level': 'all',
      });
    });
  });
}

KaedeRepository _repository(_RecordingJsonAdapter adapter) => KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

Map<String, Object?> _channelJson({
  required String id,
  required int type,
  Map<String, Object?> extra = const <String, Object?>{},
}) =>
    <String, Object?>{
      'id': id,
      'origin_domain': 'chat.example',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'type': type,
      'position': 0,
      'permissions': '0',
      'name': 'channel-$id',
      ...extra,
    };

Map<String, Object?> _messageJson(
  String id, {
  required String channelId,
  Map<String, Object?> extra = const <String, Object?>{},
}) =>
    <String, Object?>{
      'id': id,
      'origin_domain': 'chat.example',
      'channel_id': channelId,
      'channel_domain': 'chat.example',
      'author_id': '42',
      'author_domain': 'chat.example',
      'content': 'message-$id',
      'created_at': '2026-08-24T10:00:00Z',
      ...extra,
    };

KaedeChannel _post(
  String id, {
  required String createdAt,
  String? starterCreatedAt,
  bool pinned = false,
  bool archived = false,
}) =>
    KaedeChannel.fromJson(_channelJson(
      id: id,
      type: 11,
      extra: <String, Object?>{
        'parent_id': '15',
        'parent_domain': 'chat.example',
        'created_at': createdAt,
        'flags': pinned ? 2 : 0,
        'archived': archived,
        'starter_message':
            _messageJson(id, channelId: id, extra: <String, Object?>{
          'created_at': starterCreatedAt ?? createdAt,
        }),
      },
    ));

final class _RecordingJsonAdapter implements HttpClientAdapter {
  _RecordingJsonAdapter(this.body);

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
