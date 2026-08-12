import 'package:kaede_mobile/src/core/refs.dart';

typedef Json = Map<String, Object?>;

String? _string(Object? value) => value is String ? value : null;
int _integer(Object? value, [int fallback = 0]) =>
    value is num ? value.toInt() : int.tryParse('$value') ?? fallback;
int? _nullableInteger(Object? value) => value == null
    ? null
    : value is num
        ? value.toInt()
        : int.tryParse('$value');
bool _boolean(Object? value, [bool fallback = false]) =>
    value is bool ? value : fallback;
EntityRef? _entityRefOrNull(Object? value) {
  if (value == null) return null;
  try {
    return EntityRef.fromJson(value);
  } on FormatException {
    return null;
  }
}

List<Json> _objects(Object? value) => value is List
    ? value
        .whereType<Map<Object?, Object?>>()
        .map((item) => item.map((key, value) => MapEntry('$key', value)))
        .toList()
    : const <Json>[];

enum PresenceStatus { online, idle, dnd, invisible, offline }

enum ChannelType { text, dm, voice, unknown, category, announcement }

enum RelationshipType { friend, pendingIn, pendingOut, blocked }

ChannelType channelType(int value) => switch (value) {
      0 => ChannelType.text,
      1 => ChannelType.dm,
      2 => ChannelType.voice,
      4 => ChannelType.category,
      5 => ChannelType.announcement,
      _ => ChannelType.unknown,
    };

final class KaedeUser {
  const KaedeUser({
    required this.ref,
    required this.username,
    required this.handle,
    this.displayName,
    this.avatarHash,
    this.bannerHash,
    this.bio,
    this.customStatus,
    this.email,
    this.emailVerified = false,
    this.mfaEnabled = false,
    this.profileResolved = true,
    this.presence = PresenceStatus.offline,
  });

  factory KaedeUser.fromJson(Json json) {
    final domain = Domain(json['origin_domain']! as String);
    return KaedeUser(
      ref: EntityRef(Snowflake(json['id']! as String), domain),
      username: json['username']! as String,
      handle: _string(json['handle']) ?? '@${json['username']}@${domain.value}',
      displayName: _string(json['display_name']),
      avatarHash: _string(json['avatar_hash']),
      bannerHash: _string(json['banner_hash']),
      bio: _string(json['bio']),
      customStatus: _string(json['custom_status']),
      email: _string(json['email']),
      emailVerified: _boolean(json['email_verified']),
      mfaEnabled: _boolean(json['mfa_enabled']),
      profileResolved: _boolean(json['profile_resolved'], true),
      presence: PresenceStatus.values.firstWhere(
        (value) => value.name == _string(json['presence']),
        orElse: () => PresenceStatus.offline,
      ),
    );
  }

  final EntityRef ref;
  final String username;
  final String handle;
  final String? displayName;
  final String? avatarHash;
  final String? bannerHash;
  final String? bio;
  final String? customStatus;
  final String? email;
  final bool emailVerified;
  final bool mfaEnabled;
  final bool profileResolved;
  final PresenceStatus presence;

  String get name => profileResolved
      ? (displayName?.trim().isNotEmpty == true ? displayName! : username)
      : 'Remote user · ${ref.domain.value}';

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'username': username,
        'handle': handle,
        'display_name': displayName,
        'avatar_hash': avatarHash,
        'banner_hash': bannerHash,
        'bio': bio,
        'custom_status': customStatus,
        'email': email,
        'email_verified': emailVerified,
        'mfa_enabled': mfaEnabled,
        'profile_resolved': profileResolved,
        'presence': presence.name,
      };
}

final class KaedeChannel {
  const KaedeChannel({
    required this.ref,
    required this.type,
    required this.position,
    required this.permissions,
    this.guildRef,
    this.name,
    this.topic,
    this.parentRef,
    this.lastMessageRef,
    this.recipients = const <KaedeUser>[],
    this.slowModeSeconds = 0,
    this.permissionsSynced = false,
    this.historyTruncated = false,
    this.historyRetention,
    this.historyRemoteAvailable = false,
    this.oldestAvailableMessageRef,
    this.historyDegradedCode,
    this.version,
  });

