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
    this.conversationType = 'direct',
    this.ownerRef,
    this.slowModeSeconds = 0,
    this.permissionsSynced = false,
    this.historyTruncated = false,
    this.historyRetention,
    this.historyRemoteAvailable = false,
    this.oldestAvailableMessageRef,
    this.historyDegradedCode,
    this.encryptionMode = 'plaintext',
    this.encryptionState = 'plaintext',
    this.encryptionPolicyGeneration = 0,
    this.encryptionProtocol,
    this.encryptionSuite,
    this.encryptionGroupId,
    this.encryptionEpoch,
    this.encryptionActivatedAt,
    this.searchAvailable = true,
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
    final ownerId = _string(json['owner_id']);
    final ownerDomain = _string(json['owner_domain']);
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
      conversationType: _string(json['conversation_type']) ?? 'direct',
      ownerRef: ownerId == null || ownerDomain == null
          ? null
          : EntityRef(Snowflake(ownerId), Domain(ownerDomain)),
      slowModeSeconds: _integer(json['rate_limit_per_user']),
      permissionsSynced: _boolean(json['permissions_synced']),
      historyTruncated: _boolean(json['history_truncated']),
      historyRetention: _string(json['history_retention']),
      historyRemoteAvailable: _boolean(json['history_remote_available']),
      oldestAvailableMessageRef:
          _entityRefOrNull(json['oldest_available_message_ref']),
      historyDegradedCode: _string(json['history_degraded_code']),
      encryptionMode: _string(json['encryption_mode']) ?? 'plaintext',
      encryptionState: _string(json['encryption_state']) ?? 'plaintext',
      encryptionPolicyGeneration:
          _integer(json['encryption_policy_generation']),
      encryptionProtocol: _string(json['encryption_protocol']),
      encryptionSuite: _string(json['encryption_suite']),
      encryptionGroupId: _string(json['encryption_group_id']),
      encryptionEpoch: _nullableInteger(json['encryption_epoch']),
      encryptionActivatedAt: _string(json['encryption_activated_at']) == null
          ? null
          : DateTime.parse(json['encryption_activated_at']! as String).toUtc(),
      searchAvailable: _boolean(json['search_available'], true),
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
  final String conversationType;
  final EntityRef? ownerRef;
  final int slowModeSeconds;
  final bool permissionsSynced;
  final bool historyTruncated;
  final String? historyRetention;
  final bool historyRemoteAvailable;
  final EntityRef? oldestAvailableMessageRef;
  final String? historyDegradedCode;
  final String encryptionMode;
  final String encryptionState;
  final int encryptionPolicyGeneration;
  final String? encryptionProtocol;
  final String? encryptionSuite;
  final String? encryptionGroupId;
  final int? encryptionEpoch;
  final DateTime? encryptionActivatedAt;
  final bool searchAvailable;
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
        'conversation_type': conversationType,
        'owner_id': ownerRef?.id.value,
        'owner_domain': ownerRef?.domain.value,
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
        'encryption_mode': encryptionMode,
        'encryption_state': encryptionState,
        'encryption_policy_generation': encryptionPolicyGeneration.toString(),
        'encryption_protocol': encryptionProtocol,
        'encryption_suite': encryptionSuite,
        'encryption_group_id': encryptionGroupId,
        'encryption_epoch': encryptionEpoch?.toString(),
        'encryption_activated_at':
            encryptionActivatedAt?.toUtc().toIso8601String(),
        'search_available': searchAvailable,
        'version': version,
      };

  /// Returns a copy with the [user] replacing the recipient whose ref equals
  /// [userRef]. Cheaper than a JSON round-trip for a single-field profile
  /// update and keeps every other channel instance untouched.
  KaedeChannel withRecipientReplaced(EntityRef userRef, KaedeUser user) =>
      KaedeChannel(
        ref: ref,
        type: type,
        position: position,
        permissions: permissions,
        guildRef: guildRef,
        name: name,
        topic: topic,
        parentRef: parentRef,
        lastMessageRef: lastMessageRef,
        recipients: List.unmodifiable(
          [
            for (final recipient in recipients)
              recipient.ref == userRef ? user : recipient,
          ],
        ),
        conversationType: conversationType,
        ownerRef: ownerRef,
        slowModeSeconds: slowModeSeconds,
        permissionsSynced: permissionsSynced,
        historyTruncated: historyTruncated,
        historyRetention: historyRetention,
        historyRemoteAvailable: historyRemoteAvailable,
        oldestAvailableMessageRef: oldestAvailableMessageRef,
        historyDegradedCode: historyDegradedCode,
        encryptionMode: encryptionMode,
        encryptionState: encryptionState,
        encryptionPolicyGeneration: encryptionPolicyGeneration,
        encryptionProtocol: encryptionProtocol,
        encryptionSuite: encryptionSuite,
        encryptionGroupId: encryptionGroupId,
        encryptionEpoch: encryptionEpoch,
        encryptionActivatedAt: encryptionActivatedAt,
        searchAvailable: searchAvailable,
        version: version,
      );
}

