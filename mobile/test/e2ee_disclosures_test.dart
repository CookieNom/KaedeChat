import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() => SharedPreferences.setMockInitialValues(<String, Object>{}));

  test('room warning acknowledgement is durable and account/channel scoped',
      () async {
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
    expect(
      encryptedRoomWarningKey('1@home.test', '2@room.test'),
      contains('v1'),
    );
  });

  test('voice warning names protected media and unavailable server features',
      () {
    final warning = encryptedRoomJoinWarning(EncryptedRoomKind.media);
    expect(
      warning,
      contains('microphone, camera, screen video, and screen audio'),
    );
    expect(warning, contains('Server recording and transcription'));
    expect(warning, contains('safety number'));
  });

  test(
      'message warning discloses search, scanning, metadata, and recovery loss',
      () {
    final warning = encryptedRoomJoinWarning(EncryptedRoomKind.messages);
    expect(warning, contains('Server message search'));
    expect(warning, contains('file previews and scanning'));
    expect(warning, contains('traffic metadata'));
    expect(warning, contains('permanently loses encrypted history'));
  });
}