  factory KaedeChannel.fromJson(Json json) {
    final domain = Domain(json['origin_domain']! as String);
    final guildId = _string(json['guild_id']);
    final guildDomain = _string(json['guild_domain']);
    final parentId = _string(json['parent_id']);
    final parentDomain = _string(json['parent_domain']);
    final lastId = _string(json['last_message_id']);
    final lastDomain = _string(json['last_message_domain']);
    return KaedeChannel(
      ref: EntityRef(Snowflake(json['id']! as String), domain),
      guildRef: guildId == null || guildDomain == null
          ? null
          : EntityRef(Snowflake(guildId), Domain(guildDomain)),
      type: channelType(_integer(json['type'])),
      name: _string(json['name']),
      topic: _string(json['topic']),
      position: _integer(json['position']),
      parentRef: parentId == null || parentDomain == null
          ? null
          : EntityRef(Snowflake(parentId), Domain(parentDomain)),
      permissions:
          BigInt.tryParse(_string(json['permissions']) ?? '0') ?? BigInt.zero,
      lastMessageRef: lastId == null || lastDomain == null
          ? null
          : EntityRef(Snowflake(lastId), Domain(lastDomain)),
      recipients: _objects(json['recipients']).map(KaedeUser.fromJson).toList(),
      slowModeSeconds: _integer(json['rate_limit_per_user']),
      permissionsSynced: _boolean(json['permissions_synced']),
      historyTruncated: _boolean(json['history_truncated']),
      historyRetention: _string(json['history_retention']),
      historyRemoteAvailable: _boolean(json['history_remote_available']),
      oldestAvailableMessageRef:
          _entityRefOrNull(json['oldest_available_message_ref']),
      historyDegradedCode: _string(json['history_degraded_code']),
      version: _string(json['version']),
    );
  }

  final EntityRef ref;
  final EntityRef? guildRef;
  final ChannelType type;
  final String? name;
  final String? topic;
  final int position;
  final EntityRef? parentRef;
  final BigInt permissions;
  final EntityRef? lastMessageRef;
  final List<KaedeUser> recipients;
  final int slowModeSeconds;
  final bool permissionsSynced;
  final bool historyTruncated;
  final String? historyRetention;
  final bool historyRemoteAvailable;
  final EntityRef? oldestAvailableMessageRef;
  final String? historyDegradedCode;
  final String? version;

  bool allows(int bit) => permissions & BigInt.from(bit) != BigInt.zero;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'guild_id': guildRef?.id.value,
        'guild_domain': guildRef?.domain.value,
        'type': switch (type) {
          ChannelType.text => 0,
          ChannelType.dm => 1,
          ChannelType.voice => 2,
          ChannelType.category => 4,
          ChannelType.announcement => 5,
          ChannelType.unknown => -1,
        },
        'name': name,
        'topic': topic,
        'position': position,
        'parent_id': parentRef?.id.value,
        'parent_domain': parentRef?.domain.value,
        'permissions': permissions.toString(),
        'last_message_id': lastMessageRef?.id.value,
        'last_message_domain': lastMessageRef?.domain.value,
        'recipients': recipients.map((user) => user.toJson()).toList(),
        'rate_limit_per_user': slowModeSeconds,
        'permissions_synced': permissionsSynced,
        'history_truncated': historyTruncated,
        'history_retention': historyRetention,
        'history_remote_available': historyRemoteAvailable,
        'oldest_available_message_ref': oldestAvailableMessageRef == null
            ? null
            : <String, Object?>{
                'id': oldestAvailableMessageRef!.id.value,
                'origin_domain': oldestAvailableMessageRef!.domain.value,
              },
        'history_degraded_code': historyDegradedCode,
        'version': version,
      };
}

