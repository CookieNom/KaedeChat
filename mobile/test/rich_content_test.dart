import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/rich_content.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

Map<String, Object?> interactionResponseEvent(
  String operation, {
  String authority = 'c1.example',
  String interactionId = '70',
  String responseId = '71',
  String revision = '1',
  int sequence = 0,
  int callbackType = 4,
  bool ephemeral = false,
  Map<String, Object?> data = const <String, Object?>{},
  String? deletedAt,
}) =>
    <String, Object?>{
      'authority_domain': authority,
      'interaction_id': interactionId,
      'interaction_ref': '$interactionId@$authority',
      'response_id': responseId,
      'response_ref': '$responseId@$authority',
      'user_ref': '1@users.example',
      'invoker_ref': '1@users.example',
      'channel_ref': '2@$authority',
      'application_ref': '3@apps.example',
      'response_grant_id': List.filled(43, 'A').join(),
      'revision': revision,
      'operation': operation,
      'expires_at': '2099-01-01T00:00:00Z',
      'sequence': sequence,
      'callback_type': callbackType,
      'ephemeral': ephemeral,
      'data': data,
      'message_ref': null,
      'autocomplete_generation': callbackType == 8 ? '1' : null,
      'deleted_at': deletedAt,
    };

Map<String, Object?> richMessageJson() => <String, Object?>{
      'id': '10',
      'origin_domain': 'chat.example',
      'channel_id': '20',
      'channel_domain': 'chat.example',
      'author_id': '30',
      'author_domain': 'chat.example',
      'content': null,
      'mention_role_refs': <Object?>[
        <String, Object?>{'id': '41', 'origin_domain': 'chat.example'},
      ],
      'mention_everyone': true,
      'created_at': '2026-08-27T00:00:00Z',
      'application_id': '40',
      'application_domain': 'chat.example',
      'view_version': 2,
      'forwarded_message_ref': '50@chat.example',
      'embeds': <Object?>[
        <String, Object?>{
          'title': 'Release',
          'description': 'Ready to ship',
          'color': 0xEE765E,
          'fields': <Object?>[
            <String, Object?>{
              'name': 'Status',
              'value': 'Green',
              'inline': true,
            },
          ],
        },
      ],
      'components': <Object?>[
        <String, Object?>{
          'type': 1,
          'components': <Object?>[
            <String, Object?>{
              'type': 2,
              'style': 1,
              'label': 'Deploy',
              'custom_id': 'deploy',
            },
          ],
        },
      ],
      'poll': <String, Object?>{
        'question': <String, Object?>{'text': 'Ship now?'},
        'answers': <Object?>[
          <String, Object?>{
            'answer_id': 1,
            'poll_media': <String, Object?>{'text': 'Yes'},
          },
          <String, Object?>{
            'answer_id': 2,
            'poll_media': <String, Object?>{'text': 'No'},
          },
        ],
        'expiry': '2099-01-01T00:00:00Z',
        'allow_multiselect': false,
        'layout_type': 1,
        'results': <String, Object?>{
          'is_finalized': false,
          'answer_counts': <Object?>[
            <String, Object?>{
              'id': 1,
              'count': 3,
              'me_voted': true,
            },
            <String, Object?>{
              'id': 2,
              'count': 1,
              'me_voted': false,
            },
          ],
        },
      },
    };

