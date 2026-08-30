import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/reaction_emoji.dart';
import 'package:kaede_mobile/src/domain/rich_content.dart';

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

Json? _messageReference(Object? value) {
  if (value == null) return null;
  if (value is! Map || value.keys.any((key) => key is! String)) {
    throw const FormatException('Invalid message reference.');
  }
  final reference = Map<String, Object?>.from(value);
  if (reference['type'] != null && reference['type'] is! int) {
    throw const FormatException('Invalid message reference type.');
  }
  return Map<String, Object?>.unmodifiable(reference);
}

EntityRef? _messageReferenceEntity(
  Json? reference,
  String idKey,
  String domainKey,
) {
  if (reference == null) return null;
  final id = _string(reference[idKey]);
  final domain = _string(reference[domainKey]);
  if (id == null && domain == null) return null;
  if (id == null || domain == null) {
    throw const FormatException('Incomplete federated message reference.');
  }
  return EntityRef(Snowflake(id), Domain(domain));
}

EntityRef? _messageWebhookRef(Json json) {
  final webhookId = _string(json['webhook_id']);
  final rawWebhook = json['webhook'];
  if (webhookId == null && rawWebhook == null) return null;
  if (webhookId == null || rawWebhook is! Map) {
    throw const FormatException('Invalid webhook message identity.');
  }
  final webhook = Map<String, Object?>.from(rawWebhook);
  final id = _string(webhook['id']);
  final domain = _string(webhook['origin_domain']);
  final ref = _string(webhook['ref']);
  if (id != webhookId || domain == null || ref == null) {
    throw const FormatException('Invalid webhook message identity.');
  }
  final parsed = EntityRef.parse(ref);
  if (parsed.id.value != id || parsed.domain.value != domain) {
    throw const FormatException('Invalid webhook message identity.');
  }
  return parsed;
}

List<Json> _objects(Object? value) {
  if (value == null) return const <Json>[];
  if (value is! List) throw const FormatException('Expected an object array.');
  final result = <Json>[];
  for (final item in value) {
    if (item is! Map || item.keys.any((key) => key is! String)) {
      throw const FormatException('Object array contains an invalid child.');
    }
    result.add(Map<String, Object?>.from(item));
  }
  return result;
}

enum PresenceStatus { online, idle, dnd, invisible, offline }

enum ChannelType {
  text,
  dm,
  groupDm,
  voice,
  stage,
  unknown,
  category,
  announcement,
  announcementThread,
  publicThread,
  privateThread,
  forum,
  tracker,
}

extension ChannelTypeCapabilities on ChannelType {
  bool get isVoiceLike =>
      this == ChannelType.voice || this == ChannelType.stage;
}

enum RelationshipType { friend, pendingIn, pendingOut, blocked }

ChannelType channelType(int value) => switch (value) {
      0 => ChannelType.text,
      1 => ChannelType.dm,
      3 => ChannelType.groupDm,
      2 => ChannelType.voice,
      13 => ChannelType.stage,
      4 => ChannelType.category,
      5 => ChannelType.announcement,
      10 => ChannelType.announcementThread,
      11 => ChannelType.publicThread,
      12 => ChannelType.privateThread,
      15 => ChannelType.forum,
      17 => ChannelType.tracker,
      _ => ChannelType.unknown,
    };

final class ForumTag {
  const ForumTag({
    required this.id,
    required this.name,
    this.moderated = false,
    this.emojiId,
    this.emojiName,
  });

  factory ForumTag.fromJson(Json json) => ForumTag(
        id: '${json['id']}',
        name: _string(json['name']) ?? '',
        moderated: _boolean(json['moderated']),
        emojiId: _string(json['emoji_id']),
        emojiName: _string(json['emoji_name']),
      );

  final String id;
  final String name;
  final bool moderated;
  final String? emojiId;
  final String? emojiName;

  String? get emoji => emojiName?.trim().isNotEmpty == true ? emojiName : null;

  Json toJson() => <String, Object?>{
        'id': id,
        'name': name,
        'moderated': moderated,
        if (emojiId?.isNotEmpty == true)
          'emoji_id': emojiId
        else if (emojiName?.isNotEmpty == true)
          'emoji_name': emojiName,
      };
}

final class ThreadMember {
  const ThreadMember({
    required this.threadRef,
    required this.userRef,
    required this.joinTimestamp,
    this.flags = 0,
    this.notificationLevel = 'inherit',
  });

  factory ThreadMember.fromJson(Json json, {EntityRef? thread}) {
    final threadRef = thread ??
        EntityRef(
          Snowflake('${json['id'] ?? json['thread_id']}'),
          Domain('${json['thread_domain'] ?? json['origin_domain']}'),
        );
    return ThreadMember(
      threadRef: threadRef,
      userRef: EntityRef(
        Snowflake('${json['user_id']}'),
        Domain('${json['user_domain'] ?? json['origin_domain']}'),
      ),
      joinTimestamp: DateTime.parse(
        '${json['join_timestamp'] ?? json['joined_at']}',
      ).toUtc(),
      flags: _integer(json['flags']),
      notificationLevel: switch (_string(json['notification_level'])) {
        'all' => 'all',
        'mentions' => 'mentions',
        'none' => 'none',
        _ => 'inherit',
      },
    );
  }

  final EntityRef threadRef;
  final EntityRef userRef;
  final DateTime joinTimestamp;
  final int flags;
  final String notificationLevel;

  ThreadMember copyWith({String? notificationLevel}) => ThreadMember(
        threadRef: threadRef,
        userRef: userRef,
        joinTimestamp: joinTimestamp,
        flags: flags,
        notificationLevel: notificationLevel ?? this.notificationLevel,
      );

  Json toJson() => <String, Object?>{
        'id': threadRef.id.value,
        'thread_domain': threadRef.domain.value,
        'user_id': userRef.id.value,
        'user_domain': userRef.domain.value,
        'join_timestamp': joinTimestamp.toUtc().toIso8601String(),
        'flags': flags,
        'notification_level': notificationLevel,
      };
}

