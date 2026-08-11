import 'package:kaede_mobile/src/core/refs.dart';

/// Authenticated attachment bytes are served by the attachment's home
/// instance through this path. The API client deliberately builds the origin
/// from the signed-in instance and handles any object-storage redirect.
String attachmentMediaPath(EntityRef attachment) =>
    '/media/${Uri.encodeComponent(attachment.domain.value)}/'
    '${Uri.encodeComponent(attachment.id.value)}/original';

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