final class KaedeRole {
  const KaedeRole({
    required this.ref,
    required this.guildRef,
    required this.name,
    required this.color,
    required this.permissions,
    required this.position,
    required this.hoist,
    required this.mentionable,
    this.version,
  });

  factory KaedeRole.fromJson(Json json) => KaedeRole(
        ref: EntityRef(Snowflake(json['id']! as String),
            Domain(json['origin_domain']! as String)),
        guildRef: EntityRef(Snowflake(json['guild_id']! as String),
            Domain(json['guild_domain']! as String)),
        name: json['name']! as String,
        color: _integer(json['color']),
        permissions: BigInt.tryParse('${json['permissions']}') ?? BigInt.zero,
        position: _integer(json['position']),
        hoist: _boolean(json['hoist']),
        mentionable: _boolean(json['mentionable']),
        version: _string(json['version']),
      );

  final EntityRef ref;
  final EntityRef guildRef;
  final String name;
  final int color;
  final BigInt permissions;
  final int position;
  final bool hoist;
  final bool mentionable;
  final String? version;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'guild_id': guildRef.id.value,
        'guild_domain': guildRef.domain.value,
        'name': name,
        'color': color,
        'permissions': permissions.toString(),
        'position': position,
        'hoist': hoist,
        'mentionable': mentionable,
        'version': version,
      };
}

final class KaedeGuild {
  const KaedeGuild({
    required this.ref,
    required this.name,
    required this.ownerRef,
    required this.permissions,
    required this.unavailable,
    this.description,
    this.iconHash,
    this.bannerHash,
    this.channels = const <KaedeChannel>[],
    this.roles = const <KaedeRole>[],
    this.actorHighestRoleId,
    this.syncStatus,
    this.syncErrorCode,
    this.historySyncStatus,
    this.historySyncErrorCode,
    this.historySyncRetryAfterMs,
    this.historySyncResource,
    this.version,
  });

  factory KaedeGuild.fromJson(Json json) {
    final domain = Domain(json['origin_domain']! as String);
    return KaedeGuild(
      ref: EntityRef(Snowflake(json['id']! as String), domain),
      name: json['name']! as String,
      description: _string(json['description']),
      iconHash: _string(json['icon_hash']),
      bannerHash: _string(json['banner_hash']),
      ownerRef: EntityRef(Snowflake(json['owner_id']! as String),
          Domain(_string(json['owner_domain']) ?? domain.value)),
      permissions:
          BigInt.tryParse('${json['permissions'] ?? 0}') ?? BigInt.zero,
      unavailable: _boolean(json['unavailable']),
      channels: _objects(json['channels']).map(KaedeChannel.fromJson).toList(),
      roles: _objects(json['roles']).map(KaedeRole.fromJson).toList(),
      actorHighestRoleId: _string(json['actor_highest_role_id']),
      syncStatus: _string(json['sync_status']),
      syncErrorCode: _string(json['sync_error_code']),
      historySyncStatus: _string(json['history_sync_status']),
      historySyncErrorCode: _string(json['history_sync_error_code']),
      historySyncRetryAfterMs:
          _nullableInteger(json['history_sync_retry_after_ms']),
      historySyncResource: _string(json['history_sync_resource']),
      version: _string(json['version']),
    );
  }

  final EntityRef ref;
  final String name;
  final String? description;
  final String? iconHash;
  final String? bannerHash;
  final EntityRef ownerRef;
  final BigInt permissions;
  final bool unavailable;
  final List<KaedeChannel> channels;
  final List<KaedeRole> roles;
  final String? actorHighestRoleId;
  final String? syncStatus;
  final String? syncErrorCode;
  final String? historySyncStatus;
  final String? historySyncErrorCode;
  final int? historySyncRetryAfterMs;
  final String? historySyncResource;
  final String? version;

