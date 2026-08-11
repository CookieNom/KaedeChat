import 'package:kaede_mobile/src/core/refs.dart';

typedef Json = Map<String, Object?>;

String? _string(Object? value) => value is String ? value : null;
int _integer(Object? value, [int fallback = 0]) =>
    value is num ? value.toInt() : int.tryParse('$value') ?? fallback;
bool _boolean(Object? value, [bool fallback = false]) =>
    value is bool ? value : fallback;
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
  final PresenceStatus presence;

  String get name =>
      displayName?.trim().isNotEmpty == true ? displayName! : username;

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
      );

  final EntityRef ref;
  final String filename;
  final String contentType;
  final int size;
  final int? width;
  final int? height;
  final String? blurHash;
  final String scanStatus;

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
    this.pinned = false,
    this.reactionCounts = const <String, int>{},
    this.deletedAt,
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
  final bool pinned;
  final Map<String, int> reactionCounts;
  final DateTime? deletedAt;

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
    bool? pinned,
    Map<String, int>? reactionCounts,
    DateTime? deletedAt,
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
        pinned: pinned ?? this.pinned,
        reactionCounts: reactionCounts ?? this.reactionCounts,
        deletedAt: deletedAt ?? this.deletedAt,
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
        'pinned': pinned,
        'reaction_counts': reactionCounts,
        'deleted_at': deletedAt?.toUtc().toIso8601String(),
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
