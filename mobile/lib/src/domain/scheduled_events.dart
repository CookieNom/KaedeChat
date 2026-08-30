import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

typedef ScheduledEventJson = Map<String, Object?>;

enum ScheduledEventStatus {
  scheduled(1),
  active(2),
  completed(3),
  canceled(4);

  const ScheduledEventStatus(this.wire);
  final int wire;

  static ScheduledEventStatus fromWire(Object? value) {
    final wire = value is num ? value.toInt() : int.tryParse('$value');
    return values.firstWhere(
      (candidate) => candidate.wire == wire,
      orElse: () => scheduled,
    );
  }
}

enum ScheduledEventEntityType {
  stage(1),
  voice(2),
  external(3);

  const ScheduledEventEntityType(this.wire);
  final int wire;

  static ScheduledEventEntityType fromWire(Object? value) {
    final wire = value is num ? value.toInt() : int.tryParse('$value');
    return values.firstWhere(
      (candidate) => candidate.wire == wire,
      orElse: () => external,
    );
  }
}

enum ScheduledEventRecurrencePreset {
  none,
  daily,
  weekly,
  biweekly,
  monthly,
  yearly,
}

enum ScheduledEventRecurrenceFrequency {
  yearly(0),
  monthly(1),
  weekly(2),
  daily(3);

  const ScheduledEventRecurrenceFrequency(this.wire);
  final int wire;
}

ScheduledEventRecurrencePreset scheduledEventRecurrencePreset(
  ScheduledEventJson? rule,
) {
  if (rule == null) return ScheduledEventRecurrencePreset.none;
  final frequency = (rule['frequency'] as num?)?.toInt();
  if (frequency == ScheduledEventRecurrenceFrequency.daily.wire) {
    return ScheduledEventRecurrencePreset.daily;
  }
  if (frequency == ScheduledEventRecurrenceFrequency.weekly.wire) {
    return (rule['interval'] as num?)?.toInt() == 2
        ? ScheduledEventRecurrencePreset.biweekly
        : ScheduledEventRecurrencePreset.weekly;
  }
  if (frequency == ScheduledEventRecurrenceFrequency.monthly.wire) {
    return ScheduledEventRecurrencePreset.monthly;
  }
  if (frequency == ScheduledEventRecurrenceFrequency.yearly.wire) {
    return ScheduledEventRecurrencePreset.yearly;
  }
  return ScheduledEventRecurrencePreset.none;
}

String? scheduledEventRecurrenceLabel(ScheduledEventJson? rule) =>
    switch (scheduledEventRecurrencePreset(rule)) {
      ScheduledEventRecurrencePreset.none => null,
      ScheduledEventRecurrencePreset.daily => 'Repeats daily',
      ScheduledEventRecurrencePreset.weekly => 'Repeats weekly',
      ScheduledEventRecurrencePreset.biweekly => 'Repeats every 2 weeks',
      ScheduledEventRecurrencePreset.monthly => 'Repeats monthly',
      ScheduledEventRecurrencePreset.yearly => 'Repeats yearly',
    };

final class GuildScheduledEvent {
  const GuildScheduledEvent({
    required this.ref,
    required this.guildRef,
    required this.creatorRef,
    required this.name,
    required this.startTime,
    required this.status,
    required this.entityType,
    required this.createdAt,
    required this.updatedAt,
    required this.version,
    this.channelRef,
    this.creator,
    this.description,
    this.endTime,
    this.location,
    this.imageHash,
    this.recurrenceRule,
    this.userCount = 0,
    this.meSubscribed = false,
  });