  bool allows(int bit) => permissions & BigInt.from(bit) != BigInt.zero;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'name': name,
        'description': description,
        'icon_hash': iconHash,
        'banner_hash': bannerHash,
        'owner_id': ownerRef.id.value,
        'owner_domain': ownerRef.domain.value,
        'permissions': permissions.toString(),
        'unavailable': unavailable,
        'channels': channels.map((channel) => channel.toJson()).toList(),
        'roles': roles.map((role) => role.toJson()).toList(),
        'actor_highest_role_id': actorHighestRoleId,
        'sync_status': syncStatus,
        'sync_error_code': syncErrorCode,
        'history_sync_status': historySyncStatus,
        'history_sync_error_code': historySyncErrorCode,
        'history_sync_retry_after_ms': historySyncRetryAfterMs,
        'history_sync_resource': historySyncResource,
        'version': version,
      };
}

final class KaedeAttachment {
  const KaedeAttachment({
    required this.ref,
    required this.filename,
    required this.contentType,
    required this.size,
    required this.scanStatus,
    this.width,
    this.height,
    this.blurHash,
    this.historyMediaUrl,
  });

  factory KaedeAttachment.fromJson(Json json) => KaedeAttachment(
        ref: EntityRef(Snowflake(json['id']! as String),
            Domain(json['origin_domain']! as String)),
        filename: json['filename']! as String,
        contentType: json['content_type']! as String,
        size: _integer(json['size']),
        scanStatus: _string(json['scan_status']) ?? 'pending',
        width: json['width'] is num ? (json['width']! as num).toInt() : null,
        height: json['height'] is num ? (json['height']! as num).toInt() : null,
        blurHash: _string(json['blurhash']),
        historyMediaUrl: _string(json['history_media_url']),
      );

  final EntityRef ref;
  final String filename;
  final String contentType;
  final int size;
  final int? width;
  final int? height;
  final String? blurHash;
  final String scanStatus;
  final String? historyMediaUrl;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'filename': filename,
        'content_type': contentType,
        'size': size,
        'width': width,
        'height': height,
        'blurhash': blurHash,
        'scan_status': scanStatus,
        'history_media_url': historyMediaUrl,
      };
}

final class KaedeMessage {
  const KaedeMessage({
    required this.ref,
    required this.channelRef,
    required this.authorRef,
    required this.createdAt,
    this.author,
    this.content,
    this.attachments = const <KaedeAttachment>[],
    this.mentionUserRefs = const <EntityRef>[],
    this.reference,
    this.clientNonce,
    this.editedAt,
    this.deliveryStatus,
    this.failureReason,
    this.retryable = true,
    this.pinned = false,
    this.reactionCounts = const <String, int>{},
    this.deletedAt,
    this.historyPageComplete = false,
    this.historyPageErrorCode,
    this.historyPageRetryAfterMs,
  });

  factory KaedeMessage.fromJson(Json json) {
    final referenceId = _string(json['referenced_message_id']);
    final referenceDomain = _string(json['referenced_message_domain']);
    final author = json['author'];
    return KaedeMessage(
      ref: EntityRef(Snowflake(json['id']! as String),
          Domain(json['origin_domain']! as String)),
      channelRef: EntityRef(Snowflake(json['channel_id']! as String),
          Domain(json['channel_domain']! as String)),
      authorRef: EntityRef(Snowflake(json['author_id']! as String),
          Domain(json['author_domain']! as String)),
      author: author is Map
          ? KaedeUser.fromJson(Map<String, Object?>.from(author))
          : null,
      content: _string(json['content']),
      attachments:
          _objects(json['attachments']).map(KaedeAttachment.fromJson).toList(),
      mentionUserRefs: (json['mention_user_refs'] as List? ?? const <Object>[])
          .map(EntityRef.fromJson)
          .toList(),
      reference: referenceId == null || referenceDomain == null
          ? null
          : EntityRef(Snowflake(referenceId), Domain(referenceDomain)),
      clientNonce: _string(json['client_nonce']),
      createdAt: DateTime.parse(json['created_at']! as String).toUtc(),
      editedAt: _string(json['edited_at']) == null
          ? null
          : DateTime.parse(json['edited_at']! as String).toUtc(),
      deliveryStatus: _string(json['delivery_status']),
      failureReason: _string(json['failure_reason']),
      retryable: _boolean(json['retryable'], true),
      pinned: json['pinned'] == true || json['pinned_at'] != null,
      reactionCounts: json['reaction_counts'] is Map
          ? Map<String, int>.unmodifiable(
              (json['reaction_counts']! as Map).map(
                (key, value) => MapEntry('$key', (value as num).toInt()),
              ),
            )
          : const <String, int>{},
      deletedAt: _string(json['deleted_at']) == null
          ? null
          : DateTime.parse(json['deleted_at']! as String).toUtc(),
      historyPageComplete: _boolean(json['history_page_complete']),
      historyPageErrorCode: _string(json['history_page_error_code']),
      historyPageRetryAfterMs:
          (json['history_page_retry_after_ms'] as num?)?.toInt(),
    );
  }

