import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/rich_content.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';

void main() {
  test('Stage lifecycle messages use Discord timeline wording', () {
    expect(
      stageSystemMessageText(27, 'Mina', 'Town Hall'),
      'Mina started a Stage: Town Hall',
    );
    expect(
      stageSystemMessageText(28, 'Mina', 'Town Hall'),
      'Mina ended the Stage: Town Hall',
    );
    expect(stageSystemMessageText(29, 'Mina', null), 'Mina became a speaker.');
    expect(
      stageSystemMessageText(31, 'Mina', 'Questions'),
      'Mina changed the Stage topic: Questions',
    );
  });

  test('reaction moderation uses dedicated group and clear-all routes',
      () async {
    final adapter = _ActionAdapter(<_Reply>[
      const _Reply('{}'),
      const _Reply('{}'),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final channel = EntityRef.parse('2@chat.example');
    final message = EntityRef.parse('10@chat.example');

    await repository.clearReactionGroup(
      channel,
      message,
      '<:party:7@chat.example>',
    );
    await repository.clearReactions(channel, message);

    expect(
      adapter.requests.map((request) => request.method),
      <String>['DELETE', 'DELETE'],
    );
    expect(
      adapter.requests.map((request) => request.path),
      <String>[
        '/api/v1/channels/2@chat.example/messages/10@chat.example/'
            'reactions/%3C%3Aparty%3A7%40chat.example%3E',
        '/api/v1/channels/2@chat.example/messages/10@chat.example/reactions',
      ],
    );
  });

  test('qualified custom reaction tokens survive create and self-removal',
      () async {
    final adapter = _ActionAdapter(<_Reply>[
      const _Reply('{}'),
      const _Reply('{}'),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final channel = EntityRef.parse('2@chat.example');
    final message = EntityRef.parse('10@chat.example');
    const emoji = '<:party_blob:41@home.example>';

    await repository.react(channel, message, emoji);
    await repository.removeReaction(channel, message, emoji);

    expect(
      adapter.requests.map((request) => request.method),
      <String>['POST', 'DELETE'],
    );
    expect(adapter.requests.first.data, <String, Object?>{'emoji': emoji});
    expect(
      adapter.requests.map((request) => request.path),
      <String>[
        '/api/v1/channels/2@chat.example/messages/10@chat.example/reactions',
        '/api/v1/channels/2@chat.example/messages/10@chat.example/reactions/'
            '%3C%3Aparty_blob%3A41%40home.example%3E/@me',
      ],
    );
  });

  test('repository authors polls, live forwards, voters, and poll expiry',
      () async {
    final adapter = _ActionAdapter(<_Reply>[
      _Reply(jsonEncode(_messageJson(poll: _pollJson()))),
      _Reply(jsonEncode(<String, Object?>{
        'forwards': <Object?>[
          <String, Object?>{
            'destination_channel_ref': '2@chat.example',
            'message': _messageJson(
              id: '11',
              forwardedMessageRef: '9@chat.example',
            ),
          },
        ],
        'failures': <Object?>[],
      })),
      _Reply(jsonEncode(<String, Object?>{
        'users': <Object?>[_userJson()],
        'next_after': '7@chat.example',
      })),
      _Reply(jsonEncode(_messageJson(poll: _pollJson(finalized: true)))),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final channel = EntityRef.parse('2@chat.example');
    final source = EntityRef.parse('9@chat.example');
    final draft = RichPollDraft(
      question: 'Ready?',
      answers: <RichPollDraftAnswer>[
        RichPollDraftAnswer(text: 'Yes'),
        RichPollDraftAnswer(text: 'No'),
      ],
      durationHours: 24,
    );

    final created = await repository.createPollMessage(channel, draft);
    final forwarded = await repository.forwardMessage(
      sourceChannel: channel,
      sourceMessage: source,
      destinationChannels: <EntityRef>[channel],
    );
    final voters = await repository.pollVoters(
      channel: channel,
      message: created.ref,
      answerId: 1,
    );
    final finalized = await repository.finalizePoll(
      channel: channel,
      message: created.ref,
    );

    expect(created.poll?.question.text, 'Ready?');
    expect(forwarded.forwards.single.message.forwardedMessageRef, source);
    expect(voters.items.single.handle, 'maple@chat.example');
    expect(voters.nextAfter?.wire, '7@chat.example');
    expect(finalized.poll?.finalized, isTrue);
    expect(
      adapter.requests.map((request) => request.method),
      <String>['POST', 'POST', 'GET', 'POST'],
    );
    expect(
      adapter.requests.map((request) => request.path),
      <String>[
        '/api/v1/channels/2@chat.example/messages',
        '/api/v1/channels/2@chat.example/messages/9@chat.example/forward',
        '/api/v1/channels/2@chat.example/messages/10@chat.example/polls/answers/1',
        '/api/v1/channels/2@chat.example/messages/10@chat.example/polls/expire',
      ],
    );
    expect(
      (adapter.requests[0].data as Map)['poll'],
      draft.toJson(),
    );
    expect(
      (adapter.requests[1].data as Map)['destination_channel_ids'],
      <String>['2@chat.example'],
    );
  });

  test('private interaction polls use isolated response identities', () async {
    final adapter = _ActionAdapter(<_Reply>[
      const _Reply('{}'),
      _Reply(jsonEncode(<String, Object?>{
        'users': <Object?>[_userJson()],
        'next_after': null,
      })),
      const _Reply('{}'),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

    await repository.setInteractionPollVote(
      interactionId: '10',
      responseId: '20',
      answerId: 2,
      selected: true,
    );
    final voters = await repository.interactionPollVoters(
      interactionId: '10',
      responseId: '20',
      answerId: 2,
    );
    await repository.setInteractionPollVote(
      interactionId: '10',
      responseId: '20',
      answerId: 2,
      selected: false,
    );

    expect(voters.items.single.name, 'Maple');
    expect(
      adapter.requests.map((request) => request.method),
      <String>['PUT', 'GET', 'DELETE'],
    );
    expect(
      adapter.requests.map((request) => request.path),
      <String>[
        '/api/v1/interactions/10/responses/20/polls/answers/2/@me',
        '/api/v1/interactions/10/responses/20/polls/answers/2',
        '/api/v1/interactions/10/responses/20/polls/answers/2/@me',
      ],
    );
  });

  test('forward responses bind each message to the requested destination',
      () async {
    final adapter = _ActionAdapter(<_Reply>[
      _Reply(jsonEncode(<String, Object?>{
        'forwards': <Object?>[
          <String, Object?>{
            'destination_channel_ref': '3@chat.example',
            'message': _messageJson(
              id: '11',
              forwardedMessageRef: '9@chat.example',
            ),
          },
        ],
        'failures': <Object?>[],
      })),
    ]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

    await expectLater(
      repository.forwardMessage(
        sourceChannel: EntityRef.parse('2@chat.example'),
        sourceMessage: EntityRef.parse('9@chat.example'),
        destinationChannels: <EntityRef>[
          EntityRef.parse('3@chat.example'),
        ],
      ),
      throwsFormatException,
    );
  });

  test('modal submissions include their exact response correlation', () async {
    final adapter = _ActionAdapter(<_Reply>[const _Reply('{}')]);
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );
    final components = <Map<String, Object?>>[
      <String, Object?>{
        'type': 1,
        'components': <Object?>[
          <String, Object?>{
            'type': 3,
            'custom_id': 'environment',
            'values': <String>['production'],
          },
        ],
      },
    ];

    await repository.submitInteractionModal(
      channel: EntityRef.parse('2@chat.example'),
      application: EntityRef.parse('40@chat.example'),
      responseId: '71',
      customId: 'deploy_details',
      components: components,
    );

    expect(adapter.requests.single.path,
        '/api/v1/channels/2@chat.example/interactions');
    expect(adapter.requests.single.data, <String, Object?>{
      'application_ref': '40@chat.example',
      'interaction_type': 'modal_submit',
      'response_id': '71',
      'custom_id': 'deploy_details',
      'components': components,
    });
  });
}

Map<String, Object?> _messageJson({
  String id = '10',
  String? forwardedMessageRef,
  Map<String, Object?>? poll,
}) =>
    <String, Object?>{
      'id': id,
      'origin_domain': 'chat.example',
      'channel_id': '2',
      'channel_domain': 'chat.example',
      'author_id': '7',
      'author_domain': 'chat.example',
      'author': _userJson(),
      'created_at': '2026-08-27T12:00:00Z',
      if (forwardedMessageRef != null)
        'forwarded_message_ref': forwardedMessageRef,
      if (poll != null) 'poll': poll,
    };

Map<String, Object?> _pollJson({bool finalized = false}) => <String, Object?>{
      'question': <String, Object?>{'text': 'Ready?'},
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
      'results': <String, Object?>{
        'is_finalized': finalized,
        'answer_counts': const <Object?>[],
      },
    };

Map<String, Object?> _userJson() => <String, Object?>{
      'id': '7',
      'origin_domain': 'chat.example',
      'username': 'maple',
      'handle': 'maple@chat.example',
      'display_name': 'Maple',
    };

final class _Reply {
  const _Reply(this.body);

  final String body;
}

final class _ActionAdapter implements HttpClientAdapter {
  _ActionAdapter(this._replies);

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