enum AccountType { human, bot }

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
    this.ageAssuranceState = 'unknown',
    this.accountType = AccountType.human,
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
      ageAssuranceState: _string(json['age_assurance_state']) ?? 'unknown',
      accountType: switch (_string(json['account_type'])) {
        'bot' => AccountType.bot,
        _ when _boolean(json['bot']) => AccountType.bot,
        _ => AccountType.human,
      },
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
  final String ageAssuranceState;
  final AccountType accountType;
  final bool profileResolved;
  final PresenceStatus presence;

  bool get isApplication => accountType == AccountType.bot;

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
        'age_assurance_state': ageAssuranceState,
        'account_type': accountType.name,
        'bot': isApplication,
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
    this.createdAt,
    this.guildRef,
    this.name,
    this.topic,
    this.nsfw = false,
    this.parentRef,
    this.lastMessageRef,
    this.recipients = const <KaedeUser>[],
    this.conversationType = 'direct',
    this.ownerRef,
    this.slowModeSeconds = 0,
    this.bitrate = 64000,
    this.userLimit = 0,
    this.rtcRegion,
    this.videoQualityMode = 1,
    this.permissionsSynced = false,
    this.historyTruncated = false,
    this.historyRetention,
    this.federatedHistoryPolicy = 'inherit',
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
    this.archived = false,
    this.locked = false,
    this.invitable = true,
    this.autoArchiveDuration = 1440,
    this.archiveTimestamp,
    this.messageCount = 0,
    this.totalMessageSent = 0,
    this.memberCount = 0,
    this.flags = 0,
    this.appliedTagIds = const <String>[],
    this.availableTags = const <ForumTag>[],
    this.defaultReactionEmoji,
    this.defaultThreadRateLimitPerUser = 0,
    this.defaultAutoArchiveDuration = 1440,
    this.defaultSortOrder,
    this.defaultForumLayout = 0,
    this.e2eeRequired = false,
    this.starterMessage,
    this.member,
    this.version,
  });

  factory KaedeChannel.fromJson(Json json) {
    final domain = Domain(json['origin_domain']! as String);
    final ref = EntityRef(Snowflake(json['id']! as String), domain);
    final guildId = _string(json['guild_id']);
    final guildDomain = _string(json['guild_domain']);
    final parentId = _string(json['parent_id']);
    final parentDomain = _string(json['parent_domain']);
    final lastId = _string(json['last_message_id']);
    final lastDomain = _string(json['last_message_domain']);
    final ownerId = _string(json['owner_id']);
    final ownerDomain = _string(json['owner_domain']);
    final createdAt = _string(json['created_at']);
    final parentRef = parentId == null || parentDomain == null
        ? null
        : EntityRef(Snowflake(parentId), Domain(parentDomain));
    final ownerRef = ownerId == null || ownerDomain == null
        ? null
        : EntityRef(Snowflake(ownerId), Domain(ownerDomain));
    return KaedeChannel(
      ref: ref,
      guildRef: guildId == null || guildDomain == null
          ? null
          : EntityRef(Snowflake(guildId), Domain(guildDomain)),
      type: channelType(_integer(json['type'])),
      name: _string(json['name']),
      topic: _string(json['topic']),
      nsfw: _boolean(json['nsfw']),
      position: _integer(json['position']),
      createdAt:
          createdAt == null ? null : DateTime.tryParse(createdAt)?.toUtc(),
      parentRef: parentRef,
      permissions:
          BigInt.tryParse(_string(json['permissions']) ?? '0') ?? BigInt.zero,
      lastMessageRef: lastId == null || lastDomain == null
          ? null
          : EntityRef(Snowflake(lastId), Domain(lastDomain)),
      recipients: _objects(json['recipients']).map(KaedeUser.fromJson).toList(),
      conversationType: _string(json['conversation_type']) ?? 'direct',
      ownerRef: ownerRef,
      slowModeSeconds: _integer(json['rate_limit_per_user']),
      bitrate: _integer(json['bitrate'], 64000),
      userLimit: _integer(json['user_limit']),
      rtcRegion: _string(json['rtc_region']),
      videoQualityMode: _integer(json['video_quality_mode'], 1),
      permissionsSynced: _boolean(json['permissions_synced']),
      historyTruncated: _boolean(json['history_truncated']),
      historyRetention: _string(json['history_retention']),
      federatedHistoryPolicy:
          _string(json['federated_history_policy']) ?? 'inherit',
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
      archived: _boolean(json['archived']),
      locked: _boolean(json['locked']),
      invitable: _boolean(json['invitable'], true),
      autoArchiveDuration: _integer(json['auto_archive_duration'], 1440),
      archiveTimestamp: _string(json['archive_timestamp']) == null
          ? null
          : DateTime.parse('${json['archive_timestamp']}').toUtc(),
      messageCount: _integer(json['message_count']),
      totalMessageSent: _integer(json['total_message_sent']),
      memberCount: _integer(json['member_count']),
      flags: _integer(json['flags']),
      appliedTagIds: (json['applied_tag_ids'] ?? json['applied_tags']) is List
          ? ((json['applied_tag_ids'] ?? json['applied_tags'])! as List)
              .map((value) => '$value')
              .toList(growable: false)
          : const <String>[],
      availableTags:
          _objects(json['available_tags']).map(ForumTag.fromJson).toList(),
      defaultReactionEmoji: json['default_reaction_emoji'] is Map
          ? Map<String, Object?>.from(json['default_reaction_emoji']! as Map)
          : null,
      defaultThreadRateLimitPerUser:
          _integer(json['default_thread_rate_limit_per_user']),
      defaultAutoArchiveDuration:
          _integer(json['default_auto_archive_duration'], 1440),
      defaultSortOrder: _nullableInteger(json['default_sort_order']),
      defaultForumLayout: _integer(json['default_forum_layout']),
      e2eeRequired: _boolean(json['e2ee_required']),
      starterMessage: json['starter_message'] is Map
          ? KaedeMessage.fromThreadStarterJson(
              Map<String, Object?>.from(json['starter_message']! as Map),
              thread: ref,
              parent: parentRef,
              owner: ownerRef,
            )
          : null,
      member: json['member'] is Map
          ? ThreadMember.fromJson(
              Map<String, Object?>.from(json['member']! as Map),
              thread: ref,
            )
          : null,
      version: _string(json['version']),
    );
  }

  final EntityRef ref;
  final EntityRef? guildRef;
  final ChannelType type;
  final String? name;
  final String? topic;
  final bool nsfw;
  final int position;
  final DateTime? createdAt;
  final EntityRef? parentRef;
  final BigInt permissions;
  final EntityRef? lastMessageRef;
  final List<KaedeUser> recipients;
  final String conversationType;
  final EntityRef? ownerRef;
  final int slowModeSeconds;
  final int bitrate;
  final int userLimit;
  final String? rtcRegion;
  final int videoQualityMode;
  final bool permissionsSynced;
  final bool historyTruncated;
  final String? historyRetention;
  final String federatedHistoryPolicy;
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
  final bool archived;
  final bool locked;
  final bool invitable;
  final int autoArchiveDuration;
  final DateTime? archiveTimestamp;
  final int messageCount;
  final int totalMessageSent;
  final int memberCount;
  final int flags;
  final List<String> appliedTagIds;
  final List<ForumTag> availableTags;
  final Map<String, Object?>? defaultReactionEmoji;
  final int defaultThreadRateLimitPerUser;
  final int defaultAutoArchiveDuration;
  final int? defaultSortOrder;
  final int defaultForumLayout;
  final bool e2eeRequired;
  final KaedeMessage? starterMessage;
  final ThreadMember? member;
  final String? version;

  bool allows(int bit) => permissions & BigInt.from(bit) != BigInt.zero;

  bool get isThread => switch (type) {
        ChannelType.announcementThread ||
        ChannelType.publicThread ||
        ChannelType.privateThread =>
          true,
        _ => false,
      };

  bool get isForum => type == ChannelType.forum;
  bool get followed => member != null;
  bool get pinned => flags & 2 != 0;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'guild_id': guildRef?.id.value,
        'guild_domain': guildRef?.domain.value,
        'type': switch (type) {
          ChannelType.text => 0,
          ChannelType.dm => 1,
          ChannelType.groupDm => 3,
          ChannelType.voice => 2,
          ChannelType.stage => 13,
          ChannelType.category => 4,
          ChannelType.announcement => 5,
          ChannelType.announcementThread => 10,
          ChannelType.publicThread => 11,
          ChannelType.privateThread => 12,
          ChannelType.forum => 15,
          ChannelType.tracker => 17,
          ChannelType.unknown => -1,
        },
        'name': name,
        'topic': topic,
        'nsfw': nsfw,
        'position': position,
        'created_at': createdAt?.toUtc().toIso8601String(),
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
        'bitrate': type.isVoiceLike ? bitrate : null,
        'user_limit': type.isVoiceLike ? userLimit : null,
        'rtc_region': type.isVoiceLike ? rtcRegion : null,
        'video_quality_mode': type.isVoiceLike ? videoQualityMode : null,
        'permissions_synced': permissionsSynced,
        'history_truncated': historyTruncated,
        'history_retention': historyRetention,
        'federated_history_policy': federatedHistoryPolicy,
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
        'archived': archived,
        'locked': locked,
        'invitable': invitable,
        'auto_archive_duration': autoArchiveDuration,
        'archive_timestamp': archiveTimestamp?.toUtc().toIso8601String(),
        'message_count': messageCount,
        'total_message_sent': totalMessageSent,
        'member_count': memberCount,
        'flags': flags,
        'applied_tag_ids': appliedTagIds,
        'available_tags': availableTags.map((tag) => tag.toJson()).toList(),
        'default_reaction_emoji': defaultReactionEmoji,
        'default_thread_rate_limit_per_user': defaultThreadRateLimitPerUser,
        'default_auto_archive_duration': defaultAutoArchiveDuration,
        'default_sort_order': defaultSortOrder,
        'default_forum_layout': defaultForumLayout,
        'e2ee_required': e2eeRequired,
        'starter_message': starterMessage?.toJson(),
        'member': member?.toJson(),
        'version': version,
      };

  KaedeChannel copyWith({
    String? name,
    DateTime? createdAt,
    bool clearCreatedAt = false,
    bool? archived,
    bool? locked,
    bool? invitable,
    int? autoArchiveDuration,
    DateTime? archiveTimestamp,
    int? messageCount,
    int? totalMessageSent,
    int? memberCount,
    int? flags,
    List<String>? appliedTagIds,
    String? encryptionMode,
    String? encryptionState,
    int? encryptionPolicyGeneration,
    String? encryptionProtocol,
    String? encryptionSuite,
    String? encryptionGroupId,
    int? encryptionEpoch,
    DateTime? encryptionActivatedAt,
    KaedeMessage? starterMessage,
    ThreadMember? member,
    bool clearMember = false,
    String? version,
  }) =>
      KaedeChannel(
        ref: ref,
        type: type,
        position: position,
        permissions: permissions,
        createdAt: clearCreatedAt ? null : createdAt ?? this.createdAt,
        guildRef: guildRef,
        name: name ?? this.name,
        topic: topic,
        nsfw: nsfw,
        parentRef: parentRef,
        lastMessageRef: lastMessageRef,
        recipients: recipients,
        conversationType: conversationType,
        ownerRef: ownerRef,
        slowModeSeconds: slowModeSeconds,
        bitrate: bitrate,
        userLimit: userLimit,
        rtcRegion: rtcRegion,
        videoQualityMode: videoQualityMode,
        permissionsSynced: permissionsSynced,
        historyTruncated: historyTruncated,
        historyRetention: historyRetention,
        federatedHistoryPolicy: federatedHistoryPolicy,
        historyRemoteAvailable: historyRemoteAvailable,
        oldestAvailableMessageRef: oldestAvailableMessageRef,
        historyDegradedCode: historyDegradedCode,
        encryptionMode: encryptionMode ?? this.encryptionMode,
        encryptionState: encryptionState ?? this.encryptionState,
        encryptionPolicyGeneration:
            encryptionPolicyGeneration ?? this.encryptionPolicyGeneration,
        encryptionProtocol: encryptionProtocol ?? this.encryptionProtocol,
        encryptionSuite: encryptionSuite ?? this.encryptionSuite,
        encryptionGroupId: encryptionGroupId ?? this.encryptionGroupId,
        encryptionEpoch: encryptionEpoch ?? this.encryptionEpoch,
        encryptionActivatedAt:
            encryptionActivatedAt ?? this.encryptionActivatedAt,
        searchAvailable: searchAvailable,
        archived: archived ?? this.archived,
        locked: locked ?? this.locked,
        invitable: invitable ?? this.invitable,
        autoArchiveDuration: autoArchiveDuration ?? this.autoArchiveDuration,
        archiveTimestamp: archiveTimestamp ?? this.archiveTimestamp,
        messageCount: messageCount ?? this.messageCount,
        totalMessageSent: totalMessageSent ?? this.totalMessageSent,
        memberCount: memberCount ?? this.memberCount,
        flags: flags ?? this.flags,
        appliedTagIds: appliedTagIds ?? this.appliedTagIds,
        availableTags: availableTags,
        defaultReactionEmoji: defaultReactionEmoji,
        defaultThreadRateLimitPerUser: defaultThreadRateLimitPerUser,
        defaultAutoArchiveDuration: defaultAutoArchiveDuration,
        defaultSortOrder: defaultSortOrder,
        defaultForumLayout: defaultForumLayout,
        e2eeRequired: e2eeRequired,
        starterMessage: starterMessage ?? this.starterMessage,
        member: clearMember ? null : member ?? this.member,
        version: version ?? this.version,
      );

  /// Returns a copy with the [user] replacing the recipient whose ref equals
  /// [userRef]. Cheaper than a JSON round-trip for a single-field profile
  /// update and keeps every other channel instance untouched.
  KaedeChannel withRecipientReplaced(EntityRef userRef, KaedeUser user) =>
      KaedeChannel(
        ref: ref,
        type: type,
        position: position,
        permissions: permissions,
        createdAt: createdAt,
        guildRef: guildRef,
        name: name,
        topic: topic,
        nsfw: nsfw,
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
        bitrate: bitrate,
        userLimit: userLimit,
        rtcRegion: rtcRegion,
        videoQualityMode: videoQualityMode,
        permissionsSynced: permissionsSynced,
        historyTruncated: historyTruncated,
        historyRetention: historyRetention,
        federatedHistoryPolicy: federatedHistoryPolicy,
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
        archived: archived,
        locked: locked,
        invitable: invitable,
        autoArchiveDuration: autoArchiveDuration,
        archiveTimestamp: archiveTimestamp,
        messageCount: messageCount,
        totalMessageSent: totalMessageSent,
        memberCount: memberCount,
        flags: flags,
        appliedTagIds: appliedTagIds,
        availableTags: availableTags,
        defaultReactionEmoji: defaultReactionEmoji,
        defaultThreadRateLimitPerUser: defaultThreadRateLimitPerUser,
        defaultAutoArchiveDuration: defaultAutoArchiveDuration,
        defaultSortOrder: defaultSortOrder,
        defaultForumLayout: defaultForumLayout,
        e2eeRequired: e2eeRequired,
        starterMessage: starterMessage,
        member: member,
        version: version,
      );
}

