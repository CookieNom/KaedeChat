import 'package:kaede_mobile/src/core/refs.dart';

final class StageInstance {
  const StageInstance({
    required this.ref,
    required this.guildRef,
    required this.channelRef,
    required this.topic,
    required this.privacyLevel,
    required this.discoverableDisabled,
    this.scheduledEventRef,
  });

  factory StageInstance.fromJson(Map<String, Object?> json) {
    final domain = Domain('${json['origin_domain']}');
    final guildDomain = Domain('${json['guild_domain']}');
    final channelDomain = Domain('${json['channel_domain']}');
    final eventId = json['guild_scheduled_event_id'];
    final eventDomain = json['guild_scheduled_event_domain'];
    return StageInstance(
      ref: EntityRef(Snowflake('${json['id']}'), domain),
      guildRef: EntityRef(Snowflake('${json['guild_id']}'), guildDomain),
      channelRef: EntityRef(Snowflake('${json['channel_id']}'), channelDomain),
      topic: '${json['topic']}',
      privacyLevel:
          json['privacy_level'] is int ? json['privacy_level']! as int : 2,
      discoverableDisabled: json['discoverable_disabled'] != false,
      scheduledEventRef: eventId == null || eventDomain == null
          ? null
          : EntityRef(Snowflake('$eventId'), Domain('$eventDomain')),
    );
  }

  final EntityRef ref;
  final EntityRef guildRef;
  final EntityRef channelRef;
  final String topic;
  final int privacyLevel;
  final bool discoverableDisabled;
  final EntityRef? scheduledEventRef;
}
