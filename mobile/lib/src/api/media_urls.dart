import 'package:kaede_mobile/src/core/refs.dart';

const _supportedImageUploadTypes = <String>{
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/webp',
};

final _assetHashPattern = RegExp(r'^[0-9a-f]{64}$');
final _linkPreviewMediaPathPattern =
    RegExp(r'^/api/v1/link-previews/media/[0-9a-f]{48}$');
const _assetVariants = <String>{
  'original',
  'thumbnail_128',
  'thumbnail_512',
  'thumbnail_1024',
  'poster',
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
  String? privateMediaUrl,
}) {
  final privatePath = privateInteractionAttachmentMediaPath(
    attachment,
    privateMediaUrl,
  );
  if (privatePath != null) return privatePath;
  final historyPath = dmHistoryAttachmentMediaPath(
    attachment,
    'original',
    historyMediaUrl,
  );
  if (historyPath != null) return historyPath;
  return '/media/${Uri.encodeComponent(attachment.domain.value)}/'
      '${Uri.encodeComponent(attachment.id.value)}/original';
}

/// Accepts only the signed, identity-bound DM-history route projected by the
/// account authority. Federation data cannot turn an authenticated media
/// request into an arbitrary same-origin GET.
String? dmHistoryAttachmentMediaPath(
  EntityRef attachment,
  String variant,
  String? value,
) {
  if (!_assetVariants.contains(variant) || !isSafeSameOriginMediaPath(value)) {
    return null;
  }
  final uri = Uri.tryParse(value!);
  if (uri == null || uri.fragment.isNotEmpty) return null;
  final match = RegExp(
    r'^/api/v1/dms/([^/]+)/history-media/([^/]+)/([^/]+)/(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$',
  ).firstMatch(uri.path);
  if (match == null || match.group(4) != variant) return null;
  try {
    final conversation = EntityRef.parse(match.group(1)!);
    final message = EntityRef.parse(match.group(2)!);
    final projectedAttachment = EntityRef.parse(match.group(3)!);
    if (conversation.wire != match.group(1) ||
        message.wire != match.group(2) ||
        projectedAttachment.wire != match.group(3) ||
        message.domain != projectedAttachment.domain ||
        projectedAttachment != attachment) {
      return null;
    }
  } on FormatException {
    return null;
  }
  final expires = uri.queryParametersAll['expires'] ?? const <String>[];
  final tokens = uri.queryParametersAll['token'] ?? const <String>[];
  if (uri.queryParametersAll.keys.any(
        (key) => key != 'expires' && key != 'token',
      ) ||
      expires.length != 1 ||
      !RegExp(r'^[1-9][0-9]*$').hasMatch(expires.single) ||
      tokens.length != 1 ||
      !RegExp(r'^[A-Za-z0-9_-]{40,48}$').hasMatch(tokens.single)) {
    return null;
  }
  return value;
}

/// Resolves the user-authenticated path projected for an ephemeral bot file.
/// Every path identity must match the attachment and a single response
/// authority before it can override the ordinary media route.
String? privateInteractionAttachmentMediaPath(
  EntityRef attachment,
  String? value, {
  String variant = 'original',
}) {
  if (!_assetVariants.contains(variant) || !isSafeSameOriginMediaPath(value)) {
    return null;
  }
  final match = RegExp(
    r'^/api/v1/interactions/([^/]+)/responses/([^/]+)/attachments/([^/]+)$',
  ).firstMatch(value!);
  if (match == null) return null;
  try {
    final interaction = EntityRef.parse(match.group(1)!);
    final response = EntityRef.parse(match.group(2)!);
    final projectedAttachment = EntityRef.parse(match.group(3)!);
    if (interaction.wire != match.group(1) ||
        response.wire != match.group(2) ||
        projectedAttachment.wire != match.group(3) ||
        interaction.domain != response.domain ||
        response.domain != projectedAttachment.domain ||
        projectedAttachment != attachment) {
      return null;
    }
  } on FormatException {
    return null;
  }
  return '$value/$variant';
}

/// Only local absolute paths may override the canonical media route. This
/// keeps credentials on the signed-in Kaede API even for hostile federation
/// payloads that try to supply a cross-host history URL.
bool isSafeSameOriginMediaPath(String? value) =>
    value != null &&
    value.startsWith('/') &&
    !value.startsWith('//') &&
    !value.contains(r'\');

/// Resolves only the opaque, same-origin media capability returned by the
/// link-preview API. Authenticated image widgets must not accept an arbitrary
/// URL here or they could forward the account bearer token off-instance.
Uri? linkPreviewMediaUri(Domain instance, Object? value) {
  if (value is! String || !_linkPreviewMediaPathPattern.hasMatch(value)) {
    return null;
  }
  return Uri.https(instance.value, value);
}

/// The upload-status endpoint is hosted by the signed-in API and accepts the
/// attachment snowflake, not its composite federation reference.
String attachmentStatusPath(EntityRef attachment) =>
    '/api/v1/attachments/${Uri.encodeComponent(attachment.id.value)}';

/// The upload-status endpoint is uploader-only and exists on the signed-in
/// user's home. Remote/federated attachments must update through Gateway;
/// polling their bare snowflake locally is both ambiguous and incorrect.
bool canPollAttachmentStatus({
  required EntityRef attachment,
  required EntityRef messageAuthor,
  required EntityRef? currentUser,
}) =>
    currentUser != null &&
    messageAuthor == currentUser &&
    attachment.domain == currentUser.domain;

/// Constructs the public, immutable media URL served by an entity's home
/// instance. Hashes are validated before becoming part of a URL so a remote
/// payload cannot inject a path or query string.
Uri? publicAssetUri(
  Domain domain,
  String? hash, {
  String variant = 'original',
}) {
  if (hash == null || !_assetHashPattern.hasMatch(hash)) return null;
  if (!_assetVariants.contains(variant)) {
    return null;
  }
  return Uri.https(
    domain.value,
    '/media/assets/$hash/$variant',
    const <String, String>{'v': '2'},
  );
}

/// Public custom emoji assets always come from the emoji's authority, so a
/// federated bot-authored component renders the same asset on every instance.
Uri publicEmojiUri(EntityRef emoji, {String variant = 'thumbnail_128'}) {
  final safeVariant =
      const {'original', 'thumbnail_128', 'thumbnail_512'}.contains(variant)
          ? variant
          : 'thumbnail_128';
  return Uri.https(
    emoji.domain.value,
    '/media/emojis/${Uri.encodeComponent(emoji.id.value)}/$safeVariant',
  );
}