/// One authority-advertised RTC region available to a guild voice channel.
final class VoiceRegion {
  const VoiceRegion({
    required this.id,
    required this.name,
    required this.optimal,
    required this.deprecated,
    required this.custom,
  });

  factory VoiceRegion.fromJson(Json json) => VoiceRegion(
        id: json['id']! as String,
        name: json['name']! as String,
        optimal: _boolean(json['optimal']),
        deprecated: _boolean(json['deprecated']),
        custom: _boolean(json['custom'], true),
      );

  final String id;
  final String name;
  final bool optimal;
  final bool deprecated;
  final bool custom;
}

final class ThreadPage {
  const ThreadPage({
    required this.threads,
    required this.members,
    required this.hasMore,
    this.nextCursor,
  });

  factory ThreadPage.fromJson(Json json) {
    final threads = _objects(json['threads'])
        .map(KaedeChannel.fromJson)
        .toList(growable: false);
    final byId = <String, KaedeChannel>{
      for (final thread in threads) thread.ref.id.value: thread,
    };
    final members = <ThreadMember>[];
    for (final raw in _objects(json['members'])) {
      final id = '${raw['id'] ?? raw['thread_id'] ?? ''}';
      final thread = byId[id];
      if (thread == null) continue;
      members.add(ThreadMember.fromJson(raw, thread: thread.ref));
    }
    return ThreadPage(
      threads: threads,
      members: List.unmodifiable(members),
      hasMore: _boolean(json['has_more']),
      nextCursor: _string(json['next_cursor']),
    );
  }

  final List<KaedeChannel> threads;
  final List<ThreadMember> members;
  final bool hasMore;
  final String? nextCursor;
}

enum TrackerLaneKind { backlog, planned, inProgress, completed, custom }

TrackerLaneKind trackerLaneKind(Object? value) => switch ('$value') {
      'backlog' => TrackerLaneKind.backlog,
      'planned' => TrackerLaneKind.planned,
      'in_progress' => TrackerLaneKind.inProgress,
      'completed' => TrackerLaneKind.completed,
      _ => TrackerLaneKind.custom,
    };

extension TrackerLaneKindWire on TrackerLaneKind {
  String get wire => switch (this) {
        TrackerLaneKind.inProgress => 'in_progress',
        _ => name,
      };
}

enum TrackerPriority { none, low, medium, high, urgent }

TrackerPriority trackerPriority(Object? value) =>
    TrackerPriority.values
        .where((priority) => priority.name == '$value')
        .firstOrNull ??
    TrackerPriority.none;

/// Stable high-bit tracker grants. These use [BigInt] so permission checks
/// remain exact when the mobile web build is compiled through JavaScript.
abstract final class TrackerPermission {
  static final createTasks = BigInt.one << 53;
  static final editOwnTasks = BigInt.one << 54;
  static final manageTasks = BigInt.one << 55;
  static final assignTasks = BigInt.one << 56;
  static final manageTracker = BigInt.one << 57;
}

final class TrackerLane {
  const TrackerLane({
    required this.ref,
    required this.channelRef,
    required this.name,
    required this.color,
    required this.kind,
    required this.completed,
    required this.position,
    required this.taskCount,
    required this.version,
  });

  factory TrackerLane.fromJson(Json json) => TrackerLane(
        ref: EntityRef(
          Snowflake('${json['id']}'),
          Domain('${json['origin_domain']}'),
        ),
        channelRef: EntityRef(
          Snowflake('${json['channel_id']}'),
          Domain('${json['channel_domain']}'),
        ),
        name: _string(json['name']) ?? '',
        color: _integer(json['color']).clamp(0, 0xFFFFFF),
        kind: trackerLaneKind(json['kind']),
        completed: _boolean(json['completed']),
        position: _integer(json['position']),
        taskCount: _integer(json['task_count']),
        version: _string(json['version']) ?? '',
      );

  final EntityRef ref;
  final EntityRef channelRef;
  final String name;
  final int color;
  final TrackerLaneKind kind;
  final bool completed;
  final int position;
  final int taskCount;
  final String version;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'channel_id': channelRef.id.value,
        'channel_domain': channelRef.domain.value,
        'name': name,
        'color': color,
        'kind': kind.wire,
        'completed': completed,
        'position': position,
        'task_count': taskCount,
        'version': version,
      };
}

final class TrackerTask {
  const TrackerTask({
    required this.ref,
    required this.channelRef,
    required this.laneRef,
    required this.number,
    required this.key,
    required this.title,
    required this.priority,
    required this.position,
    required this.creator,
    required this.version,
    this.description,
    this.dueAt,
    this.completedAt,
    this.assignee,
  });

