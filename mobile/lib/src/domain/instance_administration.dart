import 'package:kaede_mobile/src/core/refs.dart';

typedef AdministrationJson = Map<String, Object?>;

String _requiredString(AdministrationJson json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) {
    throw FormatException('Administration resource is missing $key.', json);
  }
  return value;
}

List<String> _strings(Object? value) => value is List
    ? value.whereType<Object>().map((item) => '$item').toList(growable: false)
    : const <String>[];

final class AdministrationIdentity {
  const AdministrationIdentity({
    required this.userRef,
    required this.username,
    required this.roles,
    required this.capabilities,
  });

  factory AdministrationIdentity.fromJson(AdministrationJson json) {
    final user = Map<String, Object?>.from(json['user']! as Map);
    return AdministrationIdentity(
      userRef: EntityRef(
        Snowflake(_requiredString(user, 'id')),
        Domain(_requiredString(user, 'origin_domain')),
      ),
      username: _requiredString(user, 'username'),
      roles: _strings(json['roles']).toSet(),
      capabilities: _strings(json['capabilities']).toSet(),
    );
  }

  final EntityRef userRef;
  final String username;
  final Set<String> roles;
  final Set<String> capabilities;

  bool can(String capability) =>
      capabilities.contains('*') || capabilities.contains(capability);
}

final class AdministrationUser {
  const AdministrationUser({
    required this.ref,
    required this.username,
    required this.displayName,
    required this.accountType,
    required this.disabledAt,
    required this.suspendedUntil,
    required this.ageAssuranceState,
  });

  factory AdministrationUser.fromJson(AdministrationJson json) =>
      AdministrationUser(
        ref: EntityRef(
          Snowflake(_requiredString(json, 'id')),
          Domain(_requiredString(json, 'origin_domain')),
        ),
        username: _requiredString(json, 'username'),
        displayName: json['display_name'] as String?,
        accountType: '${json['account_type'] ?? 'human'}',
        disabledAt: _date(json['disabled_at']),
        suspendedUntil: _date(json['suspended_until']),
        ageAssuranceState: '${json['age_assurance_state'] ?? 'unknown'}',
      );

  final EntityRef ref;
  final String username;
  final String? displayName;
  final String accountType;
  final DateTime? disabledAt;
  final DateTime? suspendedUntil;
  final String ageAssuranceState;

  String get label =>
      displayName?.trim().isNotEmpty == true ? displayName!.trim() : username;
  bool get restricted =>
      disabledAt != null ||
      (suspendedUntil?.isAfter(DateTime.now().toUtc()) ?? false);
}

final class AdministrationApplication {
  const AdministrationApplication({
    required this.ref,
    required this.name,
    required this.status,
    required this.teamRef,
    required this.stateAuthority,
    required this.canManageState,
    required this.updatedAt,
  });

  factory AdministrationApplication.fromJson(AdministrationJson json) =>
      AdministrationApplication(
        ref: EntityRef.fromJson(json['ref']),
        name: _requiredString(json, 'name'),
        status: _requiredString(json, 'status'),
        teamRef: EntityRef.fromJson(json['team_ref']),
        stateAuthority: Domain(_requiredString(json, 'state_authority')),
        canManageState: json['can_manage_state'] == true,
        updatedAt: DateTime.parse(_requiredString(json, 'updated_at')),
      );

  final EntityRef ref;
  final String name;
  final String status;
  final EntityRef teamRef;
  final Domain stateAuthority;
  final bool canManageState;
  final DateTime updatedAt;
}

final class AdministrationReport {
  const AdministrationReport({
    required this.id,
    required this.source,
    required this.severity,
    required this.targetType,
    required this.targetRef,
    required this.category,
    required this.description,
    required this.status,
    required this.resolution,
    required this.subjectRef,
    required this.reporterRef,
    required this.evidence,
    required this.encryptionMode,
    required this.createdAt,
  });

