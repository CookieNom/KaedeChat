import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/e2ee/setup_retry.dart';

void main() {
  KaedeException failure(String code, int status) =>
      KaedeException(code: code, message: code, status: status);
  final leaseExpired = failure('E2EE_ACCOUNT_VAULT_LEASE_EXPIRED', 409);
  final vaultBusy = failure('E2EE_ACCOUNT_VAULT_BUSY', 409);
  final unreachable = failure('NETWORK_ERROR', 0);

  /// Fails with each of [failures] in turn, then succeeds.
  ({Future<String> Function() attempt, int Function() count}) scripted(
    List<KaedeException> failures,
  ) {
    var calls = 0;
    return (
      attempt: () async {
        calls += 1;
        if (calls <= failures.length) throw failures[calls - 1];
        return 'enabled';
      },
      count: () => calls,
    );
  }

  test('resumes after an expired lease and a dropped connection', () async {
    final pauses = <Duration>[];
    final run = scripted([leaseExpired, unreachable, unreachable]);
    expect(
      await retryE2eeSetup(run.attempt, pause: (delay) async {
        pauses.add(delay);
      }),
      'enabled',
    );
    expect(run.count(), 4);
    // An expired lease is retried at once; a dead connection backs off.
    expect(pauses, const [Duration(seconds: 2), Duration(seconds: 4)]);
  });

  test('gives up at the retry limit and on failures a retry cannot fix',
      () async {
    final offline = scripted(List.filled(10, unreachable));
    await expectLater(
      retryE2eeSetup(offline.attempt, pause: (_) async {}),
      throwsA(same(unreachable)),
    );
    expect(offline.count(), 4);

    final forbidden = failure('FORBIDDEN', 403);
    final denied = scripted([forbidden]);
    await expectLater(
      retryE2eeSetup(denied.attempt, pause: (_) async {}),
      throwsA(same(forbidden)),
    );
    expect(denied.count(), 1);
  });

  test('waits out a locked vault only when asked to', () async {
    final pauses = <Duration>[];
    final waiting = scripted([unreachable, vaultBusy, vaultBusy]);
    expect(
      await retryE2eeSetup(
        waiting.attempt,
        waitForBusyVault: true,
        pause: (delay) async {
          pauses.add(delay);
        },
      ),
      'enabled',
    );
    expect(pauses, const [
      Duration(seconds: 1),
      Duration(seconds: 10),
      Duration(seconds: 10),
    ]);

    final impatient = scripted([vaultBusy]);
    await expectLater(
      retryE2eeSetup(impatient.attempt, pause: (_) async {}),
      throwsA(same(vaultBusy)),
    );
    expect(impatient.count(), 1);
  });

  test('stops retrying once nobody is waiting for the result', () async {
    final abandoned = scripted([unreachable, unreachable]);
    await expectLater(
      retryE2eeSetup(
        abandoned.attempt,
        keepTrying: () => false,
        pause: (_) async {},
      ),
      throwsA(same(unreachable)),
    );
    expect(abandoned.count(), 1);
  });
}
