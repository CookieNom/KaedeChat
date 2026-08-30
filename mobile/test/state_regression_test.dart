import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/app/message_store.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/auth/turnstile_challenge.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:web_socket_channel/io.dart';

void main() {
  group('private self moderation status', () {
    test('parses composite guild identity and expires finite timeouts', () {
      final status = GuildSelfModerationStatus.fromJson(<String, Object?>{
        'guild_id': '10',
        'guild_domain': 'guild.example',
        'timed_out': true,
        'timeout_until': '2030-01-02T03:04:00Z',
        'timeout_indefinite': false,
        'reason': 'Repeated spam',
      });
      expect(status.guildRef.wire, '10@guild.example');
      expect(status.reason, 'Repeated spam');
      expect(status.detailsAvailable, isTrue);
      expect(status.activeAt(DateTime.utc(2029)), isTrue);
      expect(status.activeAt(DateTime.utc(2031)), isFalse);
    });

    test('marks rolling-upgrade timing fallback as non-authoritative', () {
      final status = GuildSelfModerationStatus.fromJson(<String, Object?>{
        'guild_id': '10',
        'guild_domain': 'guild.example',
        'timed_out': true,
        'timeout_until': '2030-01-02T03:04:00Z',
        'timeout_indefinite': false,
        'reason': null,
        'details_available': false,
      });
      expect(status.detailsAvailable, isFalse);
      expect(
        shouldRetrySelfModerationStatus(
          status,
          appActive: true,
          conversationPaneVisible: true,
          selectedGuild: status.guildRef,
        ),
        isTrue,
      );
      expect(
        shouldRetrySelfModerationStatus(
          status,
          appActive: false,
          conversationPaneVisible: true,
          selectedGuild: status.guildRef,
        ),
        isFalse,
      );
      expect(
        shouldRetrySelfModerationStatus(
          status,
          appActive: true,
          conversationPaneVisible: false,
          selectedGuild: status.guildRef,
        ),
        isFalse,
      );
    });

    test('requires the indefinite projection to omit a finite expiry', () {
      final status = GuildSelfModerationStatus.fromJson(<String, Object?>{
        'guild_id': '10',
        'guild_domain': 'guild.example',
        'timed_out': true,
        'timeout_until': null,
        'timeout_indefinite': true,
        'reason': null,
      });
      expect(status.activeAt(DateTime.utc(2031)), isTrue);
    });
  });

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
    test('self deafen publishes the exact implied-mute payload', () {
      expect(
        selfVoiceStatePayload(selfMute: false, selfDeaf: true),
        <String, Object?>{'self_mute': true, 'self_deaf': true},
      );
      expect(
        selfVoiceStatePayload(selfMute: false, selfDeaf: false).keys,
        <String>['self_mute', 'self_deaf'],
      );
    });

    test('voice info and soundboard request payloads match gateway contracts',
        () {
      expect(
        channelInfoRequestPayload(
          '10@remote.test',
          <String>['status', 'voice_start_time'],
        ),
        <String, Object?>{
          'guild_id': '10@remote.test',
          'fields': <String>['status', 'voice_start_time'],
        },
      );
      expect(
        soundboardSoundsRequestPayload(
          <String>['10@remote.test', '11@remote.test'],
        ),
        <String, Object?>{
          'guild_ids': <String>['10@remote.test', '11@remote.test'],
        },
      );
      expect(
        () => channelInfoRequestPayload(
          '10@remote.test',
          <String>['status', 'status'],
        ),
        throwsArgumentError,
      );
      expect(
        () => soundboardSoundsRequestPayload(<String>[]),
        throwsArgumentError,
      );
    });

    final tokens = SessionTokens(
      instance: Domain('chat.example'),
      accessToken: 'access-token',
      refreshToken: 'refresh-token',
    );

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

    test('strips peer-asserted decrypted state from dispatch data', () {
      final message = decodeGatewayEnvelope(jsonEncode(<String, Object?>{
        'op': GatewayOp.dispatch.value,
        's': 18,
        't': 'MESSAGE_CREATE',
        'd': <String, Object?>{
          'id': '1',
          'e2ee_verified': true,
          'decrypted_content': 'peer-injected plaintext',
          'attachments': <Object?>[
            <String, Object?>{
              'id': '2',
              'encrypted_manifest': <String, Object?>{'key': 'secret'},
            },
          ],
        },
      }));

      expect(message.objectData, <String, Object?>{
        'id': '1',
        'attachments': <Object?>[
          <String, Object?>{'id': '2'},
        ],
      });
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

    test('heartbeats leave scheduling margin before the gateway deadline', () {
      expect(
        gatewayHeartbeatCadence(41250),
        const Duration(milliseconds: 30937),
      );
      expect(gatewayHeartbeatCadence(1000), const Duration(seconds: 1));
    });

    test('turns structured close reasons into safe recovery guidance', () {
      final limited = gatewayCloseDetails(
        GatewayCloseCode.rateLimited.value,
        jsonEncode(<String, Object?>{
          'code': 'RATE_LIMITED',
          'retry_after_ms': 2500,
          'debug': 'token=do-not-display',
        }),
      );
      expect(limited.message, contains('rate limited'));
      expect(limited.message, contains('3 seconds'));
      expect(limited.retryAfter, const Duration(milliseconds: 2500));
      expect(limited.message, isNot(contains('do-not-display')));

      final backendRateLimit = gatewayCloseDetails(
        GatewayCloseCode.rateLimited.value,
        jsonEncode(<String, Object?>{'retry_after_ms': 1250}),
      );
      expect(backendRateLimit.message, contains('2 seconds'));
      expect(
        backendRateLimit.retryAfter,
        const Duration(milliseconds: 1250),
      );

      final sessionLimit = gatewayCloseDetails(
        4000,
        jsonEncode(<String, Object?>{'code': 'SESSION_LIMIT'}),
      );
      expect(sessionLimit.message, contains('too many active'));
      expect(sessionLimit.message, contains('another device'));
    });

    test('never displays arbitrary websocket close text', () {
      final details = gatewayCloseDetails(
        4999,
        'database password=secret at /srv/gateway',
      );
      expect(details.message, contains('interrupted'));
      expect(details.message, isNot(contains('password')));
      expect(details.message, isNot(contains('/srv')));
    });

    test('abandons a transport upgrade that never completes', () async {
      final stalledSocket = Completer<WebSocket>();
      final client = GatewayClient(
        tokens: () async => tokens,
        socketConnector: (_) => IOWebSocketChannel(stalledSocket.future),
        transportReadyTimeout: const Duration(milliseconds: 20),
        sessionReadyTimeout: const Duration(milliseconds: 40),
        transportCloseTimeout: const Duration(milliseconds: 20),
      );
      addTearDown(client.close);

      await expectLater(
        client.connect(tokens),
        throwsA(isA<TimeoutException>()),
      );

      expect(
        client.currentHealth.phase,
        GatewayConnectionPhase.reconnecting,
      );
    });

    test('retries when an upgraded transport never starts a session', () async {
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      server.listen((request) async {
        await WebSocketTransformer.upgrade(request);
      });
      addTearDown(() => server.close(force: true));
      final endpoint = Uri.parse('ws://127.0.0.1:${server.port}');
      final client = GatewayClient(
        tokens: () async => tokens,
        socketConnector: (_) => IOWebSocketChannel.connect(endpoint),
        transportReadyTimeout: const Duration(seconds: 1),
        sessionReadyTimeout: const Duration(milliseconds: 40),
        transportCloseTimeout: const Duration(milliseconds: 40),
      );
      addTearDown(client.close);
      final reconnecting = client.health.firstWhere(
        (health) => health.phase == GatewayConnectionPhase.reconnecting,
      );

      await client.connect(tokens);
      expect(client.currentHealth.phase, GatewayConnectionPhase.connecting);

      final health = await reconnecting.timeout(const Duration(seconds: 1));
      expect(health.message, contains('did not respond'));
    });

    test('authenticates then heartbeats immediately after hello', () async {
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      final received = Completer<List<int>>();
      server.listen((request) async {
        final socket = await WebSocketTransformer.upgrade(request);
        final ops = <int>[];
        socket.listen((raw) {
          final payload = jsonDecode(raw as String) as Map<String, Object?>;
          ops.add(payload['op']! as int);
          if (ops.length == 2 && !received.isCompleted) {
            received.complete(List<int>.unmodifiable(ops));
          }
        });
        socket.add(jsonEncode(<String, Object?>{
          'op': GatewayOp.hello.value,
          'd': <String, Object?>{'heartbeat_interval': 1000},
        }));
      });
      addTearDown(() => server.close(force: true));
      final endpoint = Uri.parse('ws://127.0.0.1:${server.port}');
      final client = GatewayClient(
        tokens: () async => tokens,
        socketConnector: (_) => IOWebSocketChannel.connect(endpoint),
        transportReadyTimeout: const Duration(seconds: 1),
        sessionReadyTimeout: const Duration(seconds: 1),
        transportCloseTimeout: const Duration(milliseconds: 40),
      );
      addTearDown(client.close);

      await client.connect(tokens);

      expect(
        await received.future.timeout(const Duration(seconds: 1)),
        <int>[GatewayOp.identify.value, GatewayOp.heartbeat.value],
      );
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

    test('deletion copy clears client-only decrypted and forwarded state', () {
      final source = _message(id: '101', domain: 'chat.example').copyWith(
        e2ee: <String, Object?>{'ciphertext': 'opaque'},
        e2eeVerified: true,
        forwardedMessageRef: EntityRef.parse('102@remote.example'),
        forwardedMessage: _message(id: '102', domain: 'remote.example'),
        decryptedForwardSnapshot: <String, Object?>{'content': 'secret'},
        decryptedAllowedMentions: <String, Object?>{
          'parse': <String>['users']
        },
      );

      final deleted = source.copyWith(
        clearContent: true,
        clearE2ee: true,
        e2eeVerified: false,
        clearForwardedMessageRef: true,
        clearForwardedMessage: true,
        clearDecryptedForwardSnapshot: true,
        clearDecryptedAllowedMentions: true,
        deletedAt: DateTime.utc(2026, 8, 29),
      );

      expect(deleted.e2ee, isNull);
      expect(deleted.e2eeVerified, isFalse);
      expect(deleted.forwardedMessageRef, isNull);
      expect(deleted.forwardedMessage, isNull);
      expect(deleted.decryptedForwardSnapshot, isNull);
      expect(deleted.decryptedAllowedMentions, isNull);
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