final class MessageSearchResult {
  const MessageSearchResult({
    required this.message,
    required this.channel,
    required this.snippet,
    this.guild,
  });

  factory MessageSearchResult.fromJson(Json json) => MessageSearchResult(
        message: KaedeMessage.fromJson(
            Map<String, Object?>.from(json['message']! as Map)),
        channel: KaedeChannel.fromJson(
            Map<String, Object?>.from(json['channel']! as Map)),
        guild: json['guild'] is Map
            ? KaedeGuild.fromJson(
                Map<String, Object?>.from(json['guild']! as Map))
            : null,
        snippet: _string(json['snippet']) ?? '',
      );

  final KaedeMessage message;
  final KaedeChannel channel;
  final KaedeGuild? guild;
  final String snippet;
}

final class MessageSearchPage {
  const MessageSearchPage({
    required this.results,
    required this.localCoverage,
    required this.authorityCoverage,
    this.nextCursor,
    this.encryptedChannelRefs = const <EntityRef>[],
    this.indexing = false,
  });

  factory MessageSearchPage.fromJson(Json json) {
    final coverage = json['coverage'] is Map
        ? Map<String, Object?>.from(json['coverage']! as Map)
        : const <String, Object?>{};
    return MessageSearchPage(
      results: _objects(json['results'])
          .map(MessageSearchResult.fromJson)
          .toList(growable: false),
      localCoverage: _string(coverage['local']) ?? 'unavailable',
      authorityCoverage: _string(coverage['authority']) ?? 'not_queried',
      nextCursor: _string(json['next_cursor']),
      encryptedChannelRefs:
          (json['encrypted_channel_refs'] as List? ?? const [])
              .whereType<String>()
              .map(EntityRef.parse)
              .toList(growable: false),
      indexing: _boolean(json['indexing']),
    );
  }