  factory TrackerTask.fromJson(Json json) => TrackerTask(
        ref: EntityRef(
          Snowflake('${json['id']}'),
          Domain('${json['origin_domain']}'),
        ),
        channelRef: EntityRef(
          Snowflake('${json['channel_id']}'),
          Domain('${json['channel_domain']}'),
        ),
        laneRef: EntityRef(
          Snowflake('${json['lane_id']}'),
          Domain('${json['lane_domain']}'),
        ),
        number: _integer(json['number']),
        key: _string(json['key']) ?? '',
        title: _string(json['title']) ?? '',
        description: _string(json['description']),
        priority: trackerPriority(json['priority']),
        position: _integer(json['position']),
        dueAt: DateTime.tryParse(_string(json['due_at']) ?? '')?.toUtc(),
        completedAt:
            DateTime.tryParse(_string(json['completed_at']) ?? '')?.toUtc(),
        creator: KaedeUser.fromJson(
          Map<String, Object?>.from(json['creator']! as Map),
        ),
        assignee: json['assignee'] is Map
            ? KaedeUser.fromJson(
                Map<String, Object?>.from(json['assignee']! as Map),
              )
            : null,
        version: _string(json['version']) ?? '',
      );

  final EntityRef ref;
  final EntityRef channelRef;
  final EntityRef laneRef;
  final int number;
  final String key;
  final String title;
  final String? description;
  final TrackerPriority priority;
  final int position;
  final DateTime? dueAt;
  final DateTime? completedAt;
  final KaedeUser creator;
  final KaedeUser? assignee;
  final String version;

  bool get completed => completedAt != null;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'channel_id': channelRef.id.value,
        'channel_domain': channelRef.domain.value,
        'lane_id': laneRef.id.value,
        'lane_domain': laneRef.domain.value,
        'number': '$number',
        'key': key,
        'title': title,
        'description': description,
        'priority': priority.name,
        'position': position,
        'due_at': dueAt?.toUtc().toIso8601String(),
        'completed_at': completedAt?.toUtc().toIso8601String(),
        'creator': creator.toJson(),
        'assignee': assignee?.toJson(),
        'version': version,
      };
}

final class TrackerBoard {
  const TrackerBoard({
    required this.channelRef,
    required this.keyPrefix,
    required this.nextTaskNumber,
    required this.version,
    required this.permissions,
    required this.lanes,
    required this.tasks,
  });

  factory TrackerBoard.fromJson(Json json) {
    final lanes = _objects(json['lanes'])
        .map(TrackerLane.fromJson)
        .toList(growable: false)
      ..sort((left, right) => left.position.compareTo(right.position));
    final tasks = _objects(json['tasks'])
        .map(TrackerTask.fromJson)
        .toList(growable: false)
      ..sort((left, right) {
        final laneOrder = lanes
            .indexWhere((lane) => lane.ref == left.laneRef)
            .compareTo(lanes.indexWhere((lane) => lane.ref == right.laneRef));
        return laneOrder != 0
            ? laneOrder
            : left.position.compareTo(right.position);
      });
    return TrackerBoard(
      channelRef: EntityRef(
        Snowflake('${json['channel_id']}'),
        Domain('${json['channel_domain']}'),
      ),
      keyPrefix: _string(json['key_prefix']) ?? 'TASK',
      nextTaskNumber: _integer(json['next_task_number'], 1),
      version: _string(json['version']) ?? '',
      permissions:
          BigInt.tryParse(_string(json['permissions']) ?? '0') ?? BigInt.zero,
      lanes: List.unmodifiable(lanes),
      tasks: List.unmodifiable(tasks),
    );
  }

  final EntityRef channelRef;
  final String keyPrefix;
  final int nextTaskNumber;
  final String version;
  final BigInt permissions;
  final List<TrackerLane> lanes;
  final List<TrackerTask> tasks;

  bool allows(BigInt permission) => permissions & permission == permission;

  List<TrackerTask> tasksFor(TrackerLane lane) => List.unmodifiable(
        tasks.where((task) => task.laneRef == lane.ref).toList()
          ..sort((left, right) => left.position.compareTo(right.position)),
      );

  Json toJson() => <String, Object?>{
        'channel_id': channelRef.id.value,
        'channel_domain': channelRef.domain.value,
        'key_prefix': keyPrefix,
        'next_task_number': '$nextTaskNumber',
        'version': version,
        'permissions': permissions.toString(),
        'lanes': lanes.map((lane) => lane.toJson()).toList(),
        'tasks': tasks.map((task) => task.toJson()).toList(),
      };
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
    this.iconHash,
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
        iconHash: _string(json['icon_hash']),
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
  final String? iconHash;
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
        'icon_hash': iconHash,
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
    this.stickers = const <Json>[],
    this.emojiMaxBytes = 256 * 1024,
    this.stickerLimit = 60,
    this.stickerMaxBytes = 512 * 1024,
    this.stickerBackgroundRemovalEnabled = false,
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
      stickers: _objects(json['stickers']).toList(growable: false),
      emojiMaxBytes: _integer(json['emoji_max_bytes'], 256 * 1024),
      stickerLimit: _integer(json['sticker_limit'], 60),
      stickerMaxBytes: _integer(json['sticker_max_bytes'], 512 * 1024),
      stickerBackgroundRemovalEnabled:
          _boolean(json['sticker_background_removal_enabled']),
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
  final List<Json> stickers;
  final int emojiMaxBytes;
  final int stickerLimit;
  final int stickerMaxBytes;
  final bool stickerBackgroundRemovalEnabled;
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
        'stickers': stickers,
        'emoji_max_bytes': emojiMaxBytes,
        'sticker_limit': stickerLimit,
        'sticker_max_bytes': stickerMaxBytes,
        'sticker_background_removal_enabled': stickerBackgroundRemovalEnabled,
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
        stickers: stickers,
        emojiMaxBytes: emojiMaxBytes,
        stickerLimit: stickerLimit,
        stickerMaxBytes: stickerMaxBytes,
        stickerBackgroundRemovalEnabled: stickerBackgroundRemovalEnabled,
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
        stickers: stickers,
        emojiMaxBytes: emojiMaxBytes,
        stickerLimit: stickerLimit,
        stickerMaxBytes: stickerMaxBytes,
        stickerBackgroundRemovalEnabled: stickerBackgroundRemovalEnabled,
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
    this.privateMediaUrl,
    this.durationSecs,
    this.waveform,
    this.plaintextSha256,
    this.encryptedManifest,
  });

  factory KaedeAttachment.fromJson(
    Json json, {
    bool trustClientState = false,
  }) =>
      KaedeAttachment(
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
        privateMediaUrl: _string(json['private_media_url']),
        durationSecs: json['duration_secs'] is num
            ? (json['duration_secs']! as num).toDouble()
            : null,
        waveform: _string(json['waveform']),
        // The plaintext commitment is MLS/private-forward material. Ordinary
        // REST and Gateway attachment projections must not be able to inject
        // it into the locally authenticated presentation/cache state.
        plaintextSha256:
            trustClientState ? _string(json['plaintext_sha256']) : null,
        encryptedManifest: trustClientState && json['encrypted_manifest'] is Map
            ? Map<String, Object?>.unmodifiable(
                Map<String, Object?>.from(json['encrypted_manifest']! as Map),
              )
            : null,
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
  final String? privateMediaUrl;
  final double? durationSecs;
  final String? waveform;
  final String? plaintextSha256;

  /// Client-authenticated kaede-file-v1 material for an encrypted attachment.
  /// Network projections never populate this field; it is derived after MLS
  /// verification and retained only in the encrypted local cache.
  final Json? encryptedManifest;

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
        'private_media_url': privateMediaUrl,
        'duration_secs': durationSecs,
        'waveform': waveform,
        'plaintext_sha256': plaintextSha256,
        if (encryptedManifest != null) 'encrypted_manifest': encryptedManifest,
      };
}

final class KaedeStickerItem {
  const KaedeStickerItem({
    required this.ref,
    required this.name,
    required this.formatType,
    required this.mediaHash,
  });

  factory KaedeStickerItem.fromJson(Json json) => KaedeStickerItem(
        ref: EntityRef(
          Snowflake('${json['id']}'),
          Domain('${json['origin_domain']}'),
        ),
        name: '${json['name']}',
        formatType: _integer(json['format_type'], 1),
        mediaHash: '${json['media_hash']}',
      );

  final EntityRef ref;
  final String name;
  final int formatType;
  final String mediaHash;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'name': name,
        'format_type': formatType,
        'media_hash': mediaHash,
      };
}

/// Immutable, author-free material embedded in a forwarded message. It is not
/// a [KaedeMessage]: snapshots deliberately have no author or source channel.
final class KaedeMessageSnapshot {
  const KaedeMessageSnapshot({
    required this.content,
    required this.embeds,
    required this.components,
    required this.attachments,
    this.stickerItems = const <KaedeStickerItem>[],
    this.mentionUserRefs = const <EntityRef>[],
    this.messageSnapshots = const <KaedeMessageSnapshot>[],
    required this.messageType,
    required this.flags,
    required this.createdAt,
    this.editedAt,
  });

