import 'package:kaede_mobile/src/domain/models.dart';

const int kMaxPresenceSchedules = 10;

final class PresenceDurationOption {
  const PresenceDurationOption({
    required this.minutes,
    required this.label,
  });

  final int? minutes;
  final String label;
}

const List<PresenceDurationOption> kPresenceDurationOptions =
    <PresenceDurationOption>[
  PresenceDurationOption(minutes: 15, label: 'For 15 Minutes'),
  PresenceDurationOption(minutes: 60, label: 'For 1 Hour'),
  PresenceDurationOption(minutes: 480, label: 'For 8 Hours'),
  PresenceDurationOption(minutes: 1440, label: 'For 24 Hours'),
  PresenceDurationOption(minutes: 4320, label: 'For 3 Days'),
  PresenceDurationOption(minutes: null, label: 'Forever'),
];

const List<String> kPresenceDayShort = <String>[
  'S',
  'M',
  'T',
  'W',
  'T',
  'F',
  'S',
];

const List<String> kPresenceDayNames = <String>[
  'Sun',
  'Mon',
  'Tue',
  'Wed',
  'Thu',
  'Fri',
  'Sat',
];

const List<String> kCommonTimezones = <String>[
  'UTC',
  'Pacific/Honolulu',
  'America/Anchorage',
  'America/Los_Angeles',
  'America/Denver',
  'America/Chicago',
  'America/New_York',
  'America/Sao_Paulo',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Europe/Helsinki',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Bangkok',
  'Asia/Singapore',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Seoul',
  'Australia/Sydney',
  'Pacific/Auckland',
];

PresenceStatus parsePresenceStatusName(
  Object? raw, {
  PresenceStatus fallback = PresenceStatus.online,
}) {
  final name = '$raw';
  return PresenceStatus.values.firstWhere(
    (status) => status.name == name,
    orElse: () => fallback,
  );
}

final class PresenceScheduleRule {
  const PresenceScheduleRule({
    required this.id,
    required this.name,
    required this.status,
    required this.days,
    required this.startMinute,
    required this.endMinute,
    this.enabled = true,
  });

  factory PresenceScheduleRule.fromJson(Map<String, Object?> json) {
    final rawDays = json['days'];
    final days = rawDays is List
        ? (rawDays
            .whereType<num>()
            .map((value) => value.toInt())
            .where((day) => day >= 0 && day <= 6)
            .toSet()
            .toList()
          ..sort())
        : <int>[];
    return PresenceScheduleRule(
      id: '${json['id'] ?? ''}',
      name: '${json['name'] ?? 'Schedule'}',
      status: parsePresenceStatusName(json['status']),
      days: List<int>.unmodifiable(days),
      startMinute: (json['start_minute'] as num?)?.toInt() ?? 0,
      endMinute: (json['end_minute'] as num?)?.toInt() ?? 60,
      enabled: json['enabled'] != false,
    );
  }

  final String id;
  final String name;
  final PresenceStatus status;
  final List<int> days; // 0=Sun .. 6=Sat
  final int startMinute;
  final int endMinute;
  final bool enabled;

  bool get isOvernight => endMinute < startMinute;

  bool matchesAt(int day, int minute) {
    if (!enabled || days.isEmpty || startMinute == endMinute) return false;
    if (startMinute < endMinute) {
      return days.contains(day) && minute >= startMinute && minute < endMinute;
    }
    final yesterday = (day + 6) % 7;
    return (days.contains(day) && minute >= startMinute) ||
        (days.contains(yesterday) && minute < endMinute);
  }

  PresenceScheduleRule copyWith({
    String? name,
    PresenceStatus? status,
    List<int>? days,
    int? startMinute,
    int? endMinute,
    bool? enabled,
  }) =>
      PresenceScheduleRule(
        id: id,
        name: name ?? this.name,
        status: status ?? this.status,
        days: days ?? this.days,
        startMinute: startMinute ?? this.startMinute,
        endMinute: endMinute ?? this.endMinute,
        enabled: enabled ?? this.enabled,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'id': id,
        'name': name,
        'status': status.name,
        'days': days,
        'start_minute': startMinute,
        'end_minute': endMinute,
        'enabled': enabled,
      };
}

final class PresenceControlState {
  const PresenceControlState({
    required this.effectiveStatus,
    required this.suppressNotifications,
    required this.source,
    required this.baseStatus,
    this.timedStatus,
    this.timedExpiresAt,
    this.activeScheduleId,
    this.activeScheduleName,
    this.activeScheduleStatus,
    this.activeScheduleEndsAt,
    this.skippedScheduleName,
    this.skippedScheduleUntil,
    this.timezone = 'UTC',
    this.schedules = const <PresenceScheduleRule>[],
  });

