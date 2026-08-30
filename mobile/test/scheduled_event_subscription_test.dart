import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/scheduled_events.dart';

void main() {
  test('scheduled events preserve the viewer subscription projection', () {
    final event = GuildScheduledEvent.fromJson(<String, Object?>{
      'id': '95',
      'origin_domain': 'guild.example',
      'guild_id': '1',
      'guild_domain': 'guild.example',
      'channel_id': '2',
      'channel_domain': 'guild.example',
      'creator_id': '4',
      'creator_domain': 'remote.example',
      'name': 'Town hall',
      'description': null,
      'scheduled_start_time': '2027-01-03T18:00:00Z',
      'scheduled_end_time': null,
      'status': 1,
      'entity_type': 2,
      'entity_metadata': null,
      'image': null,
      'recurrence_rule': null,
      'created_at': '2027-01-01T00:00:00Z',
      'updated_at': '2027-01-01T00:00:00Z',
      'version': '1',
      'user_count': 3,
      'me_subscribed': true,
    });

    expect(event.meSubscribed, isTrue);
    expect(event.copyWith(meSubscribed: false).meSubscribed, isFalse);
    expect(event.creatorRef.wire, '4@remote.example');
  });

  test('scheduled event recurrence uses Discord frequency and weekday enums',
      () {
    final start = DateTime.utc(2027, 1, 3, 18); // Sunday.
    ScheduledEventJson recurrence(ScheduledEventRecurrencePreset preset) =>
        ScheduledEventDraft(
          name: 'Town hall',
          description: '',
          entityType: ScheduledEventEntityType.voice,
          channelRef: EntityRef.parse('2@guild.example'),
          startTime: start,
          recurrence: preset,
        ).toCreateJson()['recurrence_rule']! as ScheduledEventJson;

    expect(recurrence(ScheduledEventRecurrencePreset.weekly), <String, Object?>{
      'start': '2027-01-03T18:00:00.000Z',
      'end': null,
      'interval': 1,
      'frequency': 2,
      'by_weekday': <int>[6],
    });
    expect(
      recurrence(ScheduledEventRecurrencePreset.yearly)['frequency'],
      0,
    );
    expect(
      scheduledEventRecurrencePreset(<String, Object?>{
        'frequency': 3,
        'interval': 1,
      }),
      ScheduledEventRecurrencePreset.daily,
    );
  });
}
