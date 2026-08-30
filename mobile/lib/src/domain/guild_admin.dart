import 'package:kaede_mobile/src/core/network_json.dart';
import 'package:kaede_mobile/src/core/refs.dart';

typedef GuildAdminJson = Map<String, Object?>;

/// Authorized soundboard capabilities are delivered only to occupants of the
/// active voice room under this generated gateway operation.
const soundboardGatewayDispatch = 'VOICE_CHANNEL_EFFECT_SEND';

List<GuildAdminJson> _objects(Object? value) => strictNetworkObjectList(
      value,
      label: 'Guild administration object array',
    );

List<String> _strings(Object? value) => value is List
    ? value.map((item) => '$item').toList(growable: false)
    : const [];

int _integer(Object? value, [int fallback = 0]) =>
    value is num ? value.toInt() : int.tryParse('$value') ?? fallback;

double _number(Object? value, [double fallback = 0]) =>
    value is num ? value.toDouble() : double.tryParse('$value') ?? fallback;

bool _boolean(Object? value, [bool fallback = false]) =>
    value is bool ? value : fallback;

EntityRef _ref(Object? value, {required Domain fallbackDomain}) =>
    EntityRef.fromJson(value, localDomain: fallbackDomain);

EntityRef? _optionalRefPair(
  GuildAdminJson json, {
  required String idKey,
  required String domainKey,
}) {
  final id = json[idKey];
  final domain = json[domainKey];
  if (id == null || domain == null) return null;
  try {
    return EntityRef(Snowflake('$id'), Domain('$domain'));
  } on FormatException {
    return null;
  }
}

/// Returns the federated user reference that created a guild expression.
///
/// Emoji and sticker payloads use Discord's `creator_*` spelling while
/// soundboard payloads historically used `created_by_*`. Accepting both at
/// this boundary keeps authorization checks exact without making each client
/// surface understand the wire-format difference.
EntityRef? guildExpressionCreatorRef(GuildAdminJson expression) =>
    _optionalRefPair(
      expression,
      idKey: 'creator_id',
      domainKey: 'creator_domain',
    ) ??
    _optionalRefPair(
      expression,
      idKey: 'created_by_id',
      domainKey: 'created_by_domain',
    );

bool guildExpressionOwnedBy(
  GuildAdminJson expression,
  EntityRef? currentUserRef,
) {
  if (currentUserRef == null) return false;
  for (final keys in const [
    (id: 'creator_id', domain: 'creator_domain'),
    (id: 'created_by_id', domain: 'created_by_domain'),
  ]) {
    if (_optionalRefPair(
          expression,
          idKey: keys.id,
          domainKey: keys.domain,
        ) ==
        currentUserRef) {
      return true;
    }
  }
  return false;
}

/// Discord-style guild expression ownership policy.
///
/// CREATE_GUILD_EXPRESSIONS allows a member to maintain expressions they
/// created. MANAGE_GUILD_EXPRESSIONS additionally allows maintaining other
/// members' expressions.
bool canModifyGuildExpression({
  required EntityRef? creatorRef,
  required EntityRef? currentUserRef,
  required bool canCreate,
  required bool canManage,
}) =>
    canManage ||
    (canCreate &&
        creatorRef != null &&
        currentUserRef != null &&
        creatorRef == currentUserRef);

final class AutoModTriggerMetadata {
  const AutoModTriggerMetadata({
    this.keywordFilter = const [],
    this.regexPatterns = const [],
    this.presets = const [],
    this.allowList = const [],
    this.mentionTotalLimit,
    this.mentionRaidProtectionEnabled = false,
  });

  factory AutoModTriggerMetadata.fromJson(GuildAdminJson json) =>
      AutoModTriggerMetadata(
        keywordFilter: _strings(json['keyword_filter']),
        regexPatterns: _strings(json['regex_patterns']),
        presets: _strings(json['presets']),
        allowList: _strings(json['allow_list']),
        mentionTotalLimit: json['mention_total_limit'] == null
            ? null
            : _integer(json['mention_total_limit']),
        mentionRaidProtectionEnabled:
            _boolean(json['mention_raid_protection_enabled']),
      );

  final List<String> keywordFilter;
  final List<String> regexPatterns;
  final List<String> presets;
  final List<String> allowList;
  final int? mentionTotalLimit;
  final bool mentionRaidProtectionEnabled;

  GuildAdminJson toJson() => <String, Object?>{
        'keyword_filter': keywordFilter,
        'regex_patterns': regexPatterns,
        'presets': presets,
        'allow_list': allowList,
        'mention_total_limit': mentionTotalLimit,
        'mention_raid_protection_enabled': mentionRaidProtectionEnabled,
      };
}

