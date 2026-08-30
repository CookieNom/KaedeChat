import 'package:kaede_mobile/src/core/refs.dart';

const userApplicationScopes = <String>[
  'applications.commands',
  'interactions.respond',
];
const userApplicationIntents = <String>['interactions'];
const userApplicationContexts = <String>[
  'guild',
  'private_channel',
  'bot_dm',
];
const suspendedUserApplicationExplanation =
    'This application is suspended. Its commands are unavailable, and access settings cannot be changed until it is restored. You can still revoke it.';

final class ApplicationInstallInvite {
  const ApplicationInstallInvite({
    required this.application,
    required this.applicationName,
    this.applicationDescription,
    this.applicationIconHash,
    required this.botUser,
    required this.botHandle,
    this.supportUrl,
    this.privacyUrl,
    required this.supportedInstallTypes,
    required this.userInstallScopes,
    required this.userInstallContexts,
    required this.templateSlug,
    required this.templateName,
    this.templateDescription,
    required this.scopes,
    required this.intents,
    required this.permissions,
    required this.e2eeMode,
  });

  factory ApplicationInstallInvite.fromJson(Map<String, Object?> json) {
    final application = _object(json['application']);
    final template = _object(json['template']);
    final bot = _object(application['bot_user']);
    final applicationRef = EntityRef.fromJson(
      application['ref'] ??
          '${application['id']}@${application['origin_domain']}',
    );
    final botRef = EntityRef.fromJson(
      bot['ref'] ?? '${bot['id']}@${bot['origin_domain']}',
    );
    final name = '${application['name'] ?? ''}'.trim();
    final slug = '${template['slug'] ?? ''}'.trim();
    if (name.isEmpty || slug.isEmpty) {
      throw const FormatException('The application invitation is incomplete.');
    }
    return ApplicationInstallInvite(
      application: applicationRef,
      applicationName: name,
      applicationDescription: _optionalText(application['description']),
      applicationIconHash: _optionalText(application['icon_hash']),
      botUser: botRef,
      botHandle: _optionalText(bot['handle']) ??
          '${bot['username'] ?? 'bot'}@${botRef.domain.value}',
      supportUrl: _optionalText(application['support_url']),
      privacyUrl: _optionalText(application['privacy_url']),
      supportedInstallTypes: _strings(application['supported_install_types']),
      userInstallScopes: _strings(application['user_install_scopes']),
      userInstallContexts: _strings(application['user_install_contexts']),
      templateSlug: slug,
      templateName: _optionalText(template['name']) ?? name,
      templateDescription: _optionalText(template['description']),
      scopes: _strings(template['scopes']),
      intents: _strings(template['intents']),
      permissions: '${template['permissions'] ?? '0'}',
      e2eeMode: '${template['e2ee_mode'] ?? 'none'}',
    );
  }

  final EntityRef application;
  final String applicationName;
  final String? applicationDescription;
  final String? applicationIconHash;
  final EntityRef botUser;
  final String botHandle;
  final String? supportUrl;
  final String? privacyUrl;
  final List<String> supportedInstallTypes;
  final List<String> userInstallScopes;
  final List<String> userInstallContexts;
  final String templateSlug;
  final String templateName;
  final String? templateDescription;
  final List<String> scopes;
  final List<String> intents;
  final String permissions;
  final String e2eeMode;

  bool get supportsGuildInstall =>
      supportedInstallTypes.contains('guild_install') &&
      templateSlug != 'user-install';
  bool get supportsUserInstall =>
      supportedInstallTypes.contains('user_install');
}

final class UserApplicationInstallation {
  const UserApplicationInstallation({
    required this.id,
    required this.application,
    required this.applicationName,
    this.applicationDescription,
    this.applicationIconHash,
    required this.botUser,
    required this.user,
    required this.scopes,
    required this.intents,
    required this.contexts,
    required this.e2eeParticipantCapable,
    required this.grantRevision,
    required this.status,
    this.revokedAt,
    this.createdAt,
    this.updatedAt,
  });

  factory UserApplicationInstallation.fromJson(Map<String, Object?> json) =>
      UserApplicationInstallation(
        id: '${json['id']}',
        application: EntityRef.fromJson(json['application_ref']),
        applicationName: '${json['application_name'] ?? 'App'}',
        applicationDescription: json['application_description'] is String
            ? json['application_description']! as String
            : null,
        applicationIconHash: json['application_icon_hash'] is String
            ? json['application_icon_hash']! as String
            : null,
        botUser: EntityRef.fromJson(json['bot_user_ref']),
        user: EntityRef.fromJson(json['user_ref']),
        scopes: (json['scopes'] as List? ?? const <Object>[])
            .map((item) => '$item')
            .toList(growable: false),
        intents: (json['intents'] as List? ?? const <Object>[])
            .map((item) => '$item')
            .toList(growable: false),
        contexts: (json['contexts'] as List? ?? const <Object>[])
            .map((item) => '$item')
            .toList(growable: false),
        e2eeParticipantCapable: json['e2ee_participant_capable'] == true,
        grantRevision: '${json['grant_revision'] ?? '1'}',
        status: '${json['status'] ?? 'active'}',
        revokedAt: _dateTime(json['revoked_at']),
        createdAt: _dateTime(json['created_at']),
        updatedAt: _dateTime(json['updated_at']),
      );

  final String id;
  final EntityRef application;
  final String applicationName;
  final String? applicationDescription;
  final String? applicationIconHash;
  final EntityRef botUser;
  final EntityRef user;
  final List<String> scopes;
  final List<String> intents;
  final List<String> contexts;
  final bool e2eeParticipantCapable;
  final String grantRevision;
  final String status;
  final DateTime? revokedAt;
  final DateTime? createdAt;
  final DateTime? updatedAt;

  bool get isActive => status == 'active';
  bool get isSuspended => status == 'suspended';
  bool get grantsEditable => isActive;

  String? get unavailableReason => switch (status) {
        'suspended' => suspendedUserApplicationExplanation,
        'revoked' =>
          'This application installation was revoked. Its commands and access settings are unavailable.',
        _ => null,
      };

  bool get supportsEncryptedPrivateConversation =>
      isActive &&
      e2eeParticipantCapable &&
      (contexts.contains('private_channel') || contexts.contains('bot_dm'));
}

DateTime? _dateTime(Object? value) =>
    value is String ? DateTime.tryParse(value) : null;

Map<String, Object?> _object(Object? value) => value is Map
    ? value.map((key, item) => MapEntry('$key', item))
    : throw const FormatException('The application invitation is incomplete.');

String? _optionalText(Object? value) {
  final text = value is String ? value.trim() : '';
  return text.isEmpty ? null : text;
}

List<String> _strings(Object? value) => (value as List? ?? const <Object>[])
    .map((item) => '$item')
    .toList(growable: false);

Map<String, Object?> userApplicationGrantData({
  required List<String> scopes,
  required List<String> contexts,
  List<String> intents = userApplicationIntents,
}) =>
    <String, Object?>{
      'scopes': scopes,
      'contexts': contexts,
      'intents': intents,
    };
