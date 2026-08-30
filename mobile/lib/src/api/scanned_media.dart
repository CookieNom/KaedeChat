import 'dart:async';

import 'package:kaede_mobile/src/core/errors.dart';

typedef ScannedMediaJson = Map<String, Object?>;

String _scannedResourceStatus(ScannedMediaJson response) {
  final direct = response['scan_status'];
  if (direct is String) return direct;
  final attachment = response['attachment'];
  if (attachment is Map && attachment['scan_status'] is String) {
    return attachment['scan_status']! as String;
  }
  return 'pending';
}

void _throwForScannedResourceStatus(String status) {
  if (status != 'rejected' && status != 'infected' && status != 'failed') {
    return;
  }
  throw KaedeException(
    code: 'MEDIA_PROCESSING_REJECTED',
    message: status == 'failed'
        ? 'The server could not process this media. Try another file or try again later.'
        : 'The selected media did not pass media safety processing. Choose another file.',
    status: 422,
  );
}

/// Completes the API's ticket/upload/scan/commit lifecycle without creating a
/// duplicate when the first commit finishes synchronously.
///
/// A pending commit nests its scan state under `attachment`, while a completed
/// commit is the newly bound application media, webhook, or soundboard
/// resource. Keeping that protocol handling here gives every mobile surface
/// the same bounded retry behavior.
Future<T> completeScannedMediaResource<T>({
  required Future<ScannedMediaJson> Function() commit,
  required bool Function(ScannedMediaJson json) isComplete,
  required T Function(ScannedMediaJson json) parse,
  Duration pollInterval = const Duration(seconds: 1),
  int maxPollAttempts = 45,
}) async {
  if (maxPollAttempts < 1) {
    throw ArgumentError.value(maxPollAttempts, 'maxPollAttempts');
  }
  for (var attempt = 0; attempt < maxPollAttempts; attempt += 1) {
    final response = await commit();
    if (isComplete(response)) return parse(response);
    _throwForScannedResourceStatus(_scannedResourceStatus(response));
    if (attempt + 1 < maxPollAttempts && pollInterval > Duration.zero) {
      await Future<void>.delayed(pollInterval);
    }
  }

  throw const KaedeException(
    code: 'MEDIA_PROCESSING_TIMEOUT',
    message:
        'Media processing is taking longer than expected. Try again shortly.',
    status: 504,
  );
}
