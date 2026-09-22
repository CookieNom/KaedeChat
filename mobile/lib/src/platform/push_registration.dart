import 'package:kaede_mobile/src/core/errors.dart';

/// Coalesces lifecycle/token callbacks and renews long-lived relay routes daily.
final class PushRegistration {
  PushRegistration({DateTime Function()? now}) : _now = now ?? DateTime.now;

  final DateTime Function() _now;
  Future<bool>? _pending;
  (String, int)? _session;
  String? _token;
  DateTime? _renewAfter;
  DateTime? _retryAfter;
  KaedeException? _rateLimit;

  Future<void> invalidate() async {
    try {
      await _pending;
    } on Object {
      // Disabling notifications must also work after a failed registration.
    } finally {
      _renewAfter = null;
    }
  }

  void recordFailure(Object error) {
    if (error is KaedeException && error.status == 429) {
      _rateLimit = error;
      _retryAfter = _now().add(error.retryAfter ?? const Duration(minutes: 1));
    }
  }

  Future<bool> run({
    required (String, int) session,
    required String token,
    required Future<bool> Function() register,
    bool force = false,
  }) async {
    final pending = _pending;
    if (pending != null) {
      final sameRegistration = _session == session && _token == token;
      final result = await pending;
      if (sameRegistration) return result;
      return run(
          session: session, token: token, register: register, force: force);
    }
    if (!force &&
        _session == session &&
        _token == token &&
        _renewAfter != null &&
        _now().isBefore(_renewAfter!)) {
      return true;
    }
    if (_retryAfter != null && _now().isBefore(_retryAfter!)) {
      throw _rateLimit!;
    }
    _session = session;
    _token = token;
    _renewAfter = null;
    final operation = Future<bool>.sync(register);
    _pending = operation;
    try {
      final registered = await operation;
      if (registered) _renewAfter = _now().add(const Duration(days: 1));
      return registered;
    } on Object catch (error) {
      recordFailure(error);
      rethrow;
    } finally {
      _pending = null;
    }
  }
}