  factory KaedeMessageSnapshot.fromJson(
    Json json, {
    bool trustClientState = false,
  }) =>
      KaedeMessageSnapshot(
        content: _string(json['content']),
        embeds: _objects(json['embeds']).map(RichEmbed.fromJson).toList(),
        components: _objects(json['components'])
            .map(RichMessageLayout.fromJson)
            .toList(),
        attachments: _objects(json['attachments'])
            .map((item) => KaedeAttachment.fromJson(
                  item,
                  trustClientState: trustClientState,
                ))
            .toList(),
        stickerItems: _objects(json['sticker_items'])
            .map(KaedeStickerItem.fromJson)
            .toList(),
        mentionUserRefs: _objects(json['mention_user_refs'])
            .map(EntityRef.fromJson)
            .toList(),
        messageSnapshots: _objects(json['message_snapshots']).map((item) {
          final message = item['message'];
          if (message is! Map || message.keys.any((key) => key is! String)) {
            throw const FormatException(
              'Forward snapshot contains an invalid nested message.',
            );
          }
          return KaedeMessageSnapshot.fromJson(
            Map<String, Object?>.from(message),
            trustClientState: trustClientState,
          );
        }).toList(),
        messageType: (json['message_type'] as num?)?.toInt() ?? 0,
        flags: _integer(json['flags']),
        createdAt: DateTime.parse('${json['created_at']}').toUtc(),
        editedAt: _string(json['edited_at']) == null
            ? null
            : DateTime.parse('${json['edited_at']}').toUtc(),
      );

  final String? content;
  final List<RichEmbed> embeds;
  final List<RichMessageLayout> components;
  final List<KaedeAttachment> attachments;
  final List<KaedeStickerItem> stickerItems;
  final List<EntityRef> mentionUserRefs;
  final List<KaedeMessageSnapshot> messageSnapshots;
  final int messageType;
  final int flags;
  final DateTime createdAt;
  final DateTime? editedAt;

  Json toJson() => <String, Object?>{
        'content': content,
        'embeds': embeds.map((item) => item.toJson()).toList(),
        'components': components.map((item) => item.toJson()).toList(),
        'attachments': attachments.map((item) => item.toJson()).toList(),
        'sticker_items': stickerItems.map((item) => item.toJson()).toList(),
        'mention_user_refs': mentionUserRefs
            .map((item) => <String, Object?>{
                  'id': item.id.value,
                  'origin_domain': item.domain.value,
                })
            .toList(),
        'message_snapshots': messageSnapshots
            .map((item) => <String, Object?>{'message': item.toJson()})
            .toList(),
        'message_type': messageType,
        'flags': flags,
        'created_at': createdAt.toUtc().toIso8601String(),
        'edited_at': editedAt?.toUtc().toIso8601String(),
      };
}

final class KaedeInteractionMetadata {
  const KaedeInteractionMetadata({
    required this.ref,
    required this.type,
    required this.user,
    required this.userRef,
    required this.applicationRef,
    required this.integrationType,
    required this.authorizingIntegrationOwners,
    this.commandName,
    this.commandType,
    this.targetUser,
    this.targetUserRef,
    this.targetMessageRef,
    this.originalResponseMessageRef,
    this.interactedMessageRef,
    this.triggeringInteractionMetadata,
  });

  static KaedeInteractionMetadata? tryFromJson(Object? value, [int depth = 0]) {
    if (value is! Map || depth > 1) return null;
    try {
      final json = Map<String, Object?>.from(value);
      final ref = EntityRef(
        Snowflake(json['id']! as String),
        Domain(json['origin_domain']! as String),
      );
      if (json['interaction_ref'] != ref.wire) return null;
      final type = json['type'];
      if (type != 'command' && type != 'component' && type != 'modal_submit') {
        return null;
      }
      final rawUser = json['user'];
      if (rawUser is! Map) return null;
      final user = KaedeUser.fromJson(Map<String, Object?>.from(rawUser));
      final userRef = EntityRef.fromJson(json['user_ref']);
      if (user.ref != userRef) return null;
      final applicationRef = EntityRef.fromJson(json['application_ref']);
      final integrationType = json['integration_type'];
      if (integrationType != 'guild_install' &&
          integrationType != 'user_install' &&
          integrationType != 'dm_capability') {
        return null;
      }
      final rawOwners = json['authorizing_integration_owners'];
      if (rawOwners is! Map) return null;
      final owners = <String, EntityRef>{};
      for (final entry in rawOwners.entries) {
        final key = '${entry.key}';
        if (key != 'guild_install' &&
            key != 'user_install' &&
            key != 'dm_capability') {
          return null;
        }
        owners[key] = EntityRef.fromJson(entry.value);
      }
      if (!owners.containsKey(integrationType)) return null;

      final commandName = _string(json['command_name']);
      final commandType = _string(json['command_type']);
      if (type == 'command' &&
          (commandName == null ||
              commandName.trim().isEmpty ||
              !const <String>{'chat_input', 'user', 'message'}
                  .contains(commandType))) {
        return null;
      }
      final targetUser = json['target_user'] is Map
          ? KaedeUser.fromJson(
              Map<String, Object?>.from(json['target_user']! as Map),
            )
          : null;
      final targetUserRef = _entityRefOrNull(json['target_user_ref']);
      if ((targetUser == null) != (targetUserRef == null) ||
          (targetUser != null && targetUser.ref != targetUserRef)) {
        return null;
      }
      final targetMessageRef = _consistentMetadataRef(
        json,
        idKey: 'target_message_id',
        domainKey: 'target_message_domain',
        refKey: 'target_message_ref',
      );
      final originalResponseMessageRef = _consistentMetadataRef(
        json,
        idKey: 'original_response_message_id',
        domainKey: 'original_response_message_domain',
        refKey: 'original_response_message_ref',
      );
      final interactedMessageRef = _consistentMetadataRef(
        json,
        idKey: 'interacted_message_id',
        domainKey: 'interacted_message_domain',
        refKey: 'interacted_message_ref',
      );
      if (commandType == 'user' && targetUserRef == null ||
          commandType == 'message' && targetMessageRef == null ||
          type == 'component' && interactedMessageRef == null) {
        return null;
      }
      final triggering = json['triggering_interaction_metadata'] == null
          ? null
          : tryFromJson(json['triggering_interaction_metadata'], depth + 1);
      if (type == 'modal_submit' && triggering == null) return null;
      return KaedeInteractionMetadata(
        ref: ref,
        type: type as String,
        user: user,
        userRef: userRef,
        applicationRef: applicationRef,
        integrationType: integrationType as String,
        authorizingIntegrationOwners: Map.unmodifiable(owners),
        commandName: commandName,
        commandType: commandType,
        targetUser: targetUser,
        targetUserRef: targetUserRef,
        targetMessageRef: targetMessageRef,
        originalResponseMessageRef: originalResponseMessageRef,
        interactedMessageRef: interactedMessageRef,
        triggeringInteractionMetadata: triggering,
      );
    } on Object {
      return null;
    }
  }

  final EntityRef ref;
  final String type;
  final KaedeUser user;
  final EntityRef userRef;
  final EntityRef applicationRef;
  final String integrationType;
  final Map<String, EntityRef> authorizingIntegrationOwners;
  final String? commandName;
  final String? commandType;
  final KaedeUser? targetUser;
  final EntityRef? targetUserRef;
  final EntityRef? targetMessageRef;
  final EntityRef? originalResponseMessageRef;
  final EntityRef? interactedMessageRef;
  final KaedeInteractionMetadata? triggeringInteractionMetadata;

  Json toJson() => <String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'interaction_ref': ref.wire,
        'type': type,
        'user': user.toJson(),
        'user_ref': userRef.wire,
        'application_ref': applicationRef.wire,
        'integration_type': integrationType,
        'authorizing_integration_owners': authorizingIntegrationOwners.map(
          (key, value) => MapEntry(key, value.wire),
        ),
        if (commandName != null) 'command_name': commandName,
        if (commandType != null) 'command_type': commandType,
        if (targetUser != null) 'target_user': targetUser!.toJson(),
        if (targetUserRef != null) 'target_user_ref': targetUserRef!.wire,
        if (targetMessageRef != null) ...<String, Object?>{
          'target_message_id': targetMessageRef!.id.value,
          'target_message_domain': targetMessageRef!.domain.value,
          'target_message_ref': targetMessageRef!.wire,
        },
        if (originalResponseMessageRef != null) ...<String, Object?>{
          'original_response_message_id': originalResponseMessageRef!.id.value,
          'original_response_message_domain':
              originalResponseMessageRef!.domain.value,
          'original_response_message_ref': originalResponseMessageRef!.wire,
        },
        if (interactedMessageRef != null) ...<String, Object?>{
          'interacted_message_id': interactedMessageRef!.id.value,
          'interacted_message_domain': interactedMessageRef!.domain.value,
          'interacted_message_ref': interactedMessageRef!.wire,
        },
        if (triggeringInteractionMetadata != null)
          'triggering_interaction_metadata':
              triggeringInteractionMetadata!.toJson(),
      };
}