  final List<MessageSearchResult> results;
  final String localCoverage;
  final String authorityCoverage;
  final String? nextCursor;
  final List<EntityRef> encryptedChannelRefs;
  final bool indexing;
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
    this.federatedHistoryPolicy = 'disabled',
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
      federatedHistoryPolicy:
          _string(json['federated_history_policy']) ?? 'disabled',
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
  final String federatedHistoryPolicy;
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
        'federated_history_policy': federatedHistoryPolicy,
        'actor_highest_role_id': actorHighestRoleId,
        'sync_status': syncStatus,
        'sync_error_code': syncErrorCode,
        'history_sync_status': historySyncStatus,
        'history_sync_error_code': historySyncErrorCode,
        'history_sync_retry_after_ms': historySyncRetryAfterMs,
        'history_sync_resource': historySyncResource,
        'version': version,
      };

  /// Returns a copy carrying the [status] history-sync projection. Mirrors
  /// what the gateway `GUILD_HISTORY_SYNC_UPDATE` JSON patch produced, but
  /// without serializing the whole guild.
  KaedeGuild withHistorySyncStatus(
    String status, {
    required Object? code,
    required Object? retryAfterMs,
    required Object? resource,
  }) =>
      KaedeGuild(
        ref: ref,
        name: name,
        description: description,
        iconHash: iconHash,
        bannerHash: bannerHash,
        ownerRef: ownerRef,
        permissions: permissions,
        unavailable: unavailable,
        channels: channels,
        roles: roles,
        federatedHistoryPolicy: federatedHistoryPolicy,
        actorHighestRoleId: actorHighestRoleId,
        syncStatus: syncStatus,
        syncErrorCode: syncErrorCode,
        historySyncStatus: status,
        historySyncErrorCode: status == 'ready' ? null : _string(code),
        historySyncRetryAfterMs:
            status == 'retrying' ? _nullableInteger(retryAfterMs) : null,
        historySyncResource: status == 'failed' ? _string(resource) : null,
        version: version,
      );

  /// Returns a copy carrying [channels] instead of the current channel list.
  /// Used to reconcile a locally created channel without re-parsing the whole
  /// guild projection.
  KaedeGuild withChannels(List<KaedeChannel> channels) => KaedeGuild(
        ref: ref,
        name: name,
        description: description,
        iconHash: iconHash,
        bannerHash: bannerHash,
        ownerRef: ownerRef,
        permissions: permissions,
        unavailable: unavailable,
        channels: channels,
        roles: roles,
        federatedHistoryPolicy: federatedHistoryPolicy,
        actorHighestRoleId: actorHighestRoleId,
        syncStatus: syncStatus,
        syncErrorCode: syncErrorCode,
        historySyncStatus: historySyncStatus,
        historySyncErrorCode: historySyncErrorCode,
        historySyncRetryAfterMs: historySyncRetryAfterMs,
        historySyncResource: historySyncResource,
        version: version,
      );
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
    this.e2ee,
    this.encryptionPolicyGeneration = 0,
    this.encryptionEpoch,
    this.messageType = 0,
    this.attachments = const <KaedeAttachment>[],
    this.decryptedAttachments = const <Json>[],
    this.mentionUserRefs = const <EntityRef>[],
    this.reference,
    this.clientNonce,
    this.editedAt,
    this.deliveryStatus,
    this.failureReason,
    this.retryable = true,
    this.pinned = false,
    this.reactionCounts = const <String, int>{},
    this.reactedEmoji = const <String>{},
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
      e2ee: json['e2ee'] is Map
          ? Map<String, Object?>.unmodifiable(
              Map<String, Object?>.from(json['e2ee']! as Map),
            )
          : null,
      encryptionPolicyGeneration:
          _integer(json['encryption_policy_generation']),
      encryptionEpoch: _nullableInteger(json['encryption_epoch']),
      messageType: (json['message_type'] as num?)?.toInt() ?? 0,
      attachments:
          _objects(json['attachments']).map(KaedeAttachment.fromJson).toList(),
      decryptedAttachments: _objects(json['decrypted_attachments']),
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
      reactedEmoji: Set<String>.unmodifiable(
        (json['reacted_emoji'] as List? ?? const <Object>[])
            .map((value) => '$value'),
      ),
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
  final Json? e2ee;
  final int encryptionPolicyGeneration;
  final int? encryptionEpoch;
  final int messageType;
  final List<KaedeAttachment> attachments;
  final List<Json> decryptedAttachments;
  final List<EntityRef> mentionUserRefs;
  final EntityRef? reference;
  final String? clientNonce;
  final DateTime createdAt;
  final DateTime? editedAt;
  final String? deliveryStatus;
  final String? failureReason;
  final bool retryable;
  final bool pinned;
  final Set<String> reactedEmoji;
  final Map<String, int> reactionCounts;
  final DateTime? deletedAt;
  final bool historyPageComplete;
  final String? historyPageErrorCode;
  final int? historyPageRetryAfterMs;

  KaedeMessage copyWith({
    KaedeUser? author,
    String? content,
    Json? e2ee,
    bool clearE2ee = false,
    int? encryptionPolicyGeneration,
    int? encryptionEpoch,
    bool clearEncryptionEpoch = false,
    int? messageType,
    bool clearContent = false,
    List<KaedeAttachment>? attachments,
    List<Json>? decryptedAttachments,
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
    Set<String>? reactedEmoji,
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
        e2ee: clearE2ee ? null : e2ee ?? this.e2ee,
        encryptionPolicyGeneration:
            encryptionPolicyGeneration ?? this.encryptionPolicyGeneration,
        encryptionEpoch: clearEncryptionEpoch
            ? null
            : encryptionEpoch ?? this.encryptionEpoch,
        messageType: messageType ?? this.messageType,
        attachments: attachments ?? this.attachments,
        decryptedAttachments: decryptedAttachments ?? this.decryptedAttachments,
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
        reactedEmoji: reactedEmoji ?? this.reactedEmoji,
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
        'e2ee': e2ee,
        'encryption_policy_generation': encryptionPolicyGeneration.toString(),
        'encryption_epoch': encryptionEpoch?.toString(),
        'message_type': messageType,
        'attachments': attachments.map((item) => item.toJson()).toList(),
        'decrypted_attachments': decryptedAttachments,
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
        'reacted_emoji': reactedEmoji.toList(),
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
