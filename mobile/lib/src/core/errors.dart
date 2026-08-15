import 'dart:async';
import 'dart:io';

import 'package:dio/dio.dart';

final class UserInputException implements Exception {
  const UserInputException(this.message);

  final String message;

  @override
  String toString() => message;
}

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
    final body = raw is Map
        ? raw.map<String, Object?>((key, value) => MapEntry('$key', value))
        : const <String, Object?>{};
    final nested = body['detail'];
    final detail = nested is Map
        ? nested.map<String, Object?>((key, value) => MapEntry('$key', value))
        : body;
    final status = response?.statusCode ?? 0;
    final code = _string(detail['code']) ??
        _string(body['code']) ??
        (status == 0 ? 'NETWORK_ERROR' : 'HTTP_ERROR');
    final retryAfter = _retryDuration(detail, response);
    final supplied = _string(detail['message']) ??
        _string(body['message']) ??
        (nested is String ? nested : null);
    final validation =
        nested is List ? nested : detail['errors'] ?? body['errors'];
    return KaedeException(
      code: code,
      message: _friendlyMessage(
        code: code,
        supplied: supplied,
        status: status,
        type: error.type,
        retryAfter: retryAfter,
        maxBytes: detail['max_bytes'] ?? body['max_bytes'],
        validation: validation,
      ),
      status: status,
      traceId: _safeTraceId(
        _string(detail['trace_id']) ??
            _string(body['trace_id']) ??
            response?.headers.value('X-Kaede-Trace-Id'),
      ),
      retryAfter: retryAfter,
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

  static String _friendlyMessage({
    required String code,
    required String? supplied,
    required int status,
    required DioExceptionType type,
    required Duration? retryAfter,
    required Object? maxBytes,
    required Object? validation,
  }) {
    final known = _messageForCode(code, retryAfter, maxBytes: maxBytes);
    if (retryAfter != null && known != null) {
      return _withRetryInstruction(known, retryAfter);
    }
    if (maxBytes != null && known != null) return known;

    final safeSupplied = _safeServerMessage(supplied, status);
    if (safeSupplied != null) return safeSupplied;

    final validationMessage = _validationMessage(validation);
    if (validationMessage != null) return validationMessage;
    if (known != null) return known;

    if (status == 0) {
      return switch (type) {
        DioExceptionType.connectionTimeout =>
          'The server took too long to connect. Check your connection and try again.',
        DioExceptionType.sendTimeout =>
          'The upload took too long. Check your connection and try again.',
        DioExceptionType.receiveTimeout =>
          'The server took too long to respond. Try again.',
        DioExceptionType.badCertificate =>
          'Kaede could not verify the server’s secure connection. Check the server address and your device clock.',
        DioExceptionType.cancel => 'The request was cancelled.',
        _ =>
          'Kaede could not reach the server. Check your connection and the server address, then try again.',
      };
    }

    return switch (status) {
      400 =>
        'The server could not use the information sent. Check your entries and try again.',
      401 => 'Your session expired. Sign in again.',
      403 => 'You do not have permission to do that.',
      404 => 'That item no longer exists, or you no longer have access to it.',
      408 => 'The request timed out. Check your connection and try again.',
      409 => 'This changed somewhere else. Refresh the page and try again.',
      413 => _tooLargeMessage('file', maxBytes),
      415 => 'That file type is not supported.',
      422 =>
        'Some information was not accepted. Check your entries and try again.',
      429 => _rateLimitMessage(retryAfter),
      500 =>
        'The server ran into an unexpected problem. Try again. If it keeps happening, contact your instance operator.',
      502 ||
      503 ||
      504 =>
        'The server is temporarily unavailable. Try again in a moment.',
      507 =>
        'This server does not currently have enough storage capacity. Try again later or contact your instance administrator.',
      _ => 'The request could not be completed. Try again.',
    };
  }

  static String? _messageForCode(
    String code,
    Duration? retryAfter, {
    Object? maxBytes,
  }) {
    if (code == 'SLOWMODE_RATE_LIMITED') {
      return retryAfter == null
          ? 'Slow mode is active. Wait before sending another message.'
          : 'Slow mode is active. ${_retryInstruction(retryAfter)}';
    }
    if (code == 'LOGIN_RATE_LIMITED') {
      return retryAfter == null
          ? 'Too many sign-in attempts. Wait a moment before trying again.'
          : 'Too many sign-in attempts. ${_retryInstruction(retryAfter)}';
    }
    if (code == 'MFA_RATE_LIMITED') {
      return retryAfter == null
          ? 'Too many verification attempts. Wait a moment before trying again.'
          : 'Too many verification attempts. ${_retryInstruction(retryAfter)}';
    }
    if (code == 'ATTACHMENT_TOO_LARGE') {
      return _tooLargeMessage('attachment', maxBytes);
    }
    if (code == 'EMOJI_TOO_LARGE') {
      return _tooLargeMessage('emoji image', maxBytes);
    }
    if (code == 'IMPORT_TOO_LARGE') {
      return _tooLargeMessage('import file', maxBytes);
    }
    const messages = <String, String>{
      'ADMIN_AUTHENTICATION_REQUIRED':
          'Administrator authentication is required for that action.',
      'AUTHENTICATION_REQUIRED': 'Sign in to continue.',
      'SESSION_EXPIRED': 'Your session expired. Sign in again.',
      'MISSING_PERMISSIONS': 'You do not have permission to do that.',
      'CANNOT_GRANT_PERMISSIONS':
          'You cannot grant permissions that you do not have.',
      'CANNOT_MANAGE_PERMISSIONS':
          'You do not have permission to change those permissions.',
      'ROLE_HIERARCHY': 'That member or role is above your highest role.',
      'OWNER_IMMUNE': 'The guild owner cannot be moderated.',
      'GUILD_OWNER_REQUIRED': 'Only the guild owner can do that.',
      'OWNER_MUST_TRANSFER_OR_DELETE_GUILD':
          'Transfer ownership or delete the guild before leaving it.',
      'OWNER_TRANSFER_REQUIRES_LOCAL_MEMBER':
          'Ownership can only be transferred to a member on this guild’s home instance.',
      'BANNED_FROM_GUILD': 'You are banned from that guild.',
      'INSTANCE_BANNED_FROM_GUILD':
          'Your home instance is banned from that guild.',
      'MEMBER_TIMED_OUT': 'You cannot send messages while timed out.',
      'FEDERATED_MODERATION_STATUS_INVALID':
          'The guild’s home instance returned invalid timeout details. Sending is still checked by the guild home.',
      'FEDERATED_MODERATION_STATUS_UNAVAILABLE':
          'Your timeout details are temporarily unavailable from the guild’s home instance. Sending is still checked by the guild home.',
      'RELATIONSHIP_BLOCKED':
          'That action is unavailable because one of you blocked the other.',
      'DM_PRIVACY_REJECTED':
          'That user’s privacy settings do not allow this direct message.',
      'CANNOT_DM_SELF': 'You cannot open a direct message with yourself.',
      'CANNOT_DM_USER': 'You cannot send a direct message to that user.',
      'CANNOT_FRIEND_SELF': 'You cannot send a friend request to yourself.',
      'CHANNEL_NOT_EMPTY':
          'Move or delete the channels inside this category first.',
      'CHANNEL_SET_CHANGED':
          'The channel list changed somewhere else. Refresh and try again.',
      'ROLE_STATE_CHANGED':
          'The role list changed somewhere else. Refresh and try again.',
      'SETTINGS_VERSION_CONFLICT':
          'These settings changed somewhere else. Reload them and try again.',
      'PASSWORD_WORK_BUSY':
          'The server is busy checking passwords. Try again in a moment.',
      'TURNSTILE_REQUIRED': 'Complete the security check to continue.',
      'PUSH_DISABLED':
          'Push notifications are not configured on this instance.',
      'PUSH_RELAY_APP_MISMATCH':
          'This app is not compatible with your home’s configured notification relay.',
      'PUSH_RELAY_DISABLED':
          'Your home does not currently support background delivery for this app.',
      'PUSH_RELAY_ENROLLMENT_EXISTS':
          'Notification setup is already in progress. Wait a moment and try again.',
      'PUSH_RELAY_ENROLLMENT_EXPIRED':
          'Notification setup expired. Enable background notifications again.',
      'PUSH_RELAY_GRANT_INVALID':
          'The signed notification setup request is invalid or expired.',
      'PUSH_RELAY_RATE_LIMITED':
          'Too many notification registrations were attempted. Wait before trying again.',
      'PUSH_RELAY_RECEIPT_INVALID':
          'The notification relay could not complete registration. Try again later.',
      'GIF_PICKER_DISABLED': 'The GIF picker is disabled on this instance.',
      'GIF_PROVIDER_UNAVAILABLE':
          'The GIF provider is temporarily unavailable. Try again later.',
      'UPLOAD_TICKET_EXPIRED':
          'The upload took too long to start. Choose the file and try again.',
      'UPLOAD_INCOMPLETE':
          'The file did not finish uploading. Check your connection and try again.',
      'UPLOAD_SIZE_MISMATCH':
          'The uploaded file size did not match the selected file. Choose it again.',
      'UPLOAD_TYPE_MISMATCH':
          'The uploaded file type did not match the selected file. Choose it again.',
      'UPLOAD_INFLIGHT_LIMIT':
          'Too many uploads are already in progress. Wait for one to finish.',
      'UPLOAD_INFLIGHT_QUOTA_EXCEEDED':
          'Your active uploads exceed this server’s storage limit. Wait or remove an upload.',
      'USER_STORAGE_QUOTA_EXCEEDED':
          'Your attachment storage is full. Remove files before uploading another.',
      'MEDIA_STORAGE_UNAVAILABLE':
          'Attachment storage is temporarily unavailable. Try again later.',
      'MEDIA_NOT_FOUND': 'That attachment no longer exists.',
      'KAED_MEDIA_NOT_FOUND': 'That attachment no longer exists.',
      'MEDIA_NOT_AVAILABLE':
          'That attachment is not ready yet. Try again in a moment.',
      'KAED_MEDIA_UNAVAILABLE':
          'That attachment is temporarily unavailable. Try again later.',
      'REMOTE_MEDIA_REJECTED':
          'That remote file did not pass this server’s safety checks.',
      'REMOTE_MEDIA_BUSY':
          'The remote server is still preparing that attachment.',
      'REMOTE_MEDIA_CACHE_FULL':
          'This instance’s remote-media cache is full. Kaede is clearing older cached files.',
      'REMOTE_MEDIA_UNAVAILABLE':
          'That remote file is temporarily unavailable. Try again later.',
      'KAED_FED_INBOX_QUOTA_EXCEEDED':
          'This instance is temporarily at its retained federation-event limit. The remote server will retry automatically.',
      'FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED':
          'This instance cannot cache another remote account right now. Contact your instance administrator if this continues.',
      'FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED':
          'This instance cannot cache another remote server right now. Contact your instance administrator if this continues.',
      'FEDERATION_OUTBOX_CAPACITY_EXCEEDED':
          'This instance’s delivery queue for that remote server is full. Nothing was saved; wait for queued federation work to clear and try again.',
      'KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED':
          'The receiving instance cannot retain another remote account right now. This operation was not completed.',
      'KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED':
          'The receiving instance cannot retain another remote server right now. This operation was not completed.',
      'KAED_FED_OUTBOX_CAPACITY_EXCEEDED':
          'The receiving instance’s outbound federation queue is full. Kaede will retry automatically after queued work clears.',
      'KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED':
          'The receiving instance cannot accept another pending friend request right now. Your request was not delivered.',
      'KAED_FED_REPLICA_QUOTA_EXCEEDED':
          'This guild’s local replica reached its cache limit. New messages and changes may be missing until your instance frees space.',
      'KAED_FED_HISTORY_CAPACITY':
          'This instance is already importing the maximum amount of remote message history. The import will be retried automatically.',
      'FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE':
          'Older guild messages are temporarily unavailable. Kaede will retry automatically; recent messages and new activity remain available.',
      'FEDERATED_GUILD_HISTORY_LIMIT_REACHED':
          'This instance reached its configured limit for cached guild history. Recent messages and new activity remain available; contact your instance administrator if you need older history.',
      'FEDERATED_GUILD_HISTORY_REJECTED':
          'Older messages from this guild’s home instance could not be safely imported. Recent messages and new activity remain available.',
      'FEDERATED_DM_STORAGE_QUOTA_EXCEEDED':
          'This instance could not retain more direct-message data. Recent remote messages are normally kept by removing the oldest cached copies; if this persists, contact your instance administrator.',
      'KAED_FED_DM_STORAGE_QUOTA_EXCEEDED':
          'The receiving instance could not retain more direct-message data. Delivery cannot continue until it frees space or raises its limit.',
      'KAED_FED_DELIVERY_EXPIRED':
          'The remote instance did not accept this operation before the delivery window ended. Try the operation again later.',
      'KAED_FED_EVENT_TOO_LARGE':
          'This operation is too large to send between instances. Reduce its size and try again.',
      'FEDERATED_DM_HISTORY_TRUNCATED':
          'This instance keeps recent messages here and loads older messages from their home instance as you scroll.',
      'FEDERATED_DM_HISTORY_UNAVAILABLE':
          'Older messages could not be loaded from their home instance right now. Your recent messages are still available; try again in a moment.',
      'SEARCH_DISABLED_FOR_E2EE':
          'Search is unavailable in end-to-end encrypted conversations because the server cannot read or index their contents.',
      'SEARCH_DISABLED_BY_INSTANCE':
          'Message search is disabled by this instance’s administrator.',
      'SEARCH_UNAVAILABLE':
          'Message search is temporarily unavailable. Try again shortly.',
      'INVALID_SEARCH_CURSOR':
          'That search page expired. Run the search again.',
      'FEDERATED_SEARCH_RESPONSE_INVALID':
          'The other server returned an invalid search response. Locally cached messages may still be available.',
      'VOICE_DISABLED': 'Voice is disabled on this instance.',
      'VOICE_DENIED': 'You do not have permission to join that voice channel.',
      'VOICE_NOT_CONNECTED': 'You are no longer connected to voice.',
      'VOICE_HOME_UNREACHABLE':
          'The voice server is temporarily unavailable. Try again in a moment.',
      'CALL_HOME_UNREACHABLE':
          'The call server is temporarily unavailable. Try again in a moment.',
      'CALL_REJECTED': 'The call was declined.',
      'CALL_NOT_ACCEPTED': 'The call has not been accepted yet.',
      'FEDERATION_UNAVAILABLE':
          'The remote instance is temporarily unavailable. Try again later.',
      'FEDERATED_WRITE_UNAVAILABLE':
          'The remote instance could not save that change. Try again later.',
      'FEDERATION_LOOKUP_RATE_LIMITED':
          'Too many remote lookups. Wait a moment before trying again.',
      'INTERNAL_SERVER_ERROR':
          'The server ran into an unexpected problem. Try again. If it keeps happening, contact your instance operator.',
    };
    if (messages[code] case final message?) return message;
    if (code == 'RATE_LIMITED' ||
        code == 'KAED_RATE_LIMITED' ||
        code.endsWith('_RATE_LIMITED')) {
      return _rateLimitMessage(retryAfter);
    }
    if (code.endsWith('_NOT_FOUND')) {
      return 'That item no longer exists, or you no longer have access to it.';
    }
    if (code.startsWith('FEDERATION_') || code.startsWith('KAED_FED_')) {
      return 'The remote instance could not complete that request. Try again later.';
    }
    return null;
  }

  static String _rateLimitMessage(Duration? retryAfter) {
    if (retryAfter == null || retryAfter <= Duration.zero) {
      return 'Too many requests. Wait a moment before trying again.';
    }
    return 'Too many requests. ${_retryInstruction(retryAfter)}';
  }

  static String _retryInstruction(Duration retryAfter) {
    final seconds = (retryAfter.inMilliseconds / 1000).ceil();
    final wait = seconds < 60
        ? '$seconds second${seconds == 1 ? '' : 's'}'
        : '${(seconds / 60).ceil()} minute${seconds <= 60 ? '' : 's'}';
    return 'Try again in $wait.';
  }

  static String _withRetryInstruction(String message, Duration retryAfter) {
    if (RegExp(r'try again in \d', caseSensitive: false).hasMatch(message)) {
      return message;
    }
    final withoutVagueDelay = message.replaceFirst(
      RegExp(
        r'\s*Try again (?:in a moment|later|shortly)\.$',
        caseSensitive: false,
      ),
      '',
    );
    return '${_sentence(withoutVagueDelay)} ${_retryInstruction(retryAfter)}';
  }

  static String _tooLargeMessage(String subject, Object? maximum) {
    final bytes =
        maximum is num ? maximum.round() : int.tryParse('${maximum ?? ''}');
    if (bytes == null || bytes <= 0) {
      return 'That $subject is larger than this server allows.';
    }
    return 'That $subject is larger than this server’s ${_formatBytes(bytes)} limit.';
  }

  static String _formatBytes(int bytes) {
    const kibibyte = 1024;
    const mebibyte = kibibyte * 1024;
    const gibibyte = mebibyte * 1024;
    if (bytes >= gibibyte && bytes % gibibyte == 0) {
      return '${bytes ~/ gibibyte} GiB';
    }
    if (bytes >= mebibyte) {
      final value = bytes / mebibyte;
      return '${value == value.roundToDouble() ? value.round() : value.toStringAsFixed(1)} MiB';
    }
    if (bytes >= kibibyte) {
      final value = bytes / kibibyte;
      return '${value == value.roundToDouble() ? value.round() : value.toStringAsFixed(1)} KiB';
    }
    return '$bytes bytes';
  }

  static String? _validationMessage(Object? validation) {
    if (validation is! List || validation.isEmpty) return null;
    final first = validation.first;
    if (first is! Map) {
      return 'Some information was not accepted. Check your entries and try again.';
    }
    final message = _string(first['message']) ?? _string(first['msg']);
    if (message == null || message.trim().isEmpty) {
      return 'Some information was not accepted. Check your entries and try again.';
    }
    final location = first['location'] ?? first['loc'];
    String? field;
    if (location is List && location.isNotEmpty) {
      final useful = location
          .map((item) => '$item')
          .where((item) => item != 'body' && item != 'query' && item != 'path')
          .toList(growable: false);
      if (useful.isNotEmpty) {
        field = useful.last.replaceAll('_', ' ');
      }
    }
    final safe = _safeServerMessage(message, 422);
    if (safe == null) {
      return 'Some information was not accepted. Check your entries and try again.';
    }
    return field == null
        ? 'Some information was not accepted: $safe'
        : 'Check $field: ${_lowercaseFirst(safe)}';
  }

  static String? _safeServerMessage(String? supplied, int status) {
    if (supplied == null || status >= 500) return null;
    final value = supplied.trim().replaceAll(RegExp(r'\s+'), ' ');
    if (value.isEmpty || value.length > 240) return null;
    final lower = value.toLowerCase();
    const generic = <String>{
      'bad request',
      'error',
      'failed',
      'forbidden',
      'internal server error',
      'not found',
      'request failed',
      'request validation failed',
      'service unavailable',
      'unauthorized',
    };
    if (generic.contains(lower) ||
        RegExp(r'^[A-Z][A-Z0-9_]{2,63}$').hasMatch(value) ||
        _containsSensitiveDetails(lower) ||
        value.contains('<html')) {
      return null;
    }
    return _sentence(value);
  }

  @override
  String toString() => message;
}

