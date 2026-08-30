import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/features/voice/soundboard_access.dart';

void main() {
  test('soundboard is available only in guild voice channels', () {
    expect(
      soundboardChannelSupported(channelType: 2, directCall: false),
      isTrue,
    );
    expect(
      soundboardChannelSupported(channelType: 13, directCall: false),
      isFalse,
    );
    expect(
      soundboardChannelSupported(channelType: 2, directCall: true),
      isFalse,
    );
  });

  test('soundboard requires a live, unsuppressed, unmuted speaker', () {
    String? reason({
      bool connected = true,
      bool canSpeak = true,
      bool selfMuted = false,
      bool selfDeafened = false,
      bool serverMuted = false,
      bool serverDeafened = false,
      bool suppressed = false,
    }) =>
        soundboardPlaybackUnavailableReason(
          connected: connected,
          canSpeak: canSpeak,
          selfMuted: selfMuted,
          selfDeafened: selfDeafened,
          serverMuted: serverMuted,
          serverDeafened: serverDeafened,
          suppressed: suppressed,
        );

    expect(reason(), isNull);
    expect(reason(connected: false), contains('Join'));
    expect(reason(canSpeak: false), contains('permission'));
    expect(reason(selfMuted: true), contains('Unmute'));
    expect(reason(selfDeafened: true), contains('Undeafen'));
    expect(reason(serverMuted: true), contains('moderator'));
    expect(reason(serverDeafened: true), contains('moderator'));
    expect(reason(suppressed: true), contains('Stage speakers'));
  });

  test('external-sound eligibility compares the full federated guild ref', () {
    final target = EntityRef.parse('1@guild.example');
    expect(
      soundboardSourceAllowed(
        targetGuildRef: target,
        sourceGuildRef: null,
        canUseExternalSounds: false,
      ),
      isTrue,
    );
    expect(
      soundboardSourceAllowed(
        targetGuildRef: target,
        sourceGuildRef: EntityRef.parse('1@guild.example'),
        canUseExternalSounds: false,
      ),
      isTrue,
    );
    expect(
      soundboardSourceAllowed(
        targetGuildRef: target,
        sourceGuildRef: EntityRef.parse('1@remote.example'),
        canUseExternalSounds: false,
      ),
      isFalse,
    );
    expect(
      soundboardSourceAllowed(
        targetGuildRef: target,
        sourceGuildRef: EntityRef.parse('1@remote.example'),
        canUseExternalSounds: true,
      ),
      isTrue,
    );
  });
}