final class AutoModAction {
  const AutoModAction({
    required this.type,
    this.customMessage,
    this.channelRef,
    this.durationSeconds,
  });

  factory AutoModAction.fromJson(
    GuildAdminJson json, {
    required Domain fallbackDomain,
  }) {
    final metadata = json['metadata'] is Map
        ? Map<String, Object?>.from(json['metadata']! as Map)
        : const <String, Object?>{};
    final rawChannel = metadata['channel_id'];
    return AutoModAction(
      type: '${json['type']}',
      customMessage: metadata['custom_message'] as String?,
      channelRef: rawChannel == null
          ? null
          : _ref(rawChannel, fallbackDomain: fallbackDomain),
      durationSeconds: metadata['duration_seconds'] == null
          ? null
          : _integer(metadata['duration_seconds']),
    );
  }

  final String type;
  final String? customMessage;
  final EntityRef? channelRef;
  final int? durationSeconds;

  GuildAdminJson toJson() => <String, Object?>{
        'type': type,
        if (customMessage?.trim().isNotEmpty == true)
          'custom_message': customMessage!.trim(),
        if (channelRef != null) 'channel_id': channelRef!.wire,
        if (durationSeconds != null) 'duration_seconds': durationSeconds,
      };
}

final class AutoModRule {
  const AutoModRule({
    required this.ref,
    required this.guildRef,
    required this.name,
    required this.eventType,
    required this.triggerType,
    required this.triggerMetadata,
    required this.actions,
    required this.enabled,
    required this.exemptRoles,
    required this.exemptChannels,
    required this.version,
    required this.createdAt,
    required this.updatedAt,
  });

  factory AutoModRule.fromJson(GuildAdminJson json) {
    final domain = Domain('${json['origin_domain']}');
    final guildDomain = Domain('${json['guild_domain']}');
    return AutoModRule(
      ref: EntityRef(Snowflake('${json['id']}'), domain),
      guildRef: EntityRef(Snowflake('${json['guild_id']}'), guildDomain),
      name: '${json['name']}',
      eventType: '${json['event_type']}',
      triggerType: '${json['trigger_type']}',
      triggerMetadata: AutoModTriggerMetadata.fromJson(
        json['trigger_metadata'] is Map
            ? Map<String, Object?>.from(json['trigger_metadata']! as Map)
            : const <String, Object?>{},
      ),
      actions: _objects(json['actions'])
          .map((item) => AutoModAction.fromJson(
                item,
                fallbackDomain: guildDomain,
              ))
          .toList(growable: false),
      enabled: _boolean(json['enabled']),
      exemptRoles: _strings(json['exempt_roles'])
          .map((item) => EntityRef.parse(item, localDomain: guildDomain))
          .toList(growable: false),
      exemptChannels: _strings(json['exempt_channels'])
          .map((item) => EntityRef.parse(item, localDomain: guildDomain))
          .toList(growable: false),
      version: _integer(json['version'], 1),
      createdAt: DateTime.parse('${json['created_at']}').toUtc(),
      updatedAt: DateTime.parse('${json['updated_at']}').toUtc(),
    );
  }

  final EntityRef ref;
  final EntityRef guildRef;
  final String name;
  final String eventType;
  final String triggerType;
  final AutoModTriggerMetadata triggerMetadata;
  final List<AutoModAction> actions;
  final bool enabled;
  final List<EntityRef> exemptRoles;
  final List<EntityRef> exemptChannels;
  final int version;
  final DateTime createdAt;
  final DateTime updatedAt;
}

final class AutoModRuleDraft {
  const AutoModRuleDraft({
    required this.name,
    required this.eventType,
    required this.triggerType,
    required this.triggerMetadata,
    required this.actions,
    required this.enabled,
    this.exemptRoles = const [],
    this.exemptChannels = const [],
  });

  factory AutoModRuleDraft.fromRule(AutoModRule rule) => AutoModRuleDraft(
        name: rule.name,
        eventType: rule.eventType,
        triggerType: rule.triggerType,
        triggerMetadata: rule.triggerMetadata,
        actions: rule.actions,
        enabled: rule.enabled,
        exemptRoles: rule.exemptRoles,
        exemptChannels: rule.exemptChannels,
      );

  final String name;
  final String eventType;
  final String triggerType;
  final AutoModTriggerMetadata triggerMetadata;
  final List<AutoModAction> actions;
  final bool enabled;
  final List<EntityRef> exemptRoles;
  final List<EntityRef> exemptChannels;

