import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/voice/voice_room.dart';

void main() {
  test('voice Apps control preserves the asynchronous launcher contract',
      () async {
    var calls = 0;
    Future<void> launchApps() async {
      calls += 1;
    }

    final launchAction = launchApps;
    final room = VoiceRoom(
      channel: KaedeChannel(
        ref: EntityRef.parse('2@voice.example'),
        guildRef: EntityRef.parse('1@voice.example'),
        type: ChannelType.voice,
        position: 0,
        permissions: BigInt.zero,
      ),
      onApps: launchAction,
    );

    final Future<void> Function()? preservedCallback = room.onApps;
    expect(preservedCallback, same(launchAction));
    await preservedCallback!();
    expect(calls, 1);
  });
}
