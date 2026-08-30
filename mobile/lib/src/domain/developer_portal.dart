import 'package:kaede_mobile/src/core/refs.dart';

typedef DeveloperPortalJson = Map<String, Object?>;

String _requiredString(DeveloperPortalJson json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) {
    throw FormatException('Developer resource is missing $key.', json);
  }
  return value;
}

List<String> _strings(Object? value) => value is List
    ? value.whereType<Object>().map((item) => '$item').toList(growable: false)
    : const <String>[];

final class DeveloperTeam {
  const DeveloperTeam({
    required this.ref,
    required this.name,
    required this.personal,
    required this.role,
  });

  factory DeveloperTeam.fromJson(DeveloperPortalJson json) => DeveloperTeam(
        ref: EntityRef.fromJson(json['ref']),
        name: _requiredString(json, 'name'),
        personal: json['personal'] == true,
        role: _requiredString(json, 'role'),
      );

  final EntityRef ref;
  final String name;
  final bool personal;
  final String role;

  bool get canManageMembers =>
      !personal && (role == 'owner' || role == 'administrator');
}

final class DeveloperTeamMember {
  const DeveloperTeamMember({
    required this.ref,
    required this.username,
    required this.displayName,
    required this.role,
  });

  factory DeveloperTeamMember.fromJson(DeveloperPortalJson json) {
    final user = Map<String, Object?>.from(json['user']! as Map);
    final id = _requiredString(user, 'id');
    final domain = _requiredString(user, 'origin_domain');
    return DeveloperTeamMember(
      ref: EntityRef(Snowflake(id), Domain(domain)),
      username: _requiredString(user, 'username'),
      displayName: user['display_name'] as String?,
      role: _requiredString(json, 'role'),
    );
  }

  final EntityRef ref;
  final String username;
  final String? displayName;
  final String role;

  String get label =>
      displayName?.trim().isNotEmpty == true ? displayName!.trim() : username;
}

final class DeveloperApplicationDetail {
  const DeveloperApplicationDetail({
    required this.ref,
    required this.name,
    required this.description,
    required this.iconHash,
    required this.supportUrl,
    required this.privacyUrl,
    required this.status,
    required this.targetPolicy,
    required this.defaultScopes,
    required this.defaultIntents,
    required this.defaultPermissions,
    required this.supportedInstallTypes,
    required this.userInstallScopes,
    required this.userInstallContexts,
    required this.e2eeModes,
    required this.botHandle,
  });

  factory DeveloperApplicationDetail.fromJson(DeveloperPortalJson json) {
    final bot = Map<String, Object?>.from(json['bot_user']! as Map);
    return DeveloperApplicationDetail(
      ref: EntityRef.fromJson(json['ref']),
      name: _requiredString(json, 'name'),
      description: json['description'] as String?,
      iconHash: json['icon_hash'] as String?,
      supportUrl: json['support_url'] as String?,
      privacyUrl: json['privacy_url'] as String?,
      status: _requiredString(json, 'status'),
      targetPolicy: _requiredString(json, 'target_policy'),
      defaultScopes: _strings(json['default_scopes']),
      defaultIntents: _strings(json['default_intents']),
      defaultPermissions: _requiredString(json, 'default_permissions'),
      supportedInstallTypes: _strings(json['supported_install_types']),
      userInstallScopes: _strings(json['user_install_scopes']),
      userInstallContexts: _strings(json['user_install_contexts']),
      e2eeModes: _strings(json['e2ee_modes']),
      botHandle: _requiredString(bot, 'handle'),
    );
  }

  final EntityRef ref;
  final String name;
  final String? description;
  final String? iconHash;
  final String? supportUrl;
  final String? privacyUrl;
  final String status;
  final String targetPolicy;
  final List<String> defaultScopes;
  final List<String> defaultIntents;
  final String defaultPermissions;
  final List<String> supportedInstallTypes;
  final List<String> userInstallScopes;
  final List<String> userInstallContexts;
  final List<String> e2eeModes;
  final String botHandle;
}

final class DeveloperCredential {
  const DeveloperCredential({
    required this.id,
    required this.label,
    required this.tokenHint,
    required this.scopes,
    required this.createdAt,
    required this.lastUsedAt,
    required this.revokedAt,
  });

