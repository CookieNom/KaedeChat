import 'package:kaede_mobile/src/core/refs.dart';

const _supportedImageUploadTypes = <String>{
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/webp',
};

/// Uses the picker-reported MIME type when it is supported, then falls back to
/// a known extension. Unknown formats must not be mislabeled as JPEG because
/// media processing validates the uploaded bytes.
String? imageUploadContentType(String filename, {String? reportedType}) {
  final normalized = reportedType?.trim().toLowerCase();
  if (normalized != null && _supportedImageUploadTypes.contains(normalized)) {
    return normalized;
  }
  final lower = filename.toLowerCase();
  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return 'image/jpeg';
  if (lower.endsWith('.gif')) return 'image/gif';
  if (lower.endsWith('.webp')) return 'image/webp';
  return null;
}

/// Authenticated attachment bytes are served by the attachment's home
/// instance through this path. The API client deliberately builds the origin
/// from the signed-in instance and handles any object-storage redirect.
String attachmentMediaPath(
  EntityRef attachment, {
  String? historyMediaUrl,
}) {
  if (isSafeSameOriginMediaPath(historyMediaUrl)) return historyMediaUrl!;
  return '/media/${Uri.encodeComponent(attachment.domain.value)}/'
      '${Uri.encodeComponent(attachment.id.value)}/original';
}

/// Only local absolute paths may override the canonical media route. This
/// keeps credentials on the signed-in Kaede API even for hostile federation
/// payloads that try to supply a cross-host history URL.
bool isSafeSameOriginMediaPath(String? value) =>
    value != null &&
    value.startsWith('/') &&
    !value.startsWith('//') &&
    !value.contains(r'\');

/// The upload-status endpoint is hosted by the signed-in API and accepts the
/// attachment snowflake, not its composite federation reference.
String attachmentStatusPath(EntityRef attachment) =>
    '/api/v1/attachments/${Uri.encodeComponent(attachment.id.value)}';

/// Constructs the public, immutable media URL served by an entity's home
/// instance. Hashes are validated before becoming part of a URL so a remote
/// payload cannot inject a path or query string.
Uri? publicAssetUri(
  Domain domain,
  String? hash, {
  String variant = 'original',
}) {
  if (hash == null || !RegExp(r'^[0-9a-f]{64}$').hasMatch(hash)) return null;
  if (!const <String>{
    'original',
    'thumbnail_128',
    'thumbnail_512',
    'thumbnail_1024',
    'poster',
  }.contains(variant)) {
    return null;
  }
  return Uri.https(
    domain.value,
    '/media/assets/$hash/$variant',
    const <String, String>{'v': '2'},
  );
}
