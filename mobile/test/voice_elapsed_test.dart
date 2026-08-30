import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/voice/voice_elapsed.dart';

void main() {
  test('voice elapsed labels use compact minute and hour forms', () {
    expect(
      voiceElapsedLabel(
        1000,
        now: DateTime.fromMillisecondsSinceEpoch(1065 * 1000, isUtc: true),
      ),
      '1:05',
    );
    expect(
      voiceElapsedLabel(
        1000,
        now: DateTime.fromMillisecondsSinceEpoch(4661 * 1000, isUtc: true),
      ),
      '1:01:01',
    );
  });

  test('voice elapsed parsing rejects invalid epochs and clamps clock skew',
      () {
    expect(voiceStartTimeSeconds(true), isNull);
    expect(voiceStartTimeSeconds(0), isNull);
    expect(voiceStartTimeSeconds(1000), 1000);
    expect(
      voiceElapsedLabel(
        1000,
        now: DateTime.fromMillisecondsSinceEpoch(999 * 1000, isUtc: true),
      ),
      '0:00',
    );
  });
}
