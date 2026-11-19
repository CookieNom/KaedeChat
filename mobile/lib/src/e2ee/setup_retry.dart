import 'package:kaede_mobile/src/core/errors.dart';

const _maximumRetries = 3;
const _busyVaultPause = Duration(seconds: 10);
// The pauses alone outlast the server's 120-second vault lease, so a lease
// orphaned by a lost response has always expired before these run out.
const _maximumBusyVaultRetries = 13;

/// Runs an encryption setup step again after failures a fresh attempt resumes.
///
/// Opening the encryption client and turning on a room are journaled, so
/// repeating them picks up where the last attempt stopped instead of starting
/// over. That makes a dropped connection or an expired vault lease safe to
/// retry here rather than handing the same retry to the user.
///
/// An attempt whose lease response was lost leaves the vault locked until the
/// server's lease expires. [waitForBusyVault] waits that out; use it only
/// where progress is visible, since it can take a couple of minutes.
/// [keepTrying] is asked before every new attempt and stops the retries once
/// nobody is waiting for the result.
Future<T> retryE2eeSetup<T>(
  Future<T> Function() attempt, {
  bool waitForBusyVault = false,
  bool Function()? keepTrying,
  Future<void> Function(Duration) pause = _pause,
}) async {
  var retries = 0;
  var busyVaultRetries = 0;
  while (true) {
    try {
      return await attempt();
    } on KaedeException catch (error) {
      final Duration delay;
      if (error.code == 'E2EE_ACCOUNT_VAULT_BUSY') {
        if (!waitForBusyVault || busyVaultRetries >= _maximumBusyVaultRetries) {
          rethrow;
        }
        busyVaultRetries += 1;
        delay = _busyVaultPause;
      } else {
        final leaseExpired = error.code == 'E2EE_ACCOUNT_VAULT_LEASE_EXPIRED';
        final unreachable = error.status == 0 ||
            error.status == 502 ||
            error.status == 503 ||
            error.status == 504;
        if (!(leaseExpired || unreachable) || retries >= _maximumRetries) {
          rethrow;
        }
        retries += 1;
        // A new lease is all an expired one needs. Give a failing connection
        // a moment to recover: 1, 2, then 4 seconds.
        delay = leaseExpired
            ? Duration.zero
            : Duration(seconds: 1 << (retries - 1));
      }
      if (delay > Duration.zero) await pause(delay);
      if (keepTrying != null && !keepTrying()) rethrow;
    }
  }
}

Future<void> _pause(Duration duration) => Future<void>.delayed(duration);
