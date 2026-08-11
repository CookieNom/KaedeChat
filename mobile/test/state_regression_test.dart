import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/app/message_store.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/auth/turnstile_challenge.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';

void main() {
  group('native security boundaries', () {
    test('bearer credentials are restricted to the home Kaede API', () {
      final instance = Domain('chat.example');
      expect(
        shouldAttachKaedeAuthorization(
          Uri.parse('https://chat.example/api/v1/channels'),
          instance,
        ),
        isTrue,
      );
      expect(
        shouldAttachKaedeAuthorization(
          Uri.parse('https://chat.example/profile'),
          instance,
        ),
        isFalse,
      );
      expect(
        shouldAttachKaedeAuthorization(
          Uri.parse('https://objects.example/api/v1/channels'),
          instance,
        ),
        isFalse,
      );
    });

    test('native challenge cannot navigate its top-level provider page', () {
      final expected = Uri.parse(
        'https://chat.example/api/v1/auth/native-challenge'
        '?action=login&request_id=0123456789abcdef',
      );
      expect(isExpectedNativeChallenge(expected, expected), isTrue);
      expect(
        isExpectedNativeChallenge(
          Uri.parse('https://challenges.cloudflare.com/turnstile'),
          expected,
        ),
        isFalse,
      );
      expect(
        isExpectedNativeChallenge(
          Uri.parse(
            'https://chat.example/api/v1/auth/native-challenge'
            '?action=register&request_id=0123456789abcdef',
          ),
          expected,
        ),
        isFalse,
      );
    });
  });

  group('gateway trust boundary', () {
    test('accepts validated hello and ready envelopes', () {
      final hello = decodeGatewayEnvelope(jsonEncode(<String, Object?>{
        'op': GatewayOp.hello.value,
        'd': <String, Object?>{'heartbeat_interval': 41250},
      }));
      expect(hello.op, GatewayOp.hello.value);
      expect(hello.objectData['heartbeat_interval'], 41250);

      final ready = decodeGatewayEnvelope(jsonEncode(<String, Object?>{
        'op': GatewayOp.dispatch.value,
        's': 17,
        't': 'READY',
        'd': <String, Object?>{'session_id': 'session-1'},
      }));
      expect(ready.eventName, 'READY');
      expect(ready.sequence, 17);
      expect(ready.objectData['session_id'], 'session-1');
    });

    test('rejects malformed and semantically invalid envelopes', () {
      final invalid = <Object?>[
        <int>[1, 2, 3],
        '[]',
        '{not-json}',
        jsonEncode(<String, Object?>{'op': 0.5}),
        jsonEncode(<String, Object?>{'op': 0, 's': -1, 't': 'MESSAGE_CREATE'}),
        jsonEncode(
            <String, Object?>{'op': 0, 't': '', 'd': <String, Object?>{}}),
        jsonEncode(<String, Object?>{
          'op': 0,
          't': 'MESSAGE_CREATE',
          'd': <Object?>[],
        }),
        jsonEncode(<String, Object?>{
          'op': 0,
          't': 'READY',
          'd': <String, Object?>{},
        }),
        jsonEncode(<String, Object?>{
          'op': GatewayOp.hello.value,
          'd': <String, Object?>{'heartbeat_interval': 999},
        }),
        'x' * (maximumGatewayFrameCharacters + 1),
      ];

      for (final frame in invalid) {
        expect(
          () => decodeGatewayEnvelope(frame),
          throwsFormatException,
          reason: '$frame',
        );
      }
    });

    test('classifies duplicate and missing dispatch sequences', () {
      expect(classifyGatewaySequence(null, 0), GatewaySequenceDecision.accept);
      expect(classifyGatewaySequence(8, 9), GatewaySequenceDecision.accept);
      expect(classifyGatewaySequence(8, 8), GatewaySequenceDecision.duplicate);
      expect(classifyGatewaySequence(8, 7), GatewaySequenceDecision.duplicate);
      expect(classifyGatewaySequence(8, 10), GatewaySequenceDecision.gap);
    });
  });

  group('durable outbox', () {
    test('retry delay grows exponentially and is bounded', () {
      expect(outboxRetryDelay(-1), const Duration(seconds: 1));
      expect(outboxRetryDelay(0), const Duration(seconds: 1));
      expect(outboxRetryDelay(1), const Duration(seconds: 2));
      expect(outboxRetryDelay(6), const Duration(seconds: 64));
      expect(outboxRetryDelay(50), const Duration(seconds: 64));
    });
  });

  group('message reconciliation', () {
    test('server echo replaces its optimistic message by stable nonce', () {
      final optimistic = _message(
        id: '100',
        domain: 'chat.example',
        nonce: 'send-1',
        content: 'pending',
      );
      final delivered = _message(
        id: '101',
        domain: 'chat.example',
        nonce: 'send-1',
        content: 'delivered',
      );

      final result = mergeMessages(<KaedeMessage>[optimistic, delivered]);
      expect(result, hasLength(1));
      expect(result.single.ref, delivered.ref);
      expect(result.single.content, 'delivered');
    });

    test('equal snowflakes from different instances remain distinct', () {
      final local = _message(id: '101', domain: 'chat.example');
      final remote = _message(id: '101', domain: 'remote.example');

      final result = mergeMessages(<KaedeMessage>[local, remote]);
      expect(
          result.map((message) => message.ref),
          containsAll(<EntityRef>[
            local.ref,
            remote.ref,
          ]));
      expect(result, hasLength(2));
    });

    test('orders deterministically and returns an immutable snapshot', () {
      final later = _message(
        id: '102',
        domain: 'chat.example',
        createdAt: DateTime.utc(2026, 8, 10, 12, 1),
      );
      final tieB = _message(
        id: '101',
        domain: 'remote.example',
        createdAt: DateTime.utc(2026, 8, 10, 12),
      );
      final tieA = _message(
        id: '101',
        domain: 'chat.example',
        createdAt: DateTime.utc(2026, 8, 10, 12),
      );

      final result = mergeMessages(<KaedeMessage>[later, tieB, tieA]);
      expect(
        result.map((message) => message.ref.wire),
        <String>[
          '101@chat.example',
          '101@remote.example',
          '102@chat.example',
        ],
      );
      expect(() => result.add(later), throwsUnsupportedError);
    });
  });
}

KaedeMessage _message({
  required String id,
  required String domain,
  String? nonce,
  String? content,
  DateTime? createdAt,
}) =>
    KaedeMessage(
      ref: EntityRef.parse('$id@$domain'),
      channelRef: EntityRef.parse('200@chat.example'),
      authorRef: EntityRef.parse('300@chat.example'),
      createdAt: createdAt ?? DateTime.utc(2026, 8, 10, 12),
      clientNonce: nonce,
      content: content,
    );