  factory AdministrationReport.fromJson(AdministrationJson json) {
    final subject = json['subject_user'];
    final reporter = json['reporter_user'];
    return AdministrationReport(
      id: _requiredString(json, 'id'),
      source: '${json['source'] ?? 'user'}',
      severity: '${json['severity'] ?? 'normal'}',
      targetType: _requiredString(json, 'target_type'),
      targetRef: _requiredString(json, 'target_ref'),
      category: _requiredString(json, 'category'),
      description: json['description'] as String?,
      status: _requiredString(json, 'status'),
      resolution: json['resolution'] as String?,
      subjectRef: subject is Map && subject['ref'] != null
          ? '${subject['ref']}'
          : json['subject_ref'] as String?,
      reporterRef: reporter is Map && reporter['ref'] != null
          ? '${reporter['ref']}'
          : json['reporter_ref'] as String?,
      evidence: json['evidence'] is Map
          ? Map<String, Object?>.from(json['evidence']! as Map)
          : const <String, Object?>{},
      encryptionMode: json['encryption_mode'] as String?,
      createdAt: DateTime.parse(_requiredString(json, 'created_at')),
    );
  }

  final String id;
  final String source;
  final String severity;
  final String targetType;
  final String targetRef;
  final String category;
  final String? description;
  final String status;
  final String? resolution;
  final String? subjectRef;
  final String? reporterRef;
  final Map<String, Object?> evidence;
  final String? encryptionMode;
  final DateTime createdAt;

  List<AdministrationReportAttachment> get attachments {
    final result = <AdministrationReportAttachment>[];
    final rows = evidence['attachments'];
    if (rows is List) {
      for (final row in rows) {
        if (row is! Map) continue;
        final parsed = AdministrationReportAttachment.tryFromJson(
          Map<String, Object?>.from(row),
        );
        if (parsed != null && !result.any((item) => item.ref == parsed.ref)) {
          result.add(parsed);
        }
      }
    }
    final focused = evidence['attachment_ref'] ??
        (targetType == 'attachment' ? targetRef : null);
    if (focused is String &&
        focused.isNotEmpty &&
        !result.any((item) => item.ref == focused)) {
      final parsed = AdministrationReportAttachment.tryFromJson(
        <String, Object?>{
          'attachment_ref': focused,
          'uploader_ref': evidence['uploader_ref'],
          'filename': evidence['filename'],
          'content_type': evidence['content_type'],
          'size': evidence['size'],
          'encryption_mode': evidence['attachment_encryption_mode'],
        },
      );
      if (parsed != null) result.add(parsed);
    }
    return List.unmodifiable(result);
  }

  bool isDisclosed(AdministrationReportAttachment attachment) =>
      encryptionMode == 'e2ee_user_disclosed' &&
      evidence['attachment_ref'] == attachment.ref &&
      evidence['disclosed_attachment_ref'] is String;

  String? attachmentContentType(AdministrationReportAttachment attachment) {
    final value = isDisclosed(attachment)
        ? evidence['disclosed_content_type']
        : attachment.contentType;
    return value is String && value.isNotEmpty ? value : null;
  }

  String attachmentFilename(AdministrationReportAttachment attachment) {
    final disclosed = evidence['disclosed_filename'];
    final value = isDisclosed(attachment) && disclosed is String
        ? disclosed
        : attachment.filename;
    return value?.trim().isNotEmpty == true
        ? value!.trim()
        : 'reported-attachment-${attachment.ref.replaceAll('@', '-')}';
  }

  String attachmentPreviewRef(AdministrationReportAttachment attachment) {
    final disclosed = evidence['disclosed_attachment_ref'];
    return isDisclosed(attachment) && disclosed is String
        ? disclosed
        : attachment.ref;
  }

  String? attachmentRestriction(
    AdministrationReportAttachment attachment,
    Domain localDomain,
  ) {
    final previewRef = _entityRefOrNull(attachmentPreviewRef(attachment));
    if (previewRef == null || previewRef.domain != localDomain) {
      return 'Remote evidence must be reviewed by its authority instance.';
    }
    if (encryptionMode == 'e2ee_metadata') {
      return 'End-to-end encrypted content was not disclosed to Trust & Safety.';
    }
    if (attachmentContentType(attachment) == null) {
      return 'This evidence has no verified content type.';
    }
    if (isDisclosed(attachment) &&
        evidence['disclosed_attachment_scan_status'] != 'clean') {
      return 'Reporter-disclosed evidence is unavailable until its safety scan is clean.';
    }
    if (!isDisclosed(attachment) && attachment.encryptionMode == 'e2ee') {
      return 'This attachment remains end-to-end encrypted.';
    }
    return null;
  }