  final EntityRef ref;
  final EntityRef channelRef;
  final EntityRef authorRef;
  final KaedeUser? author;
  final String? content;
  final List<KaedeAttachment> attachments;
  final List<EntityRef> mentionUserRefs;
  final EntityRef? reference;
  final String? clientNonce;
  final DateTime createdAt;
  final DateTime? editedAt;
  final String? deliveryStatus;
  final String? failureReason;
  final bool retryable;
  final bool pinned;
  final Map<String, int> reactionCounts;
  final DateTime? deletedAt;
  final bool historyPageComplete;
  final String? historyPageErrorCode;
  final int? historyPageRetryAfterMs;

  KaedeMessage copyWith({
    KaedeUser? author,
    String? content,
    bool clearContent = false,
    List<KaedeAttachment>? attachments,
    List<EntityRef>? mentionUserRefs,
    EntityRef? reference,
    bool clearReference = false,
    String? clientNonce,
    DateTime? editedAt,
    String? deliveryStatus,
    String? failureReason,
    bool clearFailureReason = false,
    bool? retryable,
    bool? pinned,
    Map<String, int>? reactionCounts,
    DateTime? deletedAt,
    bool? historyPageComplete,
    String? historyPageErrorCode,
    bool clearHistoryPageErrorCode = false,
    int? historyPageRetryAfterMs,
  }) =>
      KaedeMessage(
        ref: ref,
        channelRef: channelRef,
        authorRef: authorRef,
        author: author ?? this.author,
        content: clearContent ? null : content ?? this.content,
        attachments: attachments ?? this.attachments,
        mentionUserRefs: mentionUserRefs ?? this.mentionUserRefs,
        reference: clearReference ? null : reference ?? this.reference,
        clientNonce: clientNonce ?? this.clientNonce,
        createdAt: createdAt,
        editedAt: editedAt ?? this.editedAt,
        deliveryStatus: deliveryStatus ?? this.deliveryStatus,
        failureReason:
            clearFailureReason ? null : failureReason ?? this.failureReason,
        retryable: retryable ?? this.retryable,
        pinned: pinned ?? this.pinned,
        reactionCounts: reactionCounts ?? this.reactionCounts,
        deletedAt: deletedAt ?? this.deletedAt,
        historyPageComplete: historyPageComplete ?? this.historyPageComplete,
        historyPageErrorCode: clearHistoryPageErrorCode
            ? null
            : historyPageErrorCode ?? this.historyPageErrorCode,
        historyPageRetryAfterMs:
            historyPageRetryAfterMs ?? this.historyPageRetryAfterMs,
      );

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'channel_id': channelRef.id.value,
        'channel_domain': channelRef.domain.value,
        'author_id': authorRef.id.value,
        'author_domain': authorRef.domain.value,
        'author': author?.toJson(),
        'content': content,
        'attachments': attachments.map((item) => item.toJson()).toList(),
        'mention_user_refs': mentionUserRefs.map((item) => item.wire).toList(),
        'referenced_message_id': reference?.id.value,
        'referenced_message_domain': reference?.domain.value,
        'client_nonce': clientNonce,
        'created_at': createdAt.toUtc().toIso8601String(),
        'edited_at': editedAt?.toUtc().toIso8601String(),
        'delivery_status': deliveryStatus,
        'failure_reason': failureReason,
        'retryable': retryable,
        'pinned': pinned,
        'reaction_counts': reactionCounts,
        'deleted_at': deletedAt?.toUtc().toIso8601String(),
        'history_page_complete': historyPageComplete,
        'history_page_error_code': historyPageErrorCode,
        'history_page_retry_after_ms': historyPageRetryAfterMs,
      };
}

