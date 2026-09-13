import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:kaede_mobile/src/platform/video_pip.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _MediaTrack extends Fake implements rtc.MediaStreamTrack {
  @override
  String get id => 'remote-camera';
}

class _VideoTrack extends Fake implements VideoTrack {
  @override
  rtc.MediaStreamTrack get mediaStreamTrack => _MediaTrack();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('chat.kaede.mobile/video_pip');
  testWidgets('PiP honors its saved preference and native visibility',
      (tester) async {
    SharedPreferences.setMockInitialValues({VideoPip.preferenceKey: false});
    final configurations = <MethodCall>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      configurations.add(call);
      return true;
    });
    var changes = 0;
    final pip = VideoPip(onChanged: () => changes++);
    pip.configure(_VideoTrack());
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 10)));
    expect(pip.enabled, isFalse);
    expect(configurations.last.arguments['enabled'], isFalse);
    await pip.setEnabled(true);
    await tester.pump();
    expect(configurations.last.arguments['enabled'], isTrue);
    expect(configurations.last.arguments['trackId'], 'remote-camera');
    expect(pip.active,
        isFalse); // Preference alone never counts as a visible viewer.
    await TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .handlePlatformMessage(
            channel.name,
            const StandardMethodCodec()
                .encodeMethodCall(const MethodCall('state', true)),
            (_) {});
    expect(pip.active, isTrue);
    await TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .handlePlatformMessage(
            channel.name,
            const StandardMethodCodec()
                .encodeMethodCall(const MethodCall('state', false)),
            (_) {});
    expect(pip.active, isFalse);
    await pip.setEnabled(false);
    await tester.pump();
    expect(configurations.last.arguments['enabled'], isFalse);
    expect(
        (await SharedPreferences.getInstance()).getBool(VideoPip.preferenceKey),
        isFalse);
    expect(changes, greaterThan(0));
    pip.dispose();
    await tester.pump();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });
}
