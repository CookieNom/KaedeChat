import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

const messageFlagCrossposted = 1;
const messageFlagIsCrosspost = 2;

final class AnnouncementFollow {
  const AnnouncementFollow({
    required this.id,
    required this.ref,
    required this.sourceChannel,
    required this.targetChannel,
    required this.creator,
    required this.active,
    required this.federated,
    required this.createdAt,
    required this.updatedAt,
    this.generation,
  });

  factory AnnouncementFollow.fromJson(
    Map<String, Object?> json, {
    EntityRef? expectedSource,
    EntityRef? expectedTarget,
  }) {
    const fields = <String>{
      'id',
      'ref',
      'source_channel_id',
      'source_channel_domain',
      'target_channel_id',
      'target_channel_domain',
      'creator_id',
      'creator_domain',
      'active',
      'federated',
      'generation',
      'lifecycle_state',
      'name',
      'avatar_hash',
      'created_at',
      'updated_at',
    };
    if (json.keys.toSet().difference(fields).isNotEmpty ||
        fields.difference(json.keys.toSet()).isNotEmpty) {
      throw const FormatException('Announcement follow shape is invalid.');
    }
    String requiredString(String key) {
      final value = json[key];
      if (value is! String || value.isEmpty) {
        throw FormatException('Announcement follow has an invalid $key.');
      }
      return value;
    }

    final id = Snowflake(requiredString('id'));
    final ref = EntityRef.parse(requiredString('ref'));
    final source = EntityRef(
      Snowflake(requiredString('source_channel_id')),
      Domain(requiredString('source_channel_domain')),
    );
    final target = EntityRef(
      Snowflake(requiredString('target_channel_id')),
      Domain(requiredString('target_channel_domain')),
    );
    final creator = EntityRef(
      Snowflake(requiredString('creator_id')),
      Domain(requiredString('creator_domain')),
    );
    final active = json['active'];
    final federated = json['federated'];
    final generation = json['generation'];
    final lifecycle = json['lifecycle_state'];
    final name = json['name'];
    final avatarHash = json['avatar_hash'];
    final createdAt = _announcementTimestamp(json['created_at']);
    final updatedAt = _announcementTimestamp(json['updated_at']);
    final parsedGeneration = generation is String &&
            RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(generation)
        ? BigInt.tryParse(generation)
        : null;
    final validGeneration = parsedGeneration != null &&
        parsedGeneration <= BigInt.parse('9223372036854775807');
    if (active is! bool ||
        federated is! bool ||
        lifecycle is! String ||
        !const <String>{'pending', 'accepted', 'active', 'revoked'}
            .contains(lifecycle) ||
        active != (lifecycle == 'active') ||
        federated != (source.domain != target.domain) ||
        (federated ? !validGeneration : generation != null) ||
        (name != null && name is! String) ||
        (avatarHash != null && avatarHash is! String) ||
        ref.id != id ||
        ref.domain != target.domain ||
        expectedSource != null && source != expectedSource ||
        expectedTarget != null && target != expectedTarget ||
        updatedAt.isBefore(createdAt)) {
      throw const FormatException('Announcement follow lineage is invalid.');
    }
    return AnnouncementFollow(
      id: id.value,
      ref: ref,
      sourceChannel: source,
      targetChannel: target,
      creator: creator,
      active: active,
      federated: federated,
      generation: generation as String?,
      createdAt: createdAt,
      updatedAt: updatedAt,
    );
  }

  final String id;
  final EntityRef ref;
  final EntityRef sourceChannel;
  final EntityRef targetChannel;
  final EntityRef creator;
  final bool active;
  final bool federated;
  final String? generation;
  final DateTime createdAt;
  final DateTime updatedAt;
}

DateTime _announcementTimestamp(Object? value) {
  if (value is! String ||
      !(value.endsWith('Z') ||
          RegExp(r'[+-][0-9]{2}:[0-9]{2}$').hasMatch(value))) {
    throw const FormatException('Announcement follow timestamp is invalid.');
  }
  final parsed = DateTime.tryParse(value);
  if (parsed == null) {
    throw const FormatException('Announcement follow timestamp is invalid.');
  }
  return parsed.toUtc();
}

final class AnnouncementTarget {
  const AnnouncementTarget({
    required this.guild,
    required this.channel,
  });

  final KaedeGuild guild;
  final KaedeChannel channel;

  EntityRef get ref => channel.ref;
  String get label => '${guild.name} · #${channel.name ?? 'channel'}';
}

bool _allows(
  KaedeGuild guild,
  KaedeChannel channel,
  KaedeUser? actor,
  int permission,
) =>
    (actor != null && actor.ref == guild.ownerRef) ||
    channel.allows(Permission.administrator) ||
    channel.allows(permission);

bool canReadAnnouncementChannel(
  KaedeGuild guild,
  KaedeChannel channel,
  KaedeUser? actor,
) =>
    channel.type == ChannelType.announcement &&
    _allows(guild, channel, actor, Permission.viewChannel);

bool canManageAnnouncementTarget(
  KaedeGuild guild,
  KaedeChannel channel,
  KaedeUser? actor,
) =>
    channel.type == ChannelType.text &&
    _allows(guild, channel, actor, Permission.manageWebhooks);

List<AnnouncementTarget> announcementTargets(
  Iterable<KaedeGuild> guilds,
  KaedeUser? actor,
) {
  final targets = <EntityRef, AnnouncementTarget>{};
  for (final guild in guilds) {
    for (final channel in guild.channels) {
      if (channel.encryptionMode != 'e2ee' &&
          !channel.e2eeRequired &&
          canManageAnnouncementTarget(guild, channel, actor)) {
        targets[channel.ref] = AnnouncementTarget(
          guild: guild,
          channel: channel,
        );
      }
    }
  }
  final ordered = targets.values.toList(growable: false);
  ordered.sort((left, right) {
    final guildOrder = left.guild.name.compareTo(right.guild.name);
    if (guildOrder != 0) return guildOrder;
    final positionOrder = left.channel.position.compareTo(
      right.channel.position,
    );
    if (positionOrder != 0) return positionOrder;
    return left.label.compareTo(right.label);
  });
  return ordered;
}

bool canDeleteAnnouncementFollow(
  AnnouncementFollow follow,
  Iterable<KaedeGuild> guilds,
  KaedeUser? actor,
) =>
    guilds.any(
      (guild) => guild.channels.any(
        (channel) =>
            channel.ref == follow.targetChannel &&
            canManageAnnouncementTarget(guild, channel, actor),
      ),
    );

bool isPublishedAnnouncement(KaedeMessage message) =>
    message.flags & messageFlagCrossposted != 0;

/// Matches the human crosspost endpoint, including remote guild authority.
bool canPublishAnnouncementMessage(
  KaedeGuild guild,
  KaedeChannel channel,
  KaedeMessage message,
  KaedeUser? actor,
) {
  if (actor == null ||
      channel.type != ChannelType.announcement ||
      message.channelRef != channel.ref ||
      channel.encryptionMode == 'e2ee' ||
      message.e2ee != null ||
      message.deletedAt != null ||
      message.deliveryStatus == 'failed' ||
      message.ref.id.value.startsWith('pending-') ||
      isPublishedAnnouncement(message) ||
      !_allows(guild, channel, actor, Permission.sendMessages)) {
    return false;
  }
  return message.authorRef == actor.ref ||
      _allows(guild, channel, actor, Permission.manageMessages);
}