/// Converts exceptions into safe, actionable copy for snackbars and error
/// states. Unknown exception text is intentionally not displayed because it
/// can contain implementation details, local paths, or response fragments.
String userFacingError(Object error, {String? summary}) {
  final String reason;
  String? traceId;
  if (error is KaedeException) {
    reason = _sentence(error.message);
    traceId = _safeTraceId(error.traceId);
  } else if (error is UserInputException) {
    reason = _sentence(error.message);
  } else if (error is FormatException && _isUserInputMessage(error.message)) {
    reason = _sentence(error.message.toString());
  } else if (error is FormatException) {
    reason =
        'Kaede received a response it could not understand. Update Kaede and try again. If it continues, contact your instance operator.';
  } else if (error is TimeoutException) {
    reason =
        'The operation took too long. Check your connection and try again.';
  } else if (error is SocketException) {
    reason =
        'Kaede could not reach the server. Check your connection and try again.';
  } else if (error is HttpException) {
    reason =
        'The server rejected the media request. The link may have expired; try again.';
  } else if (error is FileSystemException) {
    reason =
        'Kaede could not read or save the file. Check available device storage and try again.';
  } else if (error is String &&
      RegExp(r'^[A-Z][A-Z0-9_]{2,63}$').hasMatch(error.trim())) {
    reason = KaedeException._messageForCode(error.trim(), null) ??
        'The request could not be completed. Try again.';
  } else if (error is String && _safeLocalMessage(error)) {
    reason = _sentence(error);
  } else {
    reason =
        'Something unexpected went wrong. Try again. If it keeps happening, restart Kaede.';
  }

  final prefix = summary == null || summary.trim().isEmpty
      ? ''
      : '${_sentence(summary.trim())} ';
  final reference = traceId == null ? '' : ' Reference: $traceId.';
  return '$prefix$reason$reference';
}

