import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() => SharedPreferences.setMockInitialValues(<String, Object>{}));

  test('room warning acknowledgement is account/channel scoped', () async {
    await acknowledgeEncryptedRoom('1@home.test', '2@room.test');

    expect(
      await hasAcknowledgedEncryptedRoom('1@home.test', '2@room.test'),
      isTrue,
    );
    expect(
      await hasAcknowledgedEncryptedRoom('1@home.test', '3@room.test'),
      isFalse,
    );
    expect(
      await hasAcknowledgedEncryptedRoom('4@home.test', '2@room.test'),
      isFalse,
    );
  });

  test('voice warning names protected media and unavailable server features',
      () {
    final warning = encryptedRoomJoinWarning(EncryptedRoomKind.media);
    for (final media in [
      'microphone',
      'camera',
      'screen video',
      'screen audio'
    ]) {
      expect(warning, contains(media));
    }
    expect(
        warning,
        matches(RegExp(r'server.*recording.*transcription.*unavailable',
            caseSensitive: false)));
    expect(warning, contains('safety number'));
  });

  test(
      'message warning discloses search, scanning, metadata, and recovery loss',
      () {
    final warning = encryptedRoomJoinWarning(EncryptedRoomKind.messages);
    expect(warning, matches(RegExp(r'server.*search', caseSensitive: false)));
    expect(warning,
        matches(RegExp(r'file previews.*scanning', caseSensitive: false)));
    expect(warning, contains('traffic metadata'));
    expect(
        warning,
        matches(RegExp(r'permanent.*(los|loss).*encrypted history',
            caseSensitive: false)));
  });
}