  static PresenceControlState? tryParse(Object? raw) {
    if (raw is! Map<Object?, Object?>) return null;
    final map = Map<String, Object?>.from(raw);
    final rawSchedules = map['schedules'];
    final schedules = <PresenceScheduleRule>[];
    if (rawSchedules is List) {
      for (final item in rawSchedules.whereType<Map<Object?, Object?>>()) {
        schedules.add(
          PresenceScheduleRule.fromJson(Map<String, Object?>.from(item)),
        );
      }
    }
    String? optionalString(Object? value) =>
        value is String && value.isNotEmpty ? value : null;
    DateTime? optionalDate(Object? value) =>
        value is String ? DateTime.tryParse(value)?.toLocal() : null;

    return PresenceControlState(
      effectiveStatus: parsePresenceStatusName(map['effective_status']),
      suppressNotifications: map['suppress_notifications'] == true,
      source: optionalString(map['source']) ?? 'manual',
      baseStatus: parsePresenceStatusName(map['base_status']),
      timedStatus: map['timed_status'] != null
          ? parsePresenceStatusName(map['timed_status'])
          : null,
      timedExpiresAt: optionalDate(map['timed_expires_at']),
      activeScheduleId: optionalString(map['active_schedule_id']),
      activeScheduleName: optionalString(map['active_schedule_name']),
      activeScheduleStatus: map['active_schedule_status'] != null
          ? parsePresenceStatusName(map['active_schedule_status'])
          : null,
      activeScheduleEndsAt: optionalDate(map['active_schedule_ends_at']),
      skippedScheduleName: optionalString(map['skipped_schedule_name']),
      skippedScheduleUntil: optionalDate(map['skipped_schedule_until']),
      timezone: optionalString(map['timezone']) ?? 'UTC',
      schedules: List<PresenceScheduleRule>.unmodifiable(schedules),
    );
  }

  final PresenceStatus effectiveStatus;
  final bool suppressNotifications;
  final String source; // manual | timed | schedule | override
  final PresenceStatus baseStatus;
  final PresenceStatus? timedStatus;
  final DateTime? timedExpiresAt;
  final String? activeScheduleId;
  final String? activeScheduleName;
  final PresenceStatus? activeScheduleStatus;
  final DateTime? activeScheduleEndsAt;
  final String? skippedScheduleName;
  final DateTime? skippedScheduleUntil;
  final String timezone;
  final List<PresenceScheduleRule> schedules;
}

String formatPresenceMinute(int minuteOfDay) {
  final normalized = ((minuteOfDay % 1440) + 1440) % 1440;
  final h24 = normalized ~/ 60;
  final mins = normalized % 60;
  final suffix = h24 >= 12 ? 'PM' : 'AM';
  final h12 = h24 % 12 == 0 ? 12 : h24 % 12;
  return '$h12:${mins.toString().padLeft(2, '0')} $suffix';
}

String formatPresenceDaysSummary(List<int> days) {
  final sorted = days.toSet().where((d) => d >= 0 && d <= 6).toList()..sort();
  if (sorted.length == 7) return 'Every day';
  if (sorted.length == 5 &&
      const <int>[1, 2, 3, 4, 5].every((day) => sorted.contains(day))) {
    return 'Weekdays';
  }
  if (sorted.length == 2 && sorted.contains(0) && sorted.contains(6)) {
    return 'Weekends';
  }
  return sorted.map((d) => kPresenceDayNames[d]).join(', ');
}

String formatPresenceTimestampHint(DateTime? target) {
  if (target == null) return '';
  final diffMinutes = target.difference(DateTime.now()).inMinutes;
  if (diffMinutes <= 0) return 'Ending soon';
  if (diffMinutes < 60) return '${diffMinutes}m left';
  if (diffMinutes < 20 * 60) {
    return 'Until ${formatPresenceMinute(target.hour * 60 + target.minute)}';
  }
  final days = diffMinutes ~/ 1440;
  final hours = ((diffMinutes % 1440) / 60).round();
  if (days >= 1) {
    return hours > 0 ? '${days}d ${hours}h left' : '${days}d left';
  }
  return '${(diffMinutes / 60).round()}h left';
}

String summarizePresenceSubtext(PresenceControlState? state) {
  if (state == null) return '';
  if (state.source == 'timed' && state.timedExpiresAt != null) {
    return formatPresenceTimestampHint(state.timedExpiresAt);
  }
  if (state.source == 'schedule' && state.activeScheduleName != null) {
    final until = formatPresenceTimestampHint(state.activeScheduleEndsAt);
    return until.isEmpty
        ? state.activeScheduleName!
        : '${state.activeScheduleName!} · $until';
  }
  if (state.source == 'override' && state.skippedScheduleName != null) {
    final until = formatPresenceTimestampHint(state.skippedScheduleUntil);
    return until.isEmpty
        ? 'Skipping ${state.skippedScheduleName!}'
        : 'Skipping ${state.skippedScheduleName!} · $until';
  }
  if (state.effectiveStatus == PresenceStatus.invisible &&
      state.suppressNotifications &&
      state.activeScheduleName != null) {
    return '${state.activeScheduleName!} · Alerts muted';
  }
  return '';
}

String summarizeSchedulesHint(PresenceControlState? state) {
  final enabled = (state?.schedules ?? const <PresenceScheduleRule>[])
      .where((rule) => rule.enabled)
      .toList();
  if (enabled.isEmpty) return 'Off · Set quiet hours';
  if (enabled.length == 1) {
    final only = enabled.first;
    return '${only.name} · ${formatPresenceMinute(only.startMinute)}–${formatPresenceMinute(only.endMinute)}';
  }
  return '${enabled.length} active schedules';
}
