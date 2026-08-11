import 'package:dio/dio.dart';

final class KaedeException implements Exception {
  const KaedeException({
    required this.code,
    required this.message,
    required this.status,
    this.traceId,
    this.retryAfter,
    this.details = const <String, Object?>{},
  });

  factory KaedeException.fromDio(DioException error) {
    final response = error.response;
    final raw = response?.data;
    final body =
        raw is Map ? Map<String, Object?>.from(raw) : const <String, Object?>{};
    final nested = body['detail'];
    final detail = nested is Map ? Map<String, Object?>.from(nested) : body;
    final status = response?.statusCode ?? 0;
    final code = detail['code'] as String? ?? 'NETWORK_ERROR';
    final supplied = detail['message'] as String?;
    return KaedeException(
      code: code,
      message: _friendlyMessage(code, supplied, status),
      status: status,
      traceId: detail['trace_id'] as String? ??
          response?.headers.value('X-Kaede-Trace-Id'),
      retryAfter: _retryDuration(detail, response),
      details: detail,
    );
  }

  final String code;
  final String message;
  final int status;
  final String? traceId;
  final Duration? retryAfter;
  final Map<String, Object?> details;

  static Duration? _retryDuration(
    Map<String, Object?> detail,
    Response<Object?>? response,
  ) {
    final milliseconds = detail['retry_after_ms'];
    if (milliseconds is num) {
      return Duration(milliseconds: milliseconds.round());
    }
    final seconds =
        double.tryParse(response?.headers.value('Retry-After') ?? '');
    return seconds == null
        ? null
        : Duration(milliseconds: (seconds * 1000).round());
  }

  static String _friendlyMessage(String code, String? supplied, int status) {
    const messages = <String, String>{
      'MISSING_PERMISSIONS': 'You do not have permission to do that.',
      'ROLE_HIERARCHY': 'That member or role is above your highest role.',
      'OWNER_IMMUNE': 'The guild owner cannot be moderated.',
      'SLOWMODE_RATE_LIMITED':
          'Slow mode is active. Wait before sending again.',
      'VOICE_DISABLED': 'Voice is disabled on this instance.',
      'VOICE_HOME_UNREACHABLE': 'The voice server is temporarily unavailable.',
      'TURNSTILE_REQUIRED': 'Complete the security check to continue.',
    };
    if (supplied != null &&
        supplied.trim().isNotEmpty &&
        supplied.toLowerCase() != 'forbidden') {
      return supplied;
    }
    return messages[code] ??
        (status == 403
            ? 'You do not have permission to do that.'
            : 'Request failed.');
  }

  @override
  String toString() => message;
}