  factory GuildScheduledEvent.fromJson(ScheduledEventJson json) {
    final domain = Domain('${json['origin_domain']}');
    final guildDomain = Domain('${json['guild_domain']}');
    final channelId = json['channel_id'];
    final channelDomain = json['channel_domain'];
    final creatorJson = json['creator'];
    final metadata = json['entity_metadata'];
    return GuildScheduledEvent(
      ref: EntityRef(Snowflake('${json['id']}'), domain),
      guildRef: EntityRef(Snowflake('${json['guild_id']}'), guildDomain),
      channelRef: channelId == null || channelDomain == null
          ? null
          : EntityRef(Snowflake('$channelId'), Domain('$channelDomain')),
      creatorRef: EntityRef(
        Snowflake('${json['creator_id']}'),
        Domain('${json['creator_domain']}'),
      ),
      creator: creatorJson is Map
          ? KaedeUser.fromJson(Map<String, Object?>.from(creatorJson))
          : null,
      name: '${json['name']}',
      description: json['description'] as String?,
      startTime: DateTime.parse('${json['scheduled_start_time']}').toLocal(),
      endTime: json['scheduled_end_time'] == null
          ? null
          : DateTime.parse('${json['scheduled_end_time']}').toLocal(),
      status: ScheduledEventStatus.fromWire(json['status']),
      entityType: ScheduledEventEntityType.fromWire(json['entity_type']),
      location: metadata is Map ? metadata['location'] as String? : null,
      imageHash: json['image'] as String?,
      recurrenceRule: json['recurrence_rule'] is Map
          ? Map<String, Object?>.from(json['recurrence_rule']! as Map)
          : null,
      createdAt: DateTime.parse('${json['created_at']}').toUtc(),
      updatedAt: DateTime.parse('${json['updated_at']}').toUtc(),
      version: '${json['version']}',
      userCount: (json['user_count'] as num?)?.toInt() ?? 0,
      meSubscribed: json['me_subscribed'] == true,
    );
  }

  final EntityRef ref;
  final EntityRef guildRef;
  final EntityRef? channelRef;
  final EntityRef creatorRef;
  final KaedeUser? creator;
  final String name;
  final String? description;
  final DateTime startTime;
  final DateTime? endTime;
  final ScheduledEventStatus status;
  final ScheduledEventEntityType entityType;
  final String? location;
  final String? imageHash;
  final ScheduledEventJson? recurrenceRule;
  final DateTime createdAt;
  final DateTime updatedAt;
  final String version;
  final int userCount;
  final bool meSubscribed;

  GuildScheduledEvent copyWith({int? userCount, bool? meSubscribed}) =>
      GuildScheduledEvent(
        ref: ref,
        guildRef: guildRef,
        channelRef: channelRef,
        creatorRef: creatorRef,
        creator: creator,
        name: name,
        description: description,
        startTime: startTime,
        endTime: endTime,
        status: status,
        entityType: entityType,
        location: location,
        imageHash: imageHash,
        recurrenceRule: recurrenceRule,
        createdAt: createdAt,
        updatedAt: updatedAt,
        version: version,
        userCount: userCount ?? this.userCount,
        meSubscribed: meSubscribed ?? this.meSubscribed,
      );
}

final class ScheduledEventSubscriber {
  const ScheduledEventSubscriber({
    required this.eventRef,
    required this.user,
    required this.subscribedAt,
    this.member,
  });

  factory ScheduledEventSubscriber.fromJson(ScheduledEventJson json) {
    final user = KaedeUser.fromJson(
      Map<String, Object?>.from(json['user']! as Map),
    );
    final rawMember = json['member'];
    return ScheduledEventSubscriber(
      eventRef: EntityRef(
        Snowflake('${json['guild_scheduled_event_id']}'),
        Domain('${json['guild_scheduled_event_domain']}'),
      ),
      user: user,
      member: rawMember is Map
          ? GuildMember.fromJson(Map<String, Object?>.from(rawMember))
          : null,
      subscribedAt: DateTime.parse('${json['subscribed_at']}').toUtc(),
    );
  }

  final EntityRef eventRef;
  final KaedeUser user;
  final GuildMember? member;
  final DateTime subscribedAt;
}

final class ScheduledEventDraft {
  const ScheduledEventDraft({
    required this.name,
    required this.description,
    required this.entityType,
    required this.startTime,
    this.channelRef,
    this.location = '',
    this.endTime,
    this.recurrence = ScheduledEventRecurrencePreset.none,
  });

  factory ScheduledEventDraft.fromEvent(GuildScheduledEvent event) =>
      ScheduledEventDraft(
        name: event.name,
        description: event.description ?? '',
        entityType: event.entityType,
        channelRef: event.channelRef,
        location: event.location ?? '',
        startTime: event.startTime,
        endTime: event.endTime,
        recurrence: scheduledEventRecurrencePreset(event.recurrenceRule),
      );

  final String name;
  final String description;
  final ScheduledEventEntityType entityType;
  final EntityRef? channelRef;
  final String location;
  final DateTime startTime;
  final DateTime? endTime;
  final ScheduledEventRecurrencePreset recurrence;