/// Formats the structured failure payloads delivered by the realtime gateway.
/// Gateway messages do not pass through Dio, so they need the same code and
/// trace handling explicitly.
String userFacingGatewayError(
  Map<String, Object?> payload, {
  required String fallback,
}) {
  final code = _string(payload['code']) ?? 'GATEWAY_ERROR';
  final supplied = _string(payload['reason']) ?? _string(payload['message']);
  final retryMilliseconds = payload['retry_after_ms'];
  final retryAfter = retryMilliseconds is num
      ? Duration(milliseconds: retryMilliseconds.round())
      : null;
  final known = KaedeException._messageForCode(
    code,
    retryAfter,
    maxBytes: payload['max_bytes'],
  );
  final safeSupplied = KaedeException._safeServerMessage(supplied, 400);
  final message = (retryAfter != null || payload['max_bytes'] != null)
      ? known ?? safeSupplied ?? fallback
      : safeSupplied ?? known ?? fallback;
  return userFacingError(KaedeException(
    code: code,
    message: message,
    status: 400,
    traceId: _safeTraceId(_string(payload['trace_id'])),
    retryAfter: retryAfter,
    details: payload,
  ));
}

String? _string(Object? value) => value is String ? value : null;

String _sentence(String value) {
  final trimmed = value.trim();
  if (trimmed.isEmpty) return trimmed;
  final capitalized = '${trimmed[0].toUpperCase()}${trimmed.substring(1)}';
  return RegExp(r'[.!?]$').hasMatch(capitalized)
      ? capitalized
      : '$capitalized.';
}