  GuildAdminJson toJson() => <String, Object?>{
        'name': name.trim(),
        'event_type': eventType,
        'trigger_type': triggerType,
        'trigger_metadata': triggerMetadata.toJson(),
        'actions': actions.map((action) => action.toJson()).toList(),
        'enabled': enabled,
        'exempt_roles': exemptRoles.map((item) => item.wire).toList(),
        'exempt_channels': exemptChannels.map((item) => item.wire).toList(),
      };
}

final class ModerationFailure {
  const ModerationFailure({
    required this.userRef,
    required this.code,
    required this.message,
  });

  factory ModerationFailure.fromJson(
    GuildAdminJson json, {
    required Domain fallbackDomain,
  }) =>
      ModerationFailure(
        userRef: _ref(json['user_id'], fallbackDomain: fallbackDomain),
        code: '${json['code'] ?? 'MODERATION_FAILED'}',
        message: '${json['message'] ?? 'The member could not be updated.'}',
      );

  final EntityRef userRef;
  final String code;
  final String message;
}

final class PruneResult {
  const PruneResult({
    required this.days,
    required this.pruned,
    required this.prunedUserRefs,
    required this.failures,
  });

  factory PruneResult.fromJson(
    GuildAdminJson json, {
    required Domain fallbackDomain,
  }) =>
      PruneResult(
        days: _integer(json['days']),
        pruned: json['pruned'] == null ? null : _integer(json['pruned']),
        prunedUserRefs: _strings(json['pruned_user_ids'])
            .map((item) => EntityRef.parse(item, localDomain: fallbackDomain))
            .toList(growable: false),
        failures: _objects(json['failed_users'])
            .map((item) => ModerationFailure.fromJson(
                  item,
                  fallbackDomain: fallbackDomain,
                ))
            .toList(growable: false),
      );

  final int days;
  final int? pruned;
  final List<EntityRef> prunedUserRefs;
  final List<ModerationFailure> failures;
}

final class BulkBanResult {
  const BulkBanResult({
    required this.bannedUserRefs,
    required this.failedUserRefs,
    required this.failures,
  });

  factory BulkBanResult.fromJson(
    GuildAdminJson json, {
    required Domain fallbackDomain,
  }) =>
      BulkBanResult(
        bannedUserRefs: _strings(json['banned_users'])
            .map((item) => EntityRef.parse(item, localDomain: fallbackDomain))
            .toList(growable: false),
        failedUserRefs: _strings(json['failed_users'])
            .map((item) => EntityRef.parse(item, localDomain: fallbackDomain))
            .toList(growable: false),
        failures: _objects(json['failed_user_details'])
            .map((item) => ModerationFailure.fromJson(
                  item,
                  fallbackDomain: fallbackDomain,
                ))
            .toList(growable: false),
      );

  final List<EntityRef> bannedUserRefs;
  final List<EntityRef> failedUserRefs;
  final List<ModerationFailure> failures;
}

final class SoundboardSound {
  const SoundboardSound({
    required this.ref,
    required this.guildRef,
    required this.name,
    required this.mediaHash,
    required this.contentType,
    required this.volume,
    required this.available,
    required this.durationMilliseconds,
    required this.version,
    this.creatorRef,
    this.emojiRef,
    this.emojiName,
  });

  factory SoundboardSound.fromJson(GuildAdminJson json) {
    final domain = Domain('${json['origin_domain']}');
    final guildId = json['guild_id'];
    final guildDomain = json['guild_domain'];
    final emojiId = json['emoji_id'];
    final emojiDomain = json['emoji_domain'];
    return SoundboardSound(
      ref: EntityRef(Snowflake('${json['id']}'), domain),
      guildRef: guildId == null || guildDomain == null
          ? null
          : EntityRef(Snowflake('$guildId'), Domain('$guildDomain')),
      name: '${json['name']}',
      mediaHash: '${json['media_hash']}',
      contentType: '${json['content_type']}',
      volume: _number(json['volume'], 1).clamp(0, 1).toDouble(),
      creatorRef: guildExpressionCreatorRef(json),
      emojiRef: emojiId == null || emojiDomain == null
          ? null
          : EntityRef(Snowflake('$emojiId'), Domain('$emojiDomain')),
      emojiName: json['emoji_name'] as String?,
      available: _boolean(json['available'], true),
      durationMilliseconds: _integer(json['duration_ms']),
      version: _integer(json['version'], 1),
    );
  }

  final EntityRef ref;
  final EntityRef? guildRef;
  final String name;
  final String mediaHash;
  final String contentType;
  final double volume;
  final EntityRef? creatorRef;
  final EntityRef? emojiRef;
  final String? emojiName;
  final bool available;
  final int durationMilliseconds;
  final int version;

  String get displayEmoji =>
      emojiName?.trim().isNotEmpty == true ? emojiName! : '♫';
}