  factory DeveloperCredential.fromJson(DeveloperPortalJson json) =>
      DeveloperCredential(
        id: _requiredString(json, 'id'),
        label: _requiredString(json, 'label'),
        tokenHint: _requiredString(json, 'token_hint'),
        scopes: _strings(json['scopes']),
        createdAt: DateTime.parse(_requiredString(json, 'created_at')),
        lastUsedAt: json['last_used_at'] == null
            ? null
            : DateTime.parse('${json['last_used_at']}'),
        revokedAt: json['revoked_at'] == null
            ? null
            : DateTime.parse('${json['revoked_at']}'),
      );

  final String id;
  final String label;
  final String tokenHint;
  final List<String> scopes;
  final DateTime createdAt;
  final DateTime? lastUsedAt;
  final DateTime? revokedAt;
}

final class DeveloperWorker {
  const DeveloperWorker({
    required this.id,
    required this.name,
    required this.scopes,
    required this.intents,
    required this.targetDomains,
    required this.sessionLimit,
    required this.revokedAt,
  });

  factory DeveloperWorker.fromJson(DeveloperPortalJson json) => DeveloperWorker(
        id: _requiredString(json, 'id'),
        name: _requiredString(json, 'name'),
        scopes: _strings(json['scopes']),
        intents: _strings(json['intents']),
        targetDomains: _strings(json['target_domains']),
        sessionLimit: (json['session_limit'] as num?)?.toInt() ?? 1,
        revokedAt: json['revoked_at'] == null
            ? null
            : DateTime.parse('${json['revoked_at']}'),
      );

  final String id;
  final String name;
  final List<String> scopes;
  final List<String> intents;
  final List<String> targetDomains;
  final int sessionLimit;
  final DateTime? revokedAt;
}

final class DeveloperInstallTemplate {
  const DeveloperInstallTemplate({
    required this.id,
    required this.slug,
    required this.name,
    required this.description,
    required this.scopes,
    required this.intents,
    required this.permissions,
    required this.e2eeMode,
    required this.active,
    required this.inviteUrl,
  });

  factory DeveloperInstallTemplate.fromJson(DeveloperPortalJson json) =>
      DeveloperInstallTemplate(
        id: _requiredString(json, 'id'),
        slug: _requiredString(json, 'slug'),
        name: _requiredString(json, 'name'),
        description: json['description'] as String?,
        scopes: _strings(json['scopes']),
        intents: _strings(json['intents']),
        permissions: _requiredString(json, 'permissions'),
        e2eeMode: _requiredString(json, 'e2ee_mode'),
        active: json['active'] == true,
        inviteUrl: _requiredString(json, 'invite_url'),
      );

  final String id;
  final String slug;
  final String name;
  final String? description;
  final List<String> scopes;
  final List<String> intents;
  final String permissions;
  final String e2eeMode;
  final bool active;
  final String inviteUrl;
}

final class DeveloperInstallation {
  const DeveloperInstallation({
    required this.id,
    required this.guildRef,
    required this.status,
    required this.scopes,
    required this.intents,
    required this.permissions,
    required this.channelRestrictions,
    required this.e2eeMode,
    required this.grantRevision,
  });

  factory DeveloperInstallation.fromJson(DeveloperPortalJson json) =>
      DeveloperInstallation(
        id: _requiredString(json, 'id'),
        guildRef: EntityRef.fromJson(json['guild_ref']),
        status: _requiredString(json, 'status'),
        scopes: _strings(json['scopes']),
        intents: _strings(json['intents']),
        permissions: _requiredString(json, 'permissions'),
        channelRestrictions: _strings(json['channel_restrictions']),
        e2eeMode: _requiredString(json, 'e2ee_mode'),
        grantRevision: _requiredString(json, 'grant_revision'),
      );

  final String id;
  final EntityRef guildRef;
  final String status;
  final List<String> scopes;
  final List<String> intents;
  final String permissions;
  final List<String> channelRestrictions;
  final String e2eeMode;
  final String grantRevision;
}

final class DeveloperInstanceRule {
  const DeveloperInstanceRule({
    required this.targetDomain,
    required this.effect,
  });

  factory DeveloperInstanceRule.fromJson(DeveloperPortalJson json) =>
      DeveloperInstanceRule(
        targetDomain: _requiredString(json, 'target_domain'),
        effect: _requiredString(json, 'effect'),
      );

  final String targetDomain;
  final String effect;
}