String _lowercaseFirst(String value) =>
    value.isEmpty ? value : '${value[0].toLowerCase()}${value.substring(1)}';

String? _safeTraceId(String? value) {
  final trimmed = value?.trim();
  if (trimmed == null ||
      !RegExp(r'^[A-Za-z0-9._:-]{4,128}$').hasMatch(trimmed)) {
    return null;
  }
  return trimmed;
}

bool _isUserInputMessage(Object? value) {
  if (value is! String) return false;
  const allowed = <String>{
    'Passwords do not match.',
    'This server requires an email address.',
    'Password recovery is not enabled on this server.',
    'Enter the verification token.',
  };
  return allowed.contains(value.trim());
}

bool _safeLocalMessage(String value) {
  final trimmed = value.trim();
  if (trimmed.isEmpty || trimmed.length > 240) return false;
  final lower = trimmed.toLowerCase();
  return !_containsSensitiveDetails(lower) && !lower.contains('<html');
}

bool _containsSensitiveDetails(String lower) =>
    lower.contains('traceback') ||
    lower.contains('stack trace') ||
    lower.contains('sqlalchemy') ||
    lower.contains('exception:') ||
    lower.contains('authorization:') ||
    lower.contains('bearer ') ||
    lower.contains('api_key') ||
    lower.contains('api key') ||
    lower.contains('client_secret') ||
    lower.contains('password=') ||
    lower.contains('private_key') ||
    lower.contains('private key') ||
    lower.contains('secret=') ||
    lower.contains('token=') ||
    lower.contains('/data/user/') ||
    lower.contains('/home/') ||
    lower.contains('/srv/') ||
    lower.contains('/tmp/') ||
    lower.contains('/var/') ||
    RegExp(r'[a-z]:\\').hasMatch(lower) ||
    lower.contains('file://');