final class GuildMember {
  const GuildMember(
      {required this.user,
      required this.roleIds,
      this.nickname,
      this.timeoutUntil});

  factory GuildMember.fromJson(Json json) => GuildMember(
        user:
            KaedeUser.fromJson(Map<String, Object?>.from(json['user']! as Map)),
        roleIds: (json['role_ids'] as List? ?? const <Object>[])
            .map((value) => '$value')
            .toList(),
        nickname: _string(json['nickname']),
        timeoutUntil: _string(json['timeout_until']) == null
            ? null
            : DateTime.parse(json['timeout_until']! as String),
      );

  final KaedeUser user;
  final List<String> roleIds;
  final String? nickname;
  final DateTime? timeoutUntil;

  Json toJson() => <String, Object?>{
        'user': user.toJson(),
        'role_ids': roleIds,
        'nickname': nickname,
        'timeout_until': timeoutUntil?.toUtc().toIso8601String(),
      };
}

GuildMember overlayGuildMemberProfile(
  GuildMember member,
  Map<EntityRef, KaedeUser> userProfiles,
) {
  final user = userProfiles[member.user.ref];
  if (user == null || identical(user, member.user)) return member;
  return GuildMember(
    user: user,
    roleIds: member.roleIds,
    nickname: member.nickname,
    timeoutUntil: member.timeoutUntil,
  );
}

final class GuildSelfModerationStatus {
  const GuildSelfModerationStatus({
    required this.guildRef,
    required this.timedOut,
    required this.timeoutIndefinite,
    this.detailsAvailable = true,
    this.timeoutUntil,
    this.reason,
  });

  factory GuildSelfModerationStatus.fromJson(Json json) {
    final until = _string(json['timeout_until']);
    return GuildSelfModerationStatus(
      guildRef: EntityRef(
        Snowflake('${json['guild_id']}'),
        Domain('${json['guild_domain']}'),
      ),
      timedOut: json['timed_out'] == true,
      timeoutIndefinite: json['timeout_indefinite'] == true,
      detailsAvailable: json['details_available'] != false,
      timeoutUntil: until == null ? null : DateTime.parse(until).toUtc(),
      reason: _string(json['reason'])?.trim(),
    );
  }

  final EntityRef guildRef;
  final bool timedOut;
  final DateTime? timeoutUntil;
  final bool timeoutIndefinite;
  final bool detailsAvailable;
  final String? reason;

  bool activeAt([DateTime? now]) {
    if (!timedOut) return false;
    if (timeoutIndefinite) return timeoutUntil == null;
    final until = timeoutUntil;
    return until != null && until.isAfter((now ?? DateTime.now()).toUtc());
  }

  Json toJson() => <String, Object?>{
        'guild_id': guildRef.id.value,
        'guild_domain': guildRef.domain.value,
        'timed_out': timedOut,
        'timeout_until': timeoutUntil?.toIso8601String(),
        'timeout_indefinite': timeoutIndefinite,
        'details_available': detailsAvailable,
        'reason': reason,
      };
}

bool shouldRetrySelfModerationStatus(
  GuildSelfModerationStatus status, {
  required bool appActive,
  required bool conversationPaneVisible,
  required EntityRef? selectedGuild,
}) =>
    status.activeAt() &&
    !status.detailsAvailable &&
    appActive &&
    conversationPaneVisible &&
    selectedGuild == status.guildRef;
