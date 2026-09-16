import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:mocktail/mocktail.dart';

class _Messaging extends Mock implements FirebaseMessaging {}

class _Settings extends Mock implements NotificationSettings {}

void main() {
  late _Messaging messaging;
  late PushService service;

  setUp(() {
    messaging = _Messaging();
    final settings = _Settings();
    when(() => settings.authorizationStatus)
        .thenReturn(AuthorizationStatus.authorized);
    when(() => messaging.getNotificationSettings())
        .thenAnswer((_) async => settings);
    service = PushService.test(messaging: messaging);
  });

  tearDown(() => service.dispose());

  testWidgets('waits for Apple registration before returning an FCM token',
      (tester) async {
    var attempts = 0;
    when(() => messaging.getToken()).thenAnswer((_) async {
      if (++attempts < 3) {
        throw FirebaseException(
            plugin: 'firebase_messaging', code: 'apns-token-not-set');
      }
      return 'fcm-token';
    });
    String? token;
    final request = service.pushToken().then((value) => token = value);
    await tester.pump();
    expect(attempts, 1);
    expect(token, isNull);
    await tester.pump(const Duration(milliseconds: 500));
    expect(attempts, 2);
    await tester.pump(const Duration(milliseconds: 500));
    await request;
    expect(token, 'fcm-token');
  });

  testWidgets('missing Apple registration stops with an actionable error',
      (tester) async {
    when(() => messaging.getToken()).thenThrow(FirebaseException(
        plugin: 'firebase_messaging', code: 'apns-token-not-set'));
    final result = expectLater(
      service.pushToken(),
      throwsA(isA<KaedeException>().having(
        (error) => userFacingError(error),
        'message',
        contains('Apple notification registration is not ready'),
      )),
    );
    await tester.pump();
    for (var attempt = 0; attempt < 20; attempt += 1) {
      await tester.pump(const Duration(milliseconds: 500));
    }
    await result;
    verify(() => messaging.getToken()).called(21);
    await tester.pump(const Duration(seconds: 30));
    verifyNever(() => messaging.getToken());
  });

  test('available token returns immediately without retrying', () async {
    when(() => messaging.getToken()).thenAnswer((_) async => 'fcm-token');
    expect(await service.pushToken(), 'fcm-token');
    verify(() => messaging.getToken()).called(1);
  });

  test('provider errors are actionable and hide exception details', () async {
    when(() => messaging.getToken()).thenThrow(FirebaseException(
      plugin: 'firebase_messaging',
      code: 'unknown',
      message: 'private provider details',
    ));
    await expectLater(
      service.pushToken(),
      throwsA(isA<KaedeException>()
          .having(
            (error) => userFacingError(error),
            'message',
            allOf(contains('notification service'),
                isNot(contains('private provider details'))),
          )
          .having((error) => error.details['provider_code'], 'provider code',
              'unknown')),
    );
    verify(() => messaging.getToken()).called(1);
  });

  test('diagnostics retain codes without messages or secret payloads', () {
    final report = pushFailureDiagnostics(
      'relay_subscription',
      const KaedeException(
        code: 'PUSH_TOKEN_UNAVAILABLE',
        message: 'secret message',
        status: 503,
        details: {
          'provider_code': 'apns-token-not-set',
          'provider_token': 'secret token',
          'management_secret': 'secret management value',
        },
      ),
    );
    expect(report, contains('Step: relay_subscription'));
    expect(report, contains('Error code: PUSH_TOKEN_UNAVAILABLE'));
    expect(report, contains('Provider code: apns-token-not-set'));
    expect(report, isNot(contains('secret')));
    final malformed = pushFailureDiagnostics(
      'provider_token',
      FirebaseException(
        plugin: 'firebase_messaging',
        code: 'unknown\nprivate payload',
        message: 'secret message',
      ),
    );
    expect(malformed, contains('Error code: unknown'));
    expect(malformed, isNot(contains('private payload')));
    expect(malformed, isNot(contains('secret')));
  });

  test('denied permission never requests a token', () async {
    final settings = _Settings();
    when(() => settings.authorizationStatus)
        .thenReturn(AuthorizationStatus.denied);
    when(() => messaging.getNotificationSettings())
        .thenAnswer((_) async => settings);
    expect(await service.pushToken(), isNull);
    verifyNever(() => messaging.getToken());
  });

  testWidgets('stalled token request still times out', (tester) async {
    when(() => messaging.getToken())
        .thenAnswer((_) => Completer<String?>().future);
    final result = expectLater(
      service.pushToken(),
      throwsA(isA<KaedeException>()
          .having((error) => error.code, 'code', 'PUSH_TOKEN_TIMEOUT')),
    );
    await tester.pump();
    await tester.pump(const Duration(seconds: 30));
    await result;
  });
}