void main() {
  test('network attachments cannot inject private plaintext commitments', () {
    final raw = <String, Object?>{
      'id': '1',
      'origin_domain': 'chat.example',
      'filename': 'report.txt',
      'content_type': 'text/plain',
      'size': 4,
      'scan_status': 'clean',
      'plaintext_sha256': List.filled(43, 'A').join(),
    };

    expect(KaedeAttachment.fromJson(raw).plaintextSha256, isNull);
    expect(
      KaedeAttachment.fromJson(raw, trustClientState: true).plaintextSha256,
      List.filled(43, 'A').join(),
    );
  });

  test('forward snapshots reject malformed nested children as a unit', () {
    expect(
      () => KaedeMessageSnapshot.fromJson(<String, Object?>{
        'content': 'forwarded',
        'embeds': const <Object?>[],
        'components': const <Object?>[],
        'attachments': const <Object?>[],
        'sticker_items': const <Object?>[],
        'mention_user_refs': const <Object?>[],
        'message_snapshots': <Object?>[const <String, Object?>{}],
        'message_type': 0,
        'flags': 0,
        'created_at': '2026-08-28T00:00:00Z',
        'edited_at': null,
      }),
      throwsA(isA<FormatException>()),
    );
  });

  test('rich messages decode and survive the mobile cache round trip', () {
    final message = KaedeMessage.fromJson(richMessageJson());

    expect(message.embeds.single.title, 'Release');
    expect(message.components.single.components.single.customId, 'deploy');
    expect(message.applicationRef?.wire, '40@chat.example');
    expect(message.forwardedMessageRef?.wire, '50@chat.example');
    expect(message.poll?.totalVotes, 4);
    expect(message.poll?.percentFor(1), 75);
    expect(message.mentionRoleRefs.single.wire, '41@chat.example');
    expect(message.mentionEveryone, isTrue);

    final added = message.poll!.withVote(
      answerId: 2,
      added: true,
      isCurrentUser: true,
    );
    expect(added.resultFor(2).count, 2);
    expect(added.resultFor(2).meVoted, isTrue);
    final removed = added.withVote(
      answerId: 2,
      added: false,
      isCurrentUser: true,
    );
    expect(removed.resultFor(2).count, 1);
    expect(removed.resultFor(2).meVoted, isFalse);

    final restored = KaedeMessage.fromJson(message.toJson());
    expect(restored.embeds.single.fields.single.value, 'Green');
    expect(restored.components.single.components.single.label, 'Deploy');
    expect(restored.poll?.resultFor(1).meVoted, isTrue);
    expect(restored.mentionRoleRefs.single.wire, '41@chat.example');
    expect(restored.mentionEveryone, isTrue);
  });

  test(
      'message rich arrays reject hostile scalar children instead of partial display',
      () {
    for (final field in <String>[
      'embeds',
      'components',
      'attachments',
      'sticker_items'
    ]) {
      final payload = richMessageJson();
      payload[field] = <Object?>[
        ...((payload[field] as List?) ?? const <Object?>[]),
        'silently dropped before this regression',
      ];
      expect(
        () => KaedeMessage.fromJson(payload),
        throwsA(isA<FormatException>()),
        reason: field,
      );
    }
  });

  test('poll drafts enforce Discord wire limits and preserve emoji identity',
      () {
    final draft = RichPollDraft(
      question: ' Ship now? ',
      answers: <RichPollDraftAnswer>[
        RichPollDraftAnswer(
          text: 'Yes',
          emoji: richPollEmojiFromComposerValue('✅'),
        ),
        RichPollDraftAnswer(
          text: 'Wait',
          emoji: richPollEmojiFromComposerValue(
            '<a:loading:99@chat.example>',
          ),
        ),
      ],
      durationHours: 24,
      allowMultiselect: true,
    );

    expect(draft.question, 'Ship now?');
    expect(draft.toJson(), <String, Object?>{
      'question': <String, Object?>{'text': 'Ship now?'},
      'answers': <Object?>[
        <String, Object?>{
          'poll_media': <String, Object?>{
            'text': 'Yes',
            'emoji': <String, Object?>{
              'id': null,
              'name': '✅',
              'animated': false,
            },
          },
        },
        <String, Object?>{
          'poll_media': <String, Object?>{
            'text': 'Wait',
            'emoji': <String, Object?>{
              'id': '99@chat.example',
              'name': 'loading',
              'animated': true,
            },
          },
        },
      ],
      'duration': 24,
      'allow_multiselect': true,
      'layout_type': 1,
    });
    expect(
      () => RichPollDraft(
        question: 'One choice?',
        answers: <RichPollDraftAnswer>[
          RichPollDraftAnswer(text: 'Only'),
        ],
        durationHours: 24,
      ),
      throwsArgumentError,
    );
  });

  test(
      'shared type-46 poll results are strict and E2EE labels need a verified poll',
      () async {
    final fixture = jsonDecode(await File(
      '../frontend/static/protocol/poll-result-v1.json',
    ).readAsString()) as Map<String, Object?>;
    final vectors = fixture['vectors']! as List<Object?>;
    for (final rawVector in vectors) {
      final vector = Map<String, Object?>.from(rawVector! as Map);
      final rawMessage = Map<String, Object?>.from(vector['message']! as Map);
      final rawId = '${rawMessage['id']}'.split('@').first;
      final payload = <String, Object?>{
        ...rawMessage,
        'id': rawId,
        'author_id': '1',
        'author_domain': 'polls.example',
        'content': null,
        'attachments': rawMessage['attachments'] ?? <Object?>[],
        'components': rawMessage['components'] ?? <Object?>[],
        'sticker_items': rawMessage['sticker_items'] ?? <Object?>[],
        'flags': rawMessage['flags'] ?? 0,
        'tts': false,
        'created_at': '2026-08-28T00:00:00Z',
      };
      final message = KaedeMessage.fromJson(payload);
      expect(message.pollResult?.pollMessageRef, message.reference);
      expect(KaedeMessage.fromJson(message.toJson()).pollResult, isNotNull);

      if (vector['name'] == 'e2ee_unique_winner_federated_ref') {
        final source = KaedeMessage(
          ref: EntityRef.parse('456@author.example'),
          channelRef: EntityRef.parse('77@home.example'),
          authorRef: EntityRef.parse('1@author.example'),
          createdAt: DateTime.utc(2026, 8, 28),
          e2ee: const <String, Object?>{'version': 2},
          e2eeVerified: true,
          poll: RichPoll.fromJson(
            Map<String, Object?>.from(vector['verified_source_poll']! as Map)
              ..addAll(<String, Object?>{
                'expiry': '2099-01-01T00:00:00Z',
                'allow_multiselect': false,
                'results': <String, Object?>{
                  'is_finalized': true,
                  'answer_counts': <Object?>[
                    <String, Object?>{
                      'id': 1,
                      'count': 2,
                      'me_voted': false,
                    },
                    <String, Object?>{
                      'id': 2,
                      'count': 5,
                      'me_voted': false,
                    },
                  ],
                },
              }),
          ),
        );
        final resolved = resolvedMessagePollResult(message, source);
        expect(resolved?.questionText, 'Secret launch choice');
        expect(resolved?.victorAnswerText, 'Launch');
        expect(
          resolvedMessagePollResult(
            message,
            KaedeMessage(
              ref: source.ref,
              channelRef: source.channelRef,
              authorRef: source.authorRef,
              createdAt: source.createdAt,
            ),
          ),
          isNull,
        );
      }
    }

    final vector = Map<String, Object?>.from(
      Map<String, Object?>.from(vectors[1]! as Map)['message']! as Map,
    );
    vector
      ..['id'] = '900'
      ..['author_id'] = '1'
      ..['author_domain'] = 'polls.example'
      ..['content'] = null
      ..['attachments'] = <Object?>[]
      ..['components'] = <Object?>[]
      ..['sticker_items'] = <Object?>[]
      ..['flags'] = 0
      ..['tts'] = false
      ..['created_at'] = '2026-08-28T00:00:00Z';
    final leaking = jsonDecode(jsonEncode(vector)) as Map<String, Object?>;
    final embed = Map<String, Object?>.from(
      (leaking['embeds']! as List<Object?>).single! as Map,
    );
    embed['fields'] = <Object?>[
      ...(embed['fields']! as List<Object?>),
      <String, Object?>{
        'name': 'poll_question_text',
        'value': 'secret',
        'inline': false,
      },
    ];
    leaking['embeds'] = <Object?>[embed];
    expect(
      () => KaedeMessage.fromJson(leaking),
      throwsA(isA<FormatException>()),
    );
    expect(
      () => KaedeMessage.fromJson(<String, Object?>{
        ...vector,
        'referenced_message_id': '455',
      }),
      throwsA(isA<FormatException>()),
    );
  });

  test('forward destinations span accessible encrypted conversations', () {
    final guild = EntityRef.parse('1@chat.example');
    KaedeChannel channel(
      String id, {
      EntityRef? guildRef,
      ChannelType type = ChannelType.text,
      String encryptionMode = 'plaintext',
      int permissions = Permission.sendMessages,
      bool nsfw = false,
    }) =>
        KaedeChannel(
          ref: EntityRef.parse('$id@chat.example'),
          guildRef: guildRef,
          type: type,
          name: id,
          position: 0,
          permissions: BigInt.from(permissions),
          encryptionMode: encryptionMode,
          nsfw: nsfw,
        );

    final source = channel('10', guildRef: guild);
    expect(canForwardMessageToChannel(source, channel('11', guildRef: guild)),
        isTrue);
    expect(
      canForwardMessageToChannel(
        source,
        channel('12', guildRef: guild, encryptionMode: 'e2ee'),
      ),
      isTrue,
    );
    expect(
      canForwardMessageToChannel(
        channel('14', guildRef: guild, encryptionMode: 'e2ee'),
        source,
      ),
      isTrue,
    );
    expect(
      canForwardMessageToChannel(
        source,
        channel('13', guildRef: EntityRef.parse('2@chat.example')),
      ),
      isTrue,
    );
    final dm = channel('20', guildRef: null, type: ChannelType.dm);
    final groupDm = channel('22', guildRef: null, type: ChannelType.groupDm);
    expect(canForwardMessageToChannel(dm, dm), isTrue);
    expect(canForwardMessageToChannel(dm, groupDm), isTrue);
    expect(
      canForwardMessageToChannel(
        dm,
        channel('21', guildRef: null, type: ChannelType.dm),
      ),
      isTrue,
    );
    expect(
      canForwardMessageToChannel(
        source,
        channel('23', guildRef: guild, type: ChannelType.voice),
      ),
      isTrue,
    );
    expect(
      canForwardMessageToChannel(
        source,
        channel('24', guildRef: guild, type: ChannelType.stage),
      ),
      isTrue,
    );
    expect(
      canForwardMessageToChannel(
        channel('30', guildRef: guild, nsfw: true),
        channel('31', guildRef: guild),
      ),
      isFalse,
    );
    expect(
      canForwardMessageToChannel(
        channel('30', guildRef: guild, nsfw: true),
        channel('32', guildRef: guild, nsfw: true),
      ),
      isTrue,
    );
  });

  test('forwardability excludes Discord polls, calls, and system notices', () {
    KaedeMessage message({int type = 0, RichPoll? poll}) => KaedeMessage(
          ref: EntityRef.parse('1@chat.example'),
          channelRef: EntityRef.parse('2@chat.example'),
          authorRef: EntityRef.parse('3@chat.example'),
          createdAt: DateTime.utc(2026, 8, 28),
          messageType: type,
          poll: poll,
        );
    expect(forwardMessageUnavailableReason(message()), isNull);
    expect(forwardMessageUnavailableReason(message(type: 19)), isNull);
    expect(forwardMessageUnavailableReason(message(type: 20)), isNull);
    expect(forwardMessageUnavailableReason(message(type: 23)), isNull);
    expect(
      forwardMessageUnavailableReason(
        message(
          poll: RichPoll.fromJson(<String, Object?>{
            'question': <String, Object?>{'text': 'Choose'},
            'answers': const <Object?>[],
            'results': const <String, Object?>{},
          }),
        ),
      ),
      contains('Poll'),
    );
    expect(forwardMessageUnavailableReason(message(type: 3)), contains('Call'));
    expect(
        forwardMessageUnavailableReason(message(type: 12)), contains('System'));
    expect(
      forwardMessageUnavailableReason(KaedeMessage(
        ref: EntityRef.parse('4@chat.example'),
        channelRef: EntityRef.parse('2@chat.example'),
        authorRef: EntityRef.parse('3@chat.example'),
        createdAt: DateTime.utc(2026, 8, 28),
        e2ee: const <String, Object?>{
          'forward_projection_version': 2,
          'forward_projection_digest':
              'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        },
      )),
      contains('not verified'),
    );
  });

  test('channel follow notices retain and render qualified source references',
      () {
    final notice = KaedeMessage.fromJson(<String, Object?>{
      'id': '1',
      'origin_domain': 'target.example',
      'channel_id': '2',
      'channel_domain': 'target.example',
      'author_id': '3',
      'author_domain': 'users.example',
      'author': <String, Object?>{
        'id': '3',
        'origin_domain': 'users.example',
        'username': 'maple',
        'handle': 'maple@users.example',
      },
      'content': 'upstream-news',
      'message_type': 12,
      'message_reference': <String, Object?>{
        'type': 0,
        'channel_id': '8',
        'channel_domain': 'source.example',
        'guild_id': '9',
        'guild_domain': 'source.example',
      },
      'created_at': '2026-08-29T12:00:00Z',
    });
    final known = KaedeChannel(
      ref: EntityRef.parse('8@source.example'),
      guildRef: EntityRef.parse('9@source.example'),
      type: ChannelType.announcement,
      name: 'release-notes',
      position: 0,
      permissions: BigInt.zero,
    );

    expect(notice.followedChannelRef?.wire, '8@source.example');
    expect(notice.followedGuildRef?.wire, '9@source.example');
    expect(
      channelFollowSystemMessageText(notice, <KaedeChannel>[known]),
      'maple has added #release-notes to this channel. '
      'Its most important updates will show up here.',
    );
    expect(
      channelFollowSystemMessageText(
          notice.copyWith(clearContent: true), const []),
      contains('#8@source.example'),
    );
    final restored = KaedeMessage.fromJson(notice.toJson());
    expect(restored.followedChannelRef, notice.followedChannelRef);
    expect(restored.followedGuildRef, notice.followedGuildRef);
    expect(
      () => KaedeMessage.fromJson(<String, Object?>{
        ...notice.toJson(),
        'message_reference': <String, Object?>{
          'channel_id': '8',
          'channel_domain': 'source.example',
        },
      }),
      throwsA(isA<FormatException>()),
    );
  });

  test('interaction response events expose modal and ephemeral contracts', () {
    final response =
        MobileInteractionResponse.fromJson(interactionResponseEvent(
      'CREATE',
      callbackType: 9,
      ephemeral: true,
      data: <String, Object?>{
        'title': 'Deploy details',
        'custom_id': 'deploy_details',
        'components': <Object?>[
          <String, Object?>{
            'type': 1,
            'components': <Object?>[
              <String, Object?>{
                'type': 4,
                'custom_id': 'reason',
                'label': 'Reason',
                'style': 2,
              },
            ],
          },
          <String, Object?>{
            'type': 1,
            'components': <Object?>[
              <String, Object?>{
                'type': 3,
                'custom_id': 'environment',
                'placeholder': 'Environment',
                'min_values': 1,
                'max_values': 2,
                'options': <Object?>[
                  <String, Object?>{
                    'label': 'Production',
                    'value': 'production',
                    'default': true,
                  },
                ],
              },
            ],
          },
        ],
      },
    ));

    expect(response.modal?.title, 'Deploy details');
    expect(response.modal?.rows.first.components.single.isTextInput, isTrue);
    final select = response.modal?.rows[1].components.single;
    expect(select?.isStringSelect, isTrue);
    expect(select?.minValues, 1);
    expect(select?.maxValues, 2);
    expect(select?.options.single.isDefault, isTrue);
    expect(response.autocompleteGeneration, isNull);
    expect(response.storageKey, '71@c1.example');
  });

  test('interaction response parsing rejects every malformed wire projection',
      () {
    final valid = interactionResponseEvent('CREATE');
    final malformed = <Map<String, Object?>>[
      <String, Object?>{...valid, 'unexpected': true},
      <String, Object?>{...valid, 'response_grant_id': 'not-a-grant'},
      <String, Object?>{
        ...valid,
        'callback_type': 8,
        'autocomplete_generation': null,
      },
      <String, Object?>{...valid, 'message_ref': '9@other.example'},
      <String, Object?>{
        ...valid,
        'sequence': BigInt.parse('9223372036854775808'),
      },
      <String, Object?>{...valid, 'operation': 'UPDATE', 'revision': '1'},
      <String, Object?>{...valid, 'data': <Object?>[]},
      <String, Object?>{...valid, 'decryption_unavailable': true},
    ];
    for (final event in malformed) {
      expect(
        () => MobileInteractionResponse.fromJson(event),
        throwsFormatException,
      );
    }
    final unavailable = MobileInteractionResponse.fromJson(
      <String, Object?>{...valid, 'decryption_unavailable': true},
      allowClientState: true,
    );
    expect(unavailable.decryptionUnavailable, isTrue);
  });

  test('private interaction media and poll drafts are normalized safely', () {
    final data = <String, Object?>{
      'attachments': <Object?>[
        <String, Object?>{
          'id': '90',
          'origin_domain': 'Chat.Example',
          'filename': '../release.png',
          'content_type': 'image/png',
          'size': 4096,
          'width': 800,
          'height': 600,
          'scan_status': 'clean',
          'private_media_url':
              '/api/v1/interactions/70@chat.example/responses/71@chat.example/attachments/90@chat.example',
        },
        <String, Object?>{
          'id': '../bad',
          'origin_domain': 'attacker.invalid',
          'filename': 'ignored.txt',
          'content_type': 'text/plain',
          'size': 1,
          'scan_status': 'clean',
        },
      ],
      'poll': <String, Object?>{
        'question': <String, Object?>{'text': 'Ship it?'},
        'answers': <Object?>[
          <String, Object?>{
            'poll_media': <String, Object?>{'text': 'Yes'},
          },
          <String, Object?>{
            'poll_media': <String, Object?>{'text': 'Wait'},
          },
        ],
        'duration': 24,
        'allow_multiselect': false,
        'layout_type': 1,
      },
    };

    final attachments = interactionResponseAttachments(data);
    expect(attachments, hasLength(1));
    expect(attachments.single.ref.wire, '90@chat.example');
    expect(attachments.single.filename, 'release.png');
    expect(attachments.single.scanStatus, 'clean');
    expect(
      attachmentMediaPath(
        attachments.single.ref,
        privateMediaUrl: attachments.single.privateMediaUrl,
      ),
      '/api/v1/interactions/70@chat.example/responses/71@chat.example/attachments/90@chat.example/original',
    );
    expect(
      privateInteractionAttachmentMediaPath(
        attachments.single.ref,
        '/api/v1/interactions/70@chat.example/responses/71@chat.example/attachments/91@chat.example',
      ),
      isNull,
    );
    expect(
      privateInteractionAttachmentMediaPath(
        attachments.single.ref,
        '/api/v1/interactions/70@CHAT.EXAMPLE/responses/71@chat.example/attachments/90@chat.example',
      ),
      isNull,
    );

    final poll = interactionResponsePoll(data);
    expect(poll?.question.text, 'Ship it?');
    expect(poll?.answers.map((answer) => answer.id), <int>[1, 2]);
    expect(poll?.totalVotes, 0);
  });

  test('private response updates preserve follow-ups and delete one response',
      () {
    var responses = const <String, MobileInteractionResponse>{};
    responses = applyMobileInteractionResponseEvent(
      responses,
      'INTERACTION_RESPONSE_CREATE',
      interactionResponseEvent('CREATE',
          callbackType: 4,
          ephemeral: true,
          data: <String, Object?>{'flags': 64}),
    );
    responses = applyMobileInteractionResponseEvent(
      responses,
      'INTERACTION_RESPONSE_UPDATE',
      interactionResponseEvent('UPDATE',
          revision: '2',
          callbackType: 4,
          ephemeral: true,
          data: <String, Object?>{'content': 'Finished', 'flags': 64}),
    );
    responses = applyMobileInteractionResponseEvent(
      responses,
      'INTERACTION_RESPONSE_CREATE',
      interactionResponseEvent('CREATE',
          responseId: '72',
          sequence: 1,
          ephemeral: true,
          data: <String, Object?>{'content': 'Follow-up', 'flags': 64}),
    );

    expect(responses.keys,
        containsAll(<String>['71@c1.example', '72@c1.example']));
    expect(responses['71@c1.example']?.data['content'], 'Finished');

    responses = applyMobileInteractionResponseEvent(
      responses,
      'INTERACTION_RESPONSE_DELETE',
      interactionResponseEvent('DELETE',
          responseId: '72', revision: '2', deletedAt: '2026-08-28T00:00:00Z'),
    );
    expect(responses['72@c1.example']?.deletedAt, isNotNull);

    responses = applyMobileInteractionResponseEvent(
      responses,
      'INTERACTION_RESPONSE_UPDATE',
      interactionResponseEvent('UPDATE',
          responseId: '72',
          revision: '3',
          data: <String, Object?>{'content': 'revived'}),
    );
    expect(responses['72@c1.example']?.deletedAt, isNotNull);
  });

  test('private response identities remain isolated across authorities', () {
    var responses = const <String, MobileInteractionResponse>{};
    responses = applyMobileInteractionResponseEvent(
      responses,
      'INTERACTION_RESPONSE_CREATE',
      interactionResponseEvent('CREATE',
          data: <String, Object?>{'content': 'one'}),
    );
    responses = applyMobileInteractionResponseEvent(
      responses,
      'INTERACTION_RESPONSE_CREATE',
      interactionResponseEvent('CREATE',
          authority: 'c2.example', data: <String, Object?>{'content': 'two'}),
    );

    expect(responses.keys,
        containsAll(<String>['71@c1.example', '71@c2.example']));
  });

  test('embed media accepts only proxyable URLs and same-origin capabilities',
      () {
    expect(
      richEmbedExternalMediaUri('https://cdn.example/icon.png')?.host,
      'cdn.example',
    );
    expect(richEmbedExternalMediaUri('attachment://icon.png'), isNull);
    expect(richEmbedExternalMediaUri('/media/icon.png'), isNull);
    expect(richEmbedExternalMediaUri('javascript:alert(1)'), isNull);
    expect(
      richEmbedExternalMediaUri('https://user:secret@cdn.example/icon.png'),
      isNull,
    );

    final safe = linkPreviewMediaUri(
      Domain('chat.example'),
      '/api/v1/link-previews/media/'
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    );
    expect(safe?.origin, 'https://chat.example');
    expect(
      linkPreviewMediaUri(
        Domain('chat.example'),
        '/api/v1/link-previews/media/'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?redirect=evil',
      ),
      isNull,
    );
    expect(linkPreviewMediaUri(Domain('chat.example'), '//evil.invalid/a'),
        isNull);
  });

  test('interaction authority survives unrelated Mobile navigation state', () {
    final channel = EntityRef.parse('20@chat.example');
    final application = EntityRef.parse('40@chat.example');
    final initial = MobileState(
      phase: SessionPhase.ready,
      selectedChannel: channel,
      interactionRequests: <String, MobileInteractionRequest>{
        '70': (
          channel: channel,
          application: application,
          integrationType: 'guild_install',
          interactionContext: 'guild',
          encryptionChannel: null,
        ),
      },
    );

    final navigated = initial.copyWith(
      selectedChannel: EntityRef.parse('21@chat.example'),
    );
    expect(navigated.interactionRequests['70']?.channel, channel);
    expect(navigated.interactionRequests['70']?.application, application);
  });

  test('mobile command schema retains Discord-style typed constraints', () {
    final command = MobileApplicationCommand.fromJson(<String, Object?>{
      'id': '90',
      'application_ref': '40@chat.example',
      'application_name': 'Deploy bot',
      'integration_type': 'guild_install',
      'interaction_context': 'guild',
      'name': 'deploy',
      'name_localizations': <String, Object?>{
        'fr': 'déployer',
        'en-GB': 'release',
      },
      'type': 'chat_input',
      'description': 'Deploy a build',
      'options': <Object?>[
        <String, Object?>{
          'type': 'integer',
          'name': 'replicas',
          'name_localizations': <String, Object?>{'fr': 'répliques'},
          'description': 'Replica count',
          'required': true,
          'min_value': 1,
          'max_value': 20,
          'choices': <Object?>[
            <String, Object?>{
              'name': 'Small',
              'name_localizations': <String, Object?>{'fr': 'Petit'},
              'value': 2,
            },
          ],
        },
        <String, Object?>{
          'type': 'channel',
          'name': 'updates',
          'description': 'Updates channel',
          'channel_types': <Object?>[0, 5],
        },
      ],
    });

    expect(command.options.first.required, isTrue);
    expect(command.options.first.minValue, 1);
    expect(command.options.first.maxValue, 20);
    expect(command.options.first.choices.single.value, 2);
    expect(command.displayName('fr'), 'déployer');
    expect(command.displayName('en-US'), 'release');
    expect(command.options.first.displayName('fr'), 'répliques');
    expect(command.options.first.choices.single.displayName('fr'), 'Petit');
    expect(
      mobileLocalizedCommandText(
        'Weather',
        const <String, String>{'es-ES': 'Tiempo'},
        'es-419',
      ),
      'Tiempo',
    );
    expect(command.options.last.channelTypes, <int>[0, 5]);
    expect(
      mobileCommandOptionAllowsChannelType(command.options.last, 5),
      isTrue,
    );
    expect(
      mobileCommandOptionAllowsChannelType(command.options.last, 2),
      isFalse,
    );
  });

  test('typed command composer resolves nested paths and wire types', () {
    final command = MobileApplicationCommand(
      id: 'deploy',
      application: EntityRef(Snowflake('40'), Domain('chat.example')),
      applicationName: 'Deploy bot',
      integrationType: 'guild_install',
      interactionContext: 'guild',
      name: 'deploy',
      type: 'chat_input',
      description: 'Deploy a build',
      options: <MobileApplicationCommandOption>[
        MobileApplicationCommandOption(
          name: 'environment',
          type: 'subcommand_group',
          description: 'Choose an environment',
          options: <MobileApplicationCommandOption>[
            MobileApplicationCommandOption(
              name: 'release',
              type: 'subcommand',
              description: 'Release a build',
              options: <MobileApplicationCommandOption>[
                MobileApplicationCommandOption(
                  name: 'name',
                  type: 'string',
                  description: 'Build name',
                  required: true,
                  minLength: 3,
                  maxLength: 20,
                ),
                MobileApplicationCommandOption(
                  name: 'replicas',
                  type: 'integer',
                  description: 'Replica count',
                  minValue: 1,
                  maxValue: 20,
                ),
                MobileApplicationCommandOption(
                  name: 'dry_run',
                  type: 'boolean',
                  description: 'Only validate',
                ),
                MobileApplicationCommandOption(
                  name: 'artifact',
                  type: 'attachment',
                  description: 'Build artifact',
                  required: true,
                ),
              ],
            ),
          ],
        ),
      ],
    );
    final values = <String, Object?>{
      mobileCommandContainerKey(const <String>[]): 'environment',
      mobileCommandContainerKey(const <String>['environment']): 'release',
      'environment.release.name': ' kaede ',
      'environment.release.replicas': '3',
      'environment.release.dry_run': false,
      'environment.release.artifact': 'pending-file-1',
    };

    final model = mobileCommandComposerModel(command.options, values);
    expect(model.selectors.map((item) => item.selected),
        <String>['environment', 'release']);
    expect(model.fields.map((item) => item.path), <String>[
      'environment.release.name',
      'environment.release.replicas',
      'environment.release.dry_run',
      'environment.release.artifact',
    ]);
    expect(mobileCommandOptionErrors(command, values), isEmpty);
    expect(mobileCommandAttachmentKeys(command, values),
        <String>{'pending-file-1'});
    expect(mobileCommandOptionPayload(command, values), <String, Object?>{
      'environment': <String, Object?>{
        'release': <String, Object?>{
          'name': 'kaede',
          'replicas': 3,
          'dry_run': false,
          'artifact': 'pending-file-1',
        },
      },
    });
  });

  test('typed command validation reports bounds and duplicate files', () {
    final command = MobileApplicationCommand(
      id: 'compare',
      application: EntityRef(Snowflake('40'), Domain('chat.example')),
      applicationName: 'Files bot',
      integrationType: 'guild_install',
      interactionContext: 'guild',
      name: 'compare',
      type: 'chat_input',
      description: 'Compare files',
      options: <MobileApplicationCommandOption>[
        MobileApplicationCommandOption(
          name: 'query',
          type: 'string',
          description: 'Search query',
          required: true,
          minLength: 3,
        ),
        MobileApplicationCommandOption(
          name: 'score',
          type: 'number',
          description: 'Minimum score',
          minValue: 0,
          maxValue: 1,
        ),
        MobileApplicationCommandOption(
          name: 'left',
          type: 'attachment',
          description: 'Left file',
          required: true,
        ),
        MobileApplicationCommandOption(
          name: 'right',
          type: 'attachment',
          description: 'Right file',
          required: true,
        ),
      ],
    );
    final errors = mobileCommandOptionErrors(command, <String, Object?>{
      'query': 'x',
      'score': '1.5',
      'left': 'same-file',
      'right': 'same-file',
    });

    expect(errors['query'], contains('at least 3'));
    expect(errors['score'], contains('1 or less'));
    expect(errors['right'], contains('different file'));
    expect(mobileCommandOptionsComplete(command, const {}), isFalse);
  });
}