  bool canPreview(
      AdministrationReportAttachment attachment, Domain localDomain) {
    if (attachmentRestriction(attachment, localDomain) != null) return false;
    final contentType = attachmentContentType(attachment)!;
    return contentType.startsWith('image/') ||
        contentType.startsWith('video/') ||
        contentType.startsWith('audio/');
  }
}

final class AdministrationReportAttachment {
  const AdministrationReportAttachment({
    required this.ref,
    required this.uploaderRef,
    required this.filename,
    required this.contentType,
    required this.size,
    required this.encryptionMode,
  });

  static AdministrationReportAttachment? tryFromJson(
    AdministrationJson json,
  ) {
    final ref = json['attachment_ref'];
    if (ref is! String || _entityRefOrNull(ref) == null) return null;
    return AdministrationReportAttachment(
      ref: ref,
      uploaderRef: json['uploader_ref'] as String?,
      filename: json['filename'] as String?,
      contentType: json['content_type'] as String?,
      size: switch (json['size']) {
        final int value => value,
        final num value => value.toInt(),
        _ => null,
      },
      encryptionMode: json['encryption_mode'] as String?,
    );
  }

  final String ref;
  final String? uploaderRef;
  final String? filename;
  final String? contentType;
  final int? size;
  final String? encryptionMode;
}

final class AdministrationInstanceBlock {
  const AdministrationInstanceBlock({
    required this.domain,
    required this.level,
    required this.includeSubdomains,
    required this.reason,
  });

  factory AdministrationInstanceBlock.fromJson(AdministrationJson json) =>
      AdministrationInstanceBlock(
        domain: _requiredString(json, 'domain'),
        level: _requiredString(json, 'level'),
        includeSubdomains: json['include_subdomains'] == true,
        reason: json['reason'] as String?,
      );

  final String domain;
  final String level;
  final bool includeSubdomains;
  final String? reason;
}

final class AdministrationOperator {
  const AdministrationOperator({
    required this.id,
    required this.role,
    required this.userRef,
    required this.username,
    required this.displayName,
  });

  factory AdministrationOperator.fromJson(AdministrationJson json) {
    final user = Map<String, Object?>.from(json['user']! as Map);
    return AdministrationOperator(
      id: _requiredString(json, 'id'),
      role: _requiredString(json, 'role'),
      userRef: EntityRef(
        Snowflake(_requiredString(user, 'id')),
        Domain(_requiredString(user, 'origin_domain')),
      ),
      username: _requiredString(user, 'username'),
      displayName: user['display_name'] as String?,
    );
  }

  final String id;
  final String role;
  final EntityRef userRef;
  final String username;
  final String? displayName;
}

final class AdministrationAuditEvent {
  const AdministrationAuditEvent({
    required this.id,
    required this.actorRef,
    required this.actorKind,
    required this.action,
    required this.targetType,
    required this.targetRef,
    required this.detail,
    required this.createdAt,
  });

  factory AdministrationAuditEvent.fromJson(AdministrationJson json) =>
      AdministrationAuditEvent(
        id: _requiredString(json, 'id'),
        actorRef: json['actor_ref'] as String?,
        actorKind: _requiredString(json, 'actor_kind'),
        action: _requiredString(json, 'action'),
        targetType: _requiredString(json, 'target_type'),
        targetRef: _requiredString(json, 'target_ref'),
        detail: json['metadata'] is Map
            ? Map<String, Object?>.from(json['metadata']! as Map)
            : const <String, Object?>{},
        createdAt: DateTime.parse(_requiredString(json, 'created_at')),
      );

  final String id;
  final String? actorRef;
  final String actorKind;
  final String action;
  final String targetType;
  final String targetRef;
  final Map<String, Object?> detail;
  final DateTime createdAt;
}

DateTime? _date(Object? value) =>
    value == null ? null : DateTime.tryParse('$value');

EntityRef? _entityRefOrNull(String value) {
  try {
    return EntityRef.parse(value);
  } on FormatException {
    return null;
  }
}