EntityRef? _consistentMetadataRef(
  Json json, {
  required String idKey,
  required String domainKey,
  required String refKey,
}) {
  final id = _string(json[idKey]);
  final domain = _string(json[domainKey]);
  final rawRef = _string(json[refKey]);
  if (id == null && domain == null && rawRef == null) return null;
  if (id == null || domain == null || rawRef == null) {
    throw const FormatException('Incomplete interaction message reference');
  }
  final ref = EntityRef(Snowflake(id), Domain(domain));
  if (rawRef != ref.wire) {
    throw const FormatException('Inconsistent interaction message reference');
  }
  return ref;
}

String? interactionAttributionText(
  KaedeInteractionMetadata? metadata, {
  required bool deleted,
}) {
  if (deleted || metadata == null) return null;
  final actor = metadata.user.name.trim();
  if (actor.isEmpty) return null;
  if (metadata.type == 'command') {
    final command = metadata.commandName?.trim();
    if (command == null || command.isEmpty) return null;
    return metadata.commandType == 'chat_input'
        ? '$actor used /$command'
        : '$actor used $command';
  }
  if (metadata.type == 'component') return '$actor used a message component';
  if (metadata.type == 'modal_submit') return '$actor submitted a form';
  return null;
}

final class KaedeMessage {
  const KaedeMessage({
    required this.ref,
    required this.channelRef,
    required this.authorRef,
    required this.createdAt,
    this.author,
    this.content,
    this.stickerItems = const <KaedeStickerItem>[],
    this.embeds = const <RichEmbed>[],
    this.components = const <RichMessageLayout>[],
    this.applicationRef,
    this.webhookRef,
    this.viewVersion = 0,
    this.viewPersistent = false,
    this.viewExpiresAt,
    this.interactionIntegrationType,
    this.interactionInstallationRef,
    this.interactionInstallationRevision,
    this.interactionMetadata,
    this.forwardedMessageRef,
    this.forwardedMessage,
    this.forwardSnapshot,
    this.decryptedForwardSnapshot,
    this.poll,
    this.pollResult,
    this.encryptedPollProjection,
    this.e2ee,
    this.e2eeVerified = false,
    this.encryptionPolicyGeneration = 0,
    this.encryptionEpoch,
    this.tts = false,
    this.messageType = 0,
    this.flags = 0,
    this.attachments = const <KaedeAttachment>[],
    this.decryptedAttachments = const <Json>[],
    this.decryptedAllowedMentions,
    this.mentionUserRefs = const <EntityRef>[],
    this.mentionRoleRefs = const <EntityRef>[],
    this.mentionEveryone = false,
    this.reference,
    this.messageReference,
    this.clientNonce,
    this.editedAt,
    this.deliveryStatus,
    this.failureReason,
    this.retryable = true,
    this.pinned = false,
    this.pinnedAt,
    this.reactionCounts = const <String, int>{},
    this.reactedEmoji = const <String>{},
    this.thread,
    this.referencedMessage,
    this.contentUnavailable = false,
    this.createdAtAvailable = true,
    this.deletedAt,
    this.historyPageComplete = false,
    this.historyPageErrorCode,
    this.historyPageRetryAfterMs,
  });

  factory KaedeMessage.fromJson(Json json) => KaedeMessage._fromJson(
        json,
        includeReferencedMessage: true,
        trustClientState: false,
      );

  /// Decodes a row written by this client's encrypted local snapshot store.
  /// Network and Gateway payloads must always use [KaedeMessage.fromJson].
  factory KaedeMessage.fromTrustedCacheJson(Json json) =>
      KaedeMessage._fromJson(
        json,
        includeReferencedMessage: true,
        trustClientState: true,
      );

