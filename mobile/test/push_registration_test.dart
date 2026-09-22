import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/platform/push_registration.dart';

void main() {
  test('coalesces setup and reuses routes until token, session or day changes',
      () async {
    var now = DateTime.utc(2026);
    final registration = PushRegistration(now: () => now);
    final first = Completer<bool>();
    var requests = 0;
    Future<bool> register() {
      requests++;
      return requests == 1 ? first.future : Future.value(true);
    }

    Future<bool> run({int session = 1, String token = 'token'}) =>
        registration.run(
          session: ('account', session),
          token: token,
          register: register,
        );
    final initial = run();
    final concurrent = run();
    expect(requests, 1);
    first.complete(true);
    expect(await initial, isTrue);
    expect(await concurrent, isTrue);
    await run();
    expect(requests, 1);
    await run(token: 'refreshed');
    expect(requests, 2);
    await run(session: 2, token: 'refreshed');
    expect(requests, 3);
    now = now.add(const Duration(days: 1));
    await run(session: 2, token: 'refreshed');
    expect(requests, 4);
    await registration.invalidate();
    await run(session: 2, token: 'refreshed');
    expect(requests, 5);
  });

  test('rate limit survives sign-in and manual retry until the delay expires',
      () async {
    var now = DateTime.utc(2026);
    final registration = PushRegistration(now: () => now);
    var requests = 0;
    Future<bool> run(int session) => registration.run(
          session: ('account', session),
          token: 'token',
          force: true,
          register: () async {
            if (++requests == 1) {
              throw const KaedeException(
                code: 'PUSH_RELAY_RATE_LIMITED',
                message: 'Wait before trying again.',
                status: 429,
                retryAfter: Duration(minutes: 1),
              );
            }
            return true;
          },
        );
    await expectLater(run(1), throwsA(isA<KaedeException>()));
    now = now.add(const Duration(seconds: 59));
    await expectLater(run(2), throwsA(isA<KaedeException>()));
    expect(requests, 1);
    now = now.add(const Duration(seconds: 1));
    expect(await run(2), isTrue);
    expect(requests, 2);
  });

  test('token changes queue behind setup and disabling clears its result',
      () async {
    final registration = PushRegistration();
    final first = Completer<bool>();
    var requests = 0;
    Future<bool> run(String token) => registration.run(
          session: ('account', 1),
          token: token,
          register: () {
            requests++;
            return requests == 1 ? first.future : Future.value(true);
          },
        );
    final initial = run('old');
    final refreshed = run('new');
    expect(requests, 1);
    first.complete(true);
    await initial;
    expect(await refreshed, isTrue);
    expect(requests, 2);
    await registration.invalidate();
    await run('new');
    expect(requests, 3);
  });

  test('partial call setup keeps messages ready and throttles explicit retry',
      () async {
    final registration = PushRegistration();
    var requests = 0;
    Future<bool> run({bool force = false}) => registration.run(
          session: ('account', 1),
          token: 'token',
          force: force,
          register: () async {
            requests++;
            registration.recordFailure(const KaedeException(
              code: 'PUSH_RELAY_RATE_LIMITED',
              message: 'Wait before trying again.',
              status: 429,
            ));
            return true;
          },
        );
    expect(await run(), isTrue);
    expect(await run(), isTrue);
    await expectLater(run(force: true), throwsA(isA<KaedeException>()));
    expect(requests, 1);
  });
}
