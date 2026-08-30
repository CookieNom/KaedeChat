import 'package:kaede_mobile/src/core/refs.dart';

typedef ApplicationMediaJson = Map<String, Object?>;

final _applicationEmojiNamePattern = RegExp(r'^[A-Za-z0-9_]{2,32}$');

String _requiredString(ApplicationMediaJson json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) {
    throw FormatException('Application media is missing $key.', json);
  }
  return value;
}

int? _optionalInteger(Object? value) {
  if (value == null) return null;
  if (value is num) return value.toInt();
  return int.tryParse('$value');
}

int _requiredPositiveInteger(ApplicationMediaJson json, String key) {
  final value = _optionalInteger(json[key]);
  if (value == null || value < 1) {
    throw FormatException('Application media has an invalid $key.', json);
  }
  return value;
}

bool _requiredBoolean(ApplicationMediaJson json, String key) {
  final value = json[key];
  if (value is! bool) {
    throw FormatException('Application media has an invalid $key.', json);
  }
  return value;
}

/// The image roles supported by Kaede's application asset API.
enum ApplicationAssetKind {
  icon,
  cover,
  store,
  achievement,
  activity,
  other;

  static ApplicationAssetKind fromWire(Object? value) {
    for (final kind in values) {
      if (kind.name == value) return kind;
    }
    throw FormatException('Unsupported application asset kind.', value);
  }

  String get label => switch (this) {
        icon => 'Icon',
        cover => 'Cover',
        store => 'Store art',
        achievement => 'Achievement',
        activity => 'Activity art',
        other => 'Other',
      };
}

/// An application visible to the signed-in developer through their personal
/// team or one of their shared developer teams.
final class DeveloperApplication {
  const DeveloperApplication({
    required this.ref,
    required this.name,
    required this.description,
    required this.iconHash,
    required this.status,
  });

  factory DeveloperApplication.fromJson(ApplicationMediaJson json) =>
      DeveloperApplication(
        ref: EntityRef.fromJson(json['ref']),
        name: _requiredString(json, 'name'),
        description: json['description'] as String?,
        iconHash: json['icon_hash'] as String?,
        status: _requiredString(json, 'status'),
      );

  final EntityRef ref;
  final String name;
  final String? description;
  final String? iconHash;
  final String status;
}

final class ApplicationAsset {
  const ApplicationAsset({
    required this.id,
    required this.applicationRef,
    required this.kind,
    required this.name,
    required this.mediaHash,
    required this.contentType,
    required this.width,
    required this.height,
    required this.version,
  });

  factory ApplicationAsset.fromJson(ApplicationMediaJson json) {
    final width = _optionalInteger(json['width']);
    final height = _optionalInteger(json['height']);
    if ((width == null) != (height == null) ||
        (width != null && (width < 1 || height! < 1))) {
      throw FormatException('Application asset dimensions are invalid.', json);
    }
    return ApplicationAsset(
      id: Snowflake(_requiredString(json, 'id')),
      applicationRef: EntityRef.fromJson(json['application_ref']),
      kind: ApplicationAssetKind.fromWire(json['kind']),
      name: _requiredString(json, 'name'),
      mediaHash: _requiredString(json, 'media_hash'),
      contentType: _requiredString(json, 'content_type'),
      width: width,
      height: height,
      version: _requiredPositiveInteger(json, 'version'),
    );
  }

  final Snowflake id;
  final EntityRef applicationRef;
  final ApplicationAssetKind kind;
  final String name;
  final String mediaHash;
  final String contentType;
  final int? width;
  final int? height;
  final int version;

  String get dimensions =>
      width == null ? 'Original dimensions' : '$width × $height';
}

final class ApplicationEmoji {
  const ApplicationEmoji({
    required this.id,
    required this.applicationRef,
    required this.name,
    required this.mediaHash,
    required this.animated,
    required this.available,
    required this.version,
  });

  factory ApplicationEmoji.fromJson(ApplicationMediaJson json) =>
      ApplicationEmoji(
        id: Snowflake(_requiredString(json, 'id')),
        applicationRef: EntityRef.fromJson(json['application_ref']),
        name: _requiredString(json, 'name'),
        mediaHash: _requiredString(json, 'media_hash'),
        animated: _requiredBoolean(json, 'animated'),
        available: _requiredBoolean(json, 'available'),
        version: _requiredPositiveInteger(json, 'version'),
      );

  final Snowflake id;
  final EntityRef applicationRef;
  final String name;
  final String mediaHash;
  final bool animated;
  final bool available;
  final int version;
}

final class ApplicationAssetDraft {
  const ApplicationAssetDraft({required this.name, required this.kind});

  final String name;
  final ApplicationAssetKind kind;

  String? get validationMessage {
    final cleaned = name.trim();
    if (cleaned.isEmpty) return 'Enter an asset name.';
    if (cleaned.length > 100) {
      return 'Asset names can be at most 100 characters.';
    }
    return null;
  }

  ApplicationMediaJson createPayload(String attachmentId) {
    final validation = validationMessage;
    if (validation != null) throw FormatException(validation, name);
    return <String, Object?>{
      'attachment_id': attachmentId,
      'kind': kind.name,
      'name': name.trim(),
    };
  }

  ApplicationMediaJson get patchPayload {
    final validation = validationMessage;
    if (validation != null) throw FormatException(validation, name);
    return <String, Object?>{'kind': kind.name, 'name': name.trim()};
  }
}

final class ApplicationEmojiDraft {
  const ApplicationEmojiDraft({required this.name});

  final String name;

  String? get validationMessage => applicationEmojiNameValidation(name);

  ApplicationMediaJson createPayload(String attachmentId) {
    final validation = validationMessage;
    if (validation != null) throw FormatException(validation, name);
    return <String, Object?>{
      'attachment_id': attachmentId,
      'name': name.trim(),
    };
  }

  ApplicationMediaJson get patchPayload {
    final validation = validationMessage;
    if (validation != null) throw FormatException(validation, name);
    return <String, Object?>{'name': name.trim()};
  }
}

String? applicationEmojiNameValidation(String value) =>
    _applicationEmojiNamePattern.hasMatch(value.trim())
        ? null
        : 'Emoji names use 2–32 letters, numbers, or underscores.';

String? applicationImageValidation({
  required String filename,
  required String? contentType,
  required int size,
}) {
  if (size < 1) return 'The selected image is empty.';
  if (contentType == null) {
    return 'Choose a PNG, JPEG, GIF, or WebP image.';
  }
  if (filename.trim().isEmpty) return 'Choose an image stored on this device.';
  return null;
}

ApplicationMediaJson applicationMediaTicketPayload({
  required String filename,
  required String contentType,
  required int size,
}) =>
    <String, Object?>{
      'filename': filename,
      'content_type': contentType,
      'size': size,
    };