  factory KaedeMessage._fromJson(
    Json json, {
    required bool includeReferencedMessage,
    required bool trustClientState,
  }) {
    final messageReference = _messageReference(json['message_reference']);
    final flattenedReferenceId = _string(json['referenced_message_id']);
    final flattenedReferenceDomain = _string(json['referenced_message_domain']);
    final flattenedReference =
        flattenedReferenceId == null || flattenedReferenceDomain == null
            ? null
            : EntityRef(
                Snowflake(flattenedReferenceId),
                Domain(flattenedReferenceDomain),
              );
    final structuredReference = _messageReferenceEntity(
      messageReference,
      'message_id',
      'message_domain',
    );
    if (flattenedReference != null &&
        structuredReference != null &&
        flattenedReference != structuredReference) {
      throw const FormatException('Inconsistent message reference.');
    }
    final author = json['author'];
    final snapshots = _objects(json['message_snapshots']);
    final snapshot = snapshots.isNotEmpty && snapshots.first['message'] is Map
        ? Map<String, Object?>.from(snapshots.first['message']! as Map)
        : null;
    final rawMessageType = json['message_type'] ?? 0;
    if (rawMessageType is! int) {
      throw const FormatException('Message type must be an integer.');
    }
    if (rawMessageType == 12 &&
        (_messageReferenceEntity(
                  messageReference,
                  'channel_id',
                  'channel_domain',
                ) ==
                null ||
            _messageReferenceEntity(
                  messageReference,
                  'guild_id',
                  'guild_domain',
                ) ==
                null)) {
      throw const FormatException(
        'Channel follow messages require qualified channel and guild references.',
      );
    }
    final pollResult = rawMessageType == 46
        ? RichPollResultMessage.fromMessageJson(json)
        : json['poll_result'] == null
            ? null
            : throw const FormatException(
                'Poll result metadata requires message type 46.',
              );
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
      stickerItems: _objects(json['sticker_items'])
          .map(KaedeStickerItem.fromJson)
          .toList(),
      embeds: _objects(json['embeds']).map(RichEmbed.fromJson).toList(),
      components:
          _objects(json['components']).map(RichMessageLayout.fromJson).toList(),
      applicationRef: _entityRefOrNull(
        json['application_id'] == null || json['application_domain'] == null
            ? null
            : '${json['application_id']}@${json['application_domain']}',
      ),
      webhookRef: _messageWebhookRef(json),
      viewVersion: _integer(json['view_version']),
      viewPersistent: _boolean(json['view_persistent']),
      viewExpiresAt: _string(json['view_expires_at']) == null
          ? null
          : DateTime.parse(json['view_expires_at']! as String).toUtc(),
      interactionIntegrationType: _string(json['interaction_integration_type']),
      interactionInstallationRef:
          _entityRefOrNull(json['interaction_installation_ref']),
      interactionInstallationRevision:
          _nullableInteger(json['interaction_installation_revision']),
      interactionMetadata: _string(json['deleted_at']) == null
          ? KaedeInteractionMetadata.tryFromJson(json['interaction_metadata'])
          : null,
      forwardedMessageRef: _entityRefOrNull(
        json['forwarded_message_ref'] ??
            (json['forwarded_message_id'] == null ||
                    json['forwarded_message_domain'] == null
                ? null
                : '${json['forwarded_message_id']}@${json['forwarded_message_domain']}'),
      ),
      forwardedMessage: json['forwarded_message'] is Map
          ? KaedeMessage._fromJson(
              Map<String, Object?>.from(json['forwarded_message']! as Map),
              includeReferencedMessage: false,
              trustClientState: trustClientState,
            )
          : null,
      forwardSnapshot: snapshot == null
          ? null
          : KaedeMessageSnapshot.fromJson(
              snapshot,
              trustClientState: trustClientState,
            ),
      decryptedForwardSnapshot:
          trustClientState && json['decrypted_forward_snapshot'] is Map
              ? Map<String, Object?>.unmodifiable(
                  Map<String, Object?>.from(
                    json['decrypted_forward_snapshot']! as Map,
                  ),
                )
              : null,
      poll: json['poll'] is Map && (json['poll']! as Map)['encrypted'] != true
          ? RichPoll.fromJson(Map<String, Object?>.from(json['poll']! as Map))
          : null,
      pollResult: pollResult,
      encryptedPollProjection:
          json['poll'] is Map && (json['poll']! as Map)['encrypted'] == true
              ? Map<String, Object?>.unmodifiable(
                  Map<String, Object?>.from(json['poll']! as Map),
                )
              : null,
      e2ee: json['e2ee'] is Map
          ? Map<String, Object?>.unmodifiable(
              Map<String, Object?>.from(json['e2ee']! as Map),
            )
          : null,
      e2eeVerified: trustClientState && _boolean(json['e2ee_verified']),
      encryptionPolicyGeneration:
          _integer(json['encryption_policy_generation']),
      encryptionEpoch: _nullableInteger(json['encryption_epoch']),
      tts: _boolean(json['tts']),
      messageType: rawMessageType,
      flags: _integer(json['flags']),
      attachments:
          _objects(json['attachments']).map(KaedeAttachment.fromJson).toList(),
      decryptedAttachments: trustClientState
          ? _objects(json['decrypted_attachments'])
          : const <Json>[],
      decryptedAllowedMentions:
          trustClientState && json['decrypted_allowed_mentions'] is Map
              ? Map<String, Object?>.unmodifiable(
                  Map<String, Object?>.from(
                    json['decrypted_allowed_mentions']! as Map,
                  ),
                )
              : null,
      mentionUserRefs: (json['mention_user_refs'] as List? ?? const <Object>[])
          .map(EntityRef.fromJson)
          .toList(),
      mentionRoleRefs: (json['mention_role_refs'] as List? ?? const <Object>[])
          .map(EntityRef.fromJson)
          .toList(),
      mentionEveryone: json['mention_everyone'] == null
          ? false
          : json['mention_everyone'] is bool
              ? json['mention_everyone']! as bool
              : throw const FormatException(
                  'Message mention_everyone must be a boolean.',
                ),
      reference: flattenedReference ?? structuredReference,
      messageReference: messageReference,
      clientNonce: _string(json['client_nonce']),
      createdAt: DateTime.parse(json['created_at']! as String).toUtc(),
      editedAt: _string(json['edited_at']) == null
          ? null
          : DateTime.parse(json['edited_at']! as String).toUtc(),
      deliveryStatus: _string(json['delivery_status']),
      failureReason: _string(json['failure_reason']),
      retryable: _boolean(json['retryable'], true),
      pinned: json['pinned'] == true || json['pinned_at'] != null,
      pinnedAt: _string(json['pinned_at']) == null
          ? null
          : DateTime.parse(json['pinned_at']! as String).toUtc(),
      reactionCounts: json['reaction_counts'] is Map
          ? canonicalReactionCounts(
              (json['reaction_counts']! as Map).map(
                (key, value) => MapEntry('$key', (value as num).toInt()),
              ),
            )
          : const <String, int>{},
      reactedEmoji: canonicalReactedEmoji(
        (json['reacted_emoji'] as List? ?? const <Object>[])
            .map((value) => '$value'),
      ),
      thread: json['thread'] is Map
          ? KaedeChannel.fromJson(
              Map<String, Object?>.from(json['thread']! as Map),
            )
          : null,
      referencedMessage:
          includeReferencedMessage && json['referenced_message'] is Map
              ? KaedeMessage._fromJson(
                  Map<String, Object?>.from(
                    json['referenced_message']! as Map,
                  ),
                  includeReferencedMessage: false,
                  trustClientState: trustClientState,
                )
              : null,
      contentUnavailable: _boolean(json['content_unavailable']),
      deletedAt: _string(json['deleted_at']) == null
          ? null
          : DateTime.parse(json['deleted_at']! as String).toUtc(),
      historyPageComplete: _boolean(json['history_page_complete']),
      historyPageErrorCode: _string(json['history_page_error_code']),
      historyPageRetryAfterMs:
          (json['history_page_retry_after_ms'] as num?)?.toInt(),
    );
  }

  /// Retained-history thread sources may intentionally omit content, author,
  /// or timestamps. Normalize only that projection; ordinary transcript
  /// messages keep the strict decoder above so malformed events still fail.
  factory KaedeMessage.fromThreadStarterJson(
    Json json, {
    required EntityRef thread,
    EntityRef? parent,
    EntityRef? owner,
  }) {
    final normalized = Map<String, Object?>.of(json);
    normalized['id'] ??= thread.id.value;
    normalized['origin_domain'] ??= thread.domain.value;
    normalized['channel_id'] ??= (parent ?? thread).id.value;
    normalized['channel_domain'] ??= (parent ?? thread).domain.value;
    final author = owner ?? thread;
    normalized['author_id'] ??= author.id.value;
    normalized['author_domain'] ??= author.domain.value;
    final rawAuthor = normalized['author'];
    if (rawAuthor is Map &&
        (rawAuthor['id'] == null ||
            rawAuthor['origin_domain'] == null ||
            rawAuthor['username'] == null)) {
      normalized.remove('author');
    }
    final rawCreatedAt = _string(normalized['created_at']);
    final createdAt =
        rawCreatedAt == null ? null : DateTime.tryParse(rawCreatedAt);
    normalized['created_at'] =
        (createdAt ?? DateTime.fromMillisecondsSinceEpoch(0, isUtc: true))
            .toUtc()
            .toIso8601String();
    final decoded = KaedeMessage.fromJson(normalized);
    return decoded.copyWith(
      contentUnavailable: decoded.contentUnavailable ||
          (decoded.referencedMessage == null &&
              decoded.content == null &&
              decoded.stickerItems.isEmpty &&
              decoded.attachments.isEmpty &&
              decoded.deletedAt == null),
      createdAtAvailable: createdAt != null,
    );
  }

  final EntityRef ref;
  final EntityRef channelRef;
  final EntityRef authorRef;
  final KaedeUser? author;
  final String? content;
  final List<KaedeStickerItem> stickerItems;
  final List<RichEmbed> embeds;
  final List<RichMessageLayout> components;
  final EntityRef? applicationRef;
  final EntityRef? webhookRef;
  final int viewVersion;
  final bool viewPersistent;
  final DateTime? viewExpiresAt;
  final String? interactionIntegrationType;
  final EntityRef? interactionInstallationRef;
  final int? interactionInstallationRevision;
  final KaedeInteractionMetadata? interactionMetadata;
  final EntityRef? forwardedMessageRef;
  final KaedeMessage? forwardedMessage;
  final KaedeMessageSnapshot? forwardSnapshot;

  /// Exact authenticated snapshot retained for secure re-forwarding. This is
  /// intentionally separate from the presentation model, which strips keys.
  final Json? decryptedForwardSnapshot;
  final RichPoll? poll;
  final RichPollResultMessage? pollResult;
  final Json? encryptedPollProjection;
  final Json? e2ee;

  /// Client-local proof that [e2ee] was authenticated and decrypted.
  ///
  /// Content-less rich messages use this marker to distinguish successful
  /// decryption from an unavailable ciphertext. It must only be set by the
  /// E2EE client after validation.
  final bool e2eeVerified;
  final int encryptionPolicyGeneration;
  final int? encryptionEpoch;
  final bool tts;
  final int messageType;
  final int flags;
  final List<KaedeAttachment> attachments;
  final List<Json> decryptedAttachments;
  final Json? decryptedAllowedMentions;
  final List<EntityRef> mentionUserRefs;
  final List<EntityRef> mentionRoleRefs;
  final bool mentionEveryone;
  final EntityRef? reference;
  final Json? messageReference;
  final String? clientNonce;
  final DateTime createdAt;
  final DateTime? editedAt;
  final String? deliveryStatus;
  final String? failureReason;
  final bool retryable;
  final bool pinned;
  final DateTime? pinnedAt;
  final Set<String> reactedEmoji;
  final Map<String, int> reactionCounts;
  final KaedeChannel? thread;
  final KaedeMessage? referencedMessage;
  final bool contentUnavailable;
  final bool createdAtAvailable;
  final DateTime? deletedAt;
  final bool historyPageComplete;
  final String? historyPageErrorCode;
  final int? historyPageRetryAfterMs;

  /// Plaintext is usable only for ordinary messages or after local E2EE
  /// authentication. Network projections cannot make encrypted content usable.
  bool get clientContentAvailable => e2ee == null || e2eeVerified;

  EntityRef? get messageReferenceChannelRef => _messageReferenceEntity(
        messageReference,
        'channel_id',
        'channel_domain',
      );

  EntityRef? get messageReferenceGuildRef => _messageReferenceEntity(
        messageReference,
        'guild_id',
        'guild_domain',
      );

  EntityRef? get followedChannelRef =>
      messageType == 12 ? messageReferenceChannelRef : null;

  EntityRef? get followedGuildRef =>
      messageType == 12 ? messageReferenceGuildRef : null;

  KaedeMessage copyWith({
    KaedeUser? author,
    String? content,
    List<KaedeStickerItem>? stickerItems,
    List<RichEmbed>? embeds,
    List<RichMessageLayout>? components,
    EntityRef? applicationRef,
    EntityRef? webhookRef,
    int? viewVersion,
    bool? viewPersistent,
    DateTime? viewExpiresAt,
    String? interactionIntegrationType,
    EntityRef? interactionInstallationRef,
    int? interactionInstallationRevision,
    KaedeInteractionMetadata? interactionMetadata,
    EntityRef? forwardedMessageRef,
    bool clearForwardedMessageRef = false,
    KaedeMessage? forwardedMessage,
    bool clearForwardedMessage = false,
    KaedeMessageSnapshot? forwardSnapshot,
    bool clearForwardSnapshot = false,
    Json? decryptedForwardSnapshot,
    bool clearDecryptedForwardSnapshot = false,
    RichPoll? poll,
    bool clearPoll = false,
    RichPollResultMessage? pollResult,
    bool clearPollResult = false,
    Json? encryptedPollProjection,
    bool clearEncryptedPollProjection = false,
    Json? e2ee,
    bool clearE2ee = false,
    bool? e2eeVerified,
    int? encryptionPolicyGeneration,
    int? encryptionEpoch,
    bool clearEncryptionEpoch = false,
    bool? tts,
    int? messageType,
    int? flags,
    bool clearContent = false,
    List<KaedeAttachment>? attachments,
    List<Json>? decryptedAttachments,
    Json? decryptedAllowedMentions,
    bool clearDecryptedAllowedMentions = false,
    List<EntityRef>? mentionUserRefs,
    List<EntityRef>? mentionRoleRefs,
    bool? mentionEveryone,
    EntityRef? reference,
    bool clearReference = false,
    Json? messageReference,
    bool clearMessageReference = false,
    String? clientNonce,
    DateTime? editedAt,
    String? deliveryStatus,
    String? failureReason,
    bool clearFailureReason = false,
    bool? retryable,
    bool? pinned,
    DateTime? pinnedAt,
    bool clearPinnedAt = false,
    Set<String>? reactedEmoji,
    Map<String, int>? reactionCounts,
    KaedeChannel? thread,
    bool clearThread = false,
    KaedeMessage? referencedMessage,
    bool clearReferencedMessage = false,
    bool? contentUnavailable,
    bool? createdAtAvailable,
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
        stickerItems: stickerItems ?? this.stickerItems,
        embeds: embeds ?? this.embeds,
        components: components ?? this.components,
        applicationRef: applicationRef ?? this.applicationRef,
        webhookRef: webhookRef ?? this.webhookRef,
        viewVersion: viewVersion ?? this.viewVersion,
        viewPersistent: viewPersistent ?? this.viewPersistent,
        viewExpiresAt: viewExpiresAt ?? this.viewExpiresAt,
        interactionIntegrationType:
            interactionIntegrationType ?? this.interactionIntegrationType,
        interactionInstallationRef:
            interactionInstallationRef ?? this.interactionInstallationRef,
        interactionInstallationRevision: interactionInstallationRevision ??
            this.interactionInstallationRevision,
        interactionMetadata: (deletedAt ?? this.deletedAt) != null
            ? null
            : interactionMetadata ?? this.interactionMetadata,
        forwardedMessageRef: clearForwardedMessageRef
            ? null
            : forwardedMessageRef ?? this.forwardedMessageRef,
        forwardedMessage: clearForwardedMessage
            ? null
            : forwardedMessage ?? this.forwardedMessage,
        forwardSnapshot: clearForwardSnapshot
            ? null
            : forwardSnapshot ?? this.forwardSnapshot,
        decryptedForwardSnapshot: clearDecryptedForwardSnapshot
            ? null
            : decryptedForwardSnapshot ?? this.decryptedForwardSnapshot,
        poll: clearPoll ? null : poll ?? this.poll,
        pollResult: clearPollResult ? null : pollResult ?? this.pollResult,
        encryptedPollProjection: clearEncryptedPollProjection
            ? null
            : encryptedPollProjection ?? this.encryptedPollProjection,
        e2ee: clearE2ee ? null : e2ee ?? this.e2ee,
        e2eeVerified: e2eeVerified ?? this.e2eeVerified,
        encryptionPolicyGeneration:
            encryptionPolicyGeneration ?? this.encryptionPolicyGeneration,
        encryptionEpoch: clearEncryptionEpoch
            ? null
            : encryptionEpoch ?? this.encryptionEpoch,
        tts: tts ?? this.tts,
        messageType: messageType ?? this.messageType,
        flags: flags ?? this.flags,
        attachments: attachments ?? this.attachments,
        decryptedAttachments: decryptedAttachments ?? this.decryptedAttachments,
        decryptedAllowedMentions: clearDecryptedAllowedMentions
            ? null
            : decryptedAllowedMentions ?? this.decryptedAllowedMentions,
        mentionUserRefs: mentionUserRefs ?? this.mentionUserRefs,
        mentionRoleRefs: mentionRoleRefs ?? this.mentionRoleRefs,
        mentionEveryone: mentionEveryone ?? this.mentionEveryone,
        reference: clearReference ? null : reference ?? this.reference,
        messageReference: clearMessageReference
            ? null
            : messageReference ?? this.messageReference,
        clientNonce: clientNonce ?? this.clientNonce,
        createdAt: createdAt,
        editedAt: editedAt ?? this.editedAt,
        deliveryStatus: deliveryStatus ?? this.deliveryStatus,
        failureReason:
            clearFailureReason ? null : failureReason ?? this.failureReason,
        retryable: retryable ?? this.retryable,
        pinned: pinned ?? this.pinned,
        pinnedAt:
            pinned == false || clearPinnedAt ? null : pinnedAt ?? this.pinnedAt,
        reactedEmoji: reactedEmoji ?? this.reactedEmoji,
        reactionCounts: reactionCounts ?? this.reactionCounts,
        thread: clearThread ? null : thread ?? this.thread,
        referencedMessage: clearReferencedMessage
            ? null
            : referencedMessage ?? this.referencedMessage,
        contentUnavailable: contentUnavailable ?? this.contentUnavailable,
        createdAtAvailable: createdAtAvailable ?? this.createdAtAvailable,
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
        'sticker_items': stickerItems.map((item) => item.toJson()).toList(),
        'embeds': embeds.map((item) => item.toJson()).toList(),
        'components': components.map((item) => item.toJson()).toList(),
        'application_id': applicationRef?.id.value,
        'application_domain': applicationRef?.domain.value,
        'webhook_id': webhookRef?.id.value,
        'webhook': webhookRef == null
            ? null
            : <String, Object?>{
                'id': webhookRef!.id.value,
                'origin_domain': webhookRef!.domain.value,
                'ref': webhookRef!.wire,
                'name': author?.name ?? 'Webhook',
                'avatar_hash': author?.avatarHash,
              },
        'view_version': viewVersion,
        'view_persistent': viewPersistent,
        'view_expires_at': viewExpiresAt?.toUtc().toIso8601String(),
        'interaction_integration_type': interactionIntegrationType,
        'interaction_installation_ref': interactionInstallationRef?.wire,
        'interaction_installation_revision':
            interactionInstallationRevision?.toString(),
        'interaction_metadata':
            deletedAt == null ? interactionMetadata?.toJson() : null,
        'forwarded_message_ref': forwardedMessageRef?.wire,
        if (forwardSnapshot != null)
          'message_snapshots': <Object?>[
            <String, Object?>{'message': forwardSnapshot!.toJson()},
          ],
        if (decryptedForwardSnapshot != null)
          'decrypted_forward_snapshot': decryptedForwardSnapshot,
        'poll': poll?.toJson() ?? encryptedPollProjection,
        'poll_result': pollResult?.toJson(),
        'e2ee': e2ee,
        'e2ee_verified': e2eeVerified,
        'encryption_policy_generation': encryptionPolicyGeneration.toString(),
        'encryption_epoch': encryptionEpoch?.toString(),
        'tts': tts,
        'message_type': messageType,
        'flags': flags,
        'attachments': attachments.map((item) => item.toJson()).toList(),
        'decrypted_attachments': decryptedAttachments,
        'decrypted_allowed_mentions': decryptedAllowedMentions,
        'mention_user_refs': mentionUserRefs.map((item) => item.wire).toList(),
        'mention_role_refs': mentionRoleRefs.map((item) => item.wire).toList(),
        'mention_everyone': mentionEveryone,
        'referenced_message_id': reference?.id.value,
        'referenced_message_domain': reference?.domain.value,
        'message_reference': messageReference,
        'client_nonce': clientNonce,
        'created_at':
            createdAtAvailable ? createdAt.toUtc().toIso8601String() : null,
        'edited_at': editedAt?.toUtc().toIso8601String(),
        'delivery_status': deliveryStatus,
        'failure_reason': failureReason,
        'retryable': retryable,
        'pinned': pinned,
        'pinned_at': pinnedAt?.toUtc().toIso8601String(),
        'reacted_emoji': reactedEmoji.toList(),
        'reaction_counts': reactionCounts,
        'thread': thread?.toJson(),
        'referenced_message': referencedMessage?.toJson(),
        'content_unavailable': contentUnavailable,
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
      this.timeoutUntil,
      this.temporary = false});

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
        temporary: json['temporary'] == true,
      );

  final KaedeUser user;
  final List<String> roleIds;
  final String? nickname;
  final DateTime? timeoutUntil;
  final bool temporary;

  Json toJson() => <String, Object?>{
        'user': user.toJson(),
        'role_ids': roleIds,
        'nickname': nickname,
        'timeout_until': timeoutUntil?.toUtc().toIso8601String(),
        'temporary': temporary,
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
    temporary: member.temporary,
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