  ScheduledEventJson? _recurrenceJson(DateTime start) {
    if (recurrence == ScheduledEventRecurrencePreset.none) return null;
    final utc = start.toUtc();
    final common = <String, Object?>{
      'start': utc.toIso8601String(),
      'end': null,
      'interval': 1,
    };
    return switch (recurrence) {
      ScheduledEventRecurrencePreset.none => null,
      ScheduledEventRecurrencePreset.daily => <String, Object?>{
          ...common,
          'frequency': ScheduledEventRecurrenceFrequency.daily.wire,
        },
      ScheduledEventRecurrencePreset.weekly => <String, Object?>{
          ...common,
          'frequency': ScheduledEventRecurrenceFrequency.weekly.wire,
          'by_weekday': <int>[utc.weekday - 1],
        },
      ScheduledEventRecurrencePreset.biweekly => <String, Object?>{
          ...common,
          'frequency': ScheduledEventRecurrenceFrequency.weekly.wire,
          'interval': 2,
          'by_weekday': <int>[utc.weekday - 1],
        },
      ScheduledEventRecurrencePreset.monthly => <String, Object?>{
          ...common,
          'frequency': ScheduledEventRecurrenceFrequency.monthly.wire,
        },
      ScheduledEventRecurrencePreset.yearly => <String, Object?>{
          ...common,
          'frequency': ScheduledEventRecurrenceFrequency.yearly.wire,
          'by_month': <int>[utc.month],
          'by_month_day': <int>[utc.day],
        },
    };
  }

  ScheduledEventJson toCreateJson() {
    final cleanName = name.trim();
    final cleanDescription = description.trim();
    final cleanLocation = location.trim();
    if (cleanName.isEmpty) {
      throw const UserInputException('Give the scheduled event a name.');
    }
    if (endTime != null && !endTime!.isAfter(startTime)) {
      throw const UserInputException(
        'The end time must be later than the start time.',
      );
    }
    if (entityType != ScheduledEventEntityType.external && channelRef == null) {
      throw const UserInputException(
        'Choose the channel where this event will happen.',
      );
    }
    if (entityType == ScheduledEventEntityType.external) {
      if (cleanLocation.isEmpty) {
        throw const UserInputException(
          'Add the location or link for this external event.',
        );
      }
      if (endTime == null) {
        throw const UserInputException(
          'Choose an end time for this external event.',
        );
      }
    }
    return <String, Object?>{
      'channel_id': entityType != ScheduledEventEntityType.external
          ? channelRef!.wire
          : null,
      'entity_metadata': entityType == ScheduledEventEntityType.external
          ? <String, Object?>{'location': cleanLocation}
          : null,
      'name': cleanName,
      'privacy_level': 2,
      'scheduled_start_time': startTime.toUtc().toIso8601String(),
      'scheduled_end_time': endTime?.toUtc().toIso8601String(),
      'description': cleanDescription.isEmpty ? null : cleanDescription,
      'entity_type': entityType.wire,
      'recurrence_rule': _recurrenceJson(startTime),
    };
  }

  ScheduledEventJson patchFor(GuildScheduledEvent event) {
    final next = toCreateJson();
    final previous = <String, Object?>{
      'channel_id': event.channelRef?.wire,
      'entity_metadata': event.entityType == ScheduledEventEntityType.external
          ? <String, Object?>{'location': event.location}
          : null,
      'name': event.name,
      'privacy_level': 2,
      'scheduled_start_time': event.startTime.toUtc().toIso8601String(),
      'scheduled_end_time': event.endTime?.toUtc().toIso8601String(),
      'description': event.description,
      'entity_type': event.entityType.wire,
      'recurrence_rule':
          ScheduledEventDraft.fromEvent(event)._recurrenceJson(event.startTime),
    };
    next.removeWhere((key, value) => _sameJsonValue(value, previous[key]));
    return next;
  }
}

bool _sameJsonValue(Object? left, Object? right) {
  if (left is Map && right is Map) {
    if (left.length != right.length) return false;
    return left.entries.every(
      (entry) =>
          right.containsKey(entry.key) &&
          _sameJsonValue(entry.value, right[entry.key]),
    );
  }
  if (left is List && right is List) {
    if (left.length != right.length) return false;
    for (var index = 0; index < left.length; index += 1) {
      if (!_sameJsonValue(left[index], right[index])) return false;
    }
    return true;
  }
  return left == right;
}
