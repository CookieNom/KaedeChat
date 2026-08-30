import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/voice/e2ee_policy.dart';

void main() {
  final firstSessionId = List<String>.filled(43, 'a').join();
  final secondSessionId = List<String>.filled(43, 'b').join();
  final channel = KaedeChannel(
    ref: EntityRef(Snowflake('2'), Domain('chat.example')),
    type: ChannelType.voice,
    position: 0,
    permissions: BigInt.zero,
    encryptionMode: 'e2ee',
    encryptionState: 'active',
    encryptionPolicyGeneration: 4,
    encryptionEpoch: 7,
  );
  final grant = <String, Object?>{
    'e2ee': true,
    'channel_id': '2',
    'channel_domain': 'chat.example',
    'encryption_policy_generation': '4',
    'encryption_epoch': '7',
    'media_protocol': 'livekit-e2ee-v1',
    'media_suite': 'AES-256-GCM',
    'media_session_id': firstSessionId,
    'media_epoch': '7',
    'bitrate': 64000,
    'user_limit': 0,
    'rtc_region': null,
    'video_quality_mode': 1,
  };

  test('rejects an old media grant after a channel epoch rotation', () {
    expect(voiceGrantMatchesChannelPolicy(grant, channel), isTrue);
    final rotated = KaedeChannel(
      ref: channel.ref,
      type: channel.type,
      position: channel.position,
      permissions: channel.permissions,
      encryptionMode: 'e2ee',
      encryptionState: 'active',
      encryptionPolicyGeneration: 5,
      encryptionEpoch: 8,
    );

    expect(voiceGrantMatchesChannelPolicy(grant, rotated), isFalse);
    expect(
      voiceGrantMatchesChannelPolicy(
        <String, Object?>{
          ...grant,
          'encryption_policy_generation': '5',
          'encryption_epoch': '8',
          'media_session_id': secondSessionId,
          'media_epoch': '8',
        },
        rotated,
      ),
      isTrue,
    );
  });

  test('rejects rekeying, wrong-room, and incomplete media contexts', () {
    final rekeying = KaedeChannel(
      ref: channel.ref,
      type: channel.type,
      position: channel.position,
      permissions: channel.permissions,
      encryptionMode: 'e2ee',
      encryptionState: 'rekeying',
      encryptionPolicyGeneration: 4,
      encryptionEpoch: 7,
    );
    expect(voiceGrantMatchesChannelPolicy(grant, rekeying), isFalse);
    expect(
      voiceGrantMatchesChannelPolicy(
        <String, Object?>{...grant, 'channel_id': '3'},
        channel,
      ),
      isFalse,
    );
    expect(
      voiceGrantMatchesChannelPolicy(
        <String, Object?>{...grant, 'media_session_id': 'short'},
        channel,
      ),
      isFalse,
    );
    expect(
      voiceGrantMatchesChannelPolicy(
        <String, Object?>{
          ...grant,
          'media_session_id': BigInt.parse(
            '1111111111111111111111111111111111111111111',
          ),
        },
        channel,
      ),
      isFalse,
    );
  });

  test('binds plaintext grants and forbids encrypted context fields', () {
    final plaintext = KaedeChannel(
      ref: channel.ref,
      type: channel.type,
      position: channel.position,
      permissions: channel.permissions,
      encryptionMode: 'plaintext',
      encryptionState: 'plaintext',
      encryptionPolicyGeneration: 0,
    );
    final plaintextGrant = <String, Object?>{
      'e2ee': false,
      'channel_id': '2',
      'channel_domain': 'chat.example',
      'bitrate': 64000,
      'user_limit': 0,
      'rtc_region': null,
      'video_quality_mode': 1,
    };

    expect(voiceGrantMatchesChannelPolicy(plaintextGrant, plaintext), isTrue);
    expect(
      voiceGrantMatchesChannelPolicy(
        <String, Object?>{...plaintextGrant, 'channel_id': '3'},
        plaintext,
      ),
      isFalse,
    );
    expect(
      voiceGrantMatchesChannelPolicy(
        <String, Object?>{
          ...plaintextGrant,
          'media_protocol': 'livekit-e2ee-v1',
        },
        plaintext,
      ),
      isFalse,
    );
    expect(
      voiceGrantMatchesChannelPolicy(
        <String, Object?>{...plaintextGrant, 'e2ee': true},
        plaintext,
      ),
      isFalse,
    );
    final missingMode = <String, Object?>{...plaintextGrant}..remove('e2ee');
    expect(voiceGrantMatchesChannelPolicy(missingMode, plaintext), isFalse);
  });

  test('binds all effective media policy fields to the channel', () {
    final configured = KaedeChannel(
      ref: channel.ref,
      type: channel.type,
      position: channel.position,
      permissions: channel.permissions,
      encryptionMode: channel.encryptionMode,
      encryptionState: channel.encryptionState,
      encryptionPolicyGeneration: channel.encryptionPolicyGeneration,
      encryptionEpoch: channel.encryptionEpoch,
      bitrate: 32000,
      userLimit: 17,
      rtcRegion: 'future-region/alpha',
      videoQualityMode: 2,
    );
    final configuredGrant = <String, Object?>{
      ...grant,
      'bitrate': 32000,
      'user_limit': 17,
      'rtc_region': 'future-region/alpha',
      'video_quality_mode': 2,
    };

    expect(
      voiceGrantMatchesChannelPolicy(configuredGrant, configured),
      isTrue,
    );
    for (final changed in <Map<String, Object?>>[
      <String, Object?>{...configuredGrant, 'bitrate': 48000},
      <String, Object?>{...configuredGrant, 'user_limit': 18},
      <String, Object?>{...configuredGrant, 'rtc_region': 'other'},
      <String, Object?>{...configuredGrant, 'video_quality_mode': 1},
    ]) {
      expect(voiceGrantMatchesChannelPolicy(changed, configured), isFalse);
    }

    final policy = voiceMediaPolicyFromGrant(configuredGrant);
    expect(policy?.bitrate, 32000);
    expect(policy?.userLimit, 17);
    expect(policy?.rtcRegion, 'future-region/alpha');
    expect(policy?.videoQualityMode, 2);
  });

  test('rejects missing, malformed, and out-of-range media policy', () {
    final missing = <String, Object?>{...grant}..remove('bitrate');
    expect(voiceMediaPolicyFromGrant(missing), isNull);
    final missingRegion = <String, Object?>{...grant}..remove('rtc_region');
    expect(voiceMediaPolicyFromGrant(missingRegion), isNull);
    expect(
      voiceMediaPolicyFromGrant(<String, Object?>{...grant, 'bitrate': 7999}),
      isNull,
    );
    expect(
      voiceMediaPolicyFromGrant(
        <String, Object?>{...grant, 'user_limit': 10000},
      )?.userLimit,
      10000,
    );
    expect(
      voiceMediaPolicyFromGrant(
        <String, Object?>{...grant, 'user_limit': 10001},
      ),
      isNull,
    );
    expect(
      voiceMediaPolicyFromGrant(<String, Object?>{...grant, 'rtc_region': ''}),
      isNull,
    );
    expect(
      voiceMediaPolicyFromGrant(
        <String, Object?>{...grant, 'rtc_region': List.filled(65, 'x').join()},
      ),
      isNull,
    );
    expect(
      voiceMediaPolicyFromGrant(
        <String, Object?>{...grant, 'video_quality_mode': 3},
      ),
      isNull,
    );
  });

  test('applies ordinary voice and Stage limits with channel type context', () {
    final highCapacityGrant = <String, Object?>{
      ...grant,
      'user_limit': 10000,
    };
    final voice = KaedeChannel(
      ref: channel.ref,
      type: ChannelType.voice,
      position: channel.position,
      permissions: channel.permissions,
      encryptionMode: channel.encryptionMode,
      encryptionState: channel.encryptionState,
      encryptionPolicyGeneration: channel.encryptionPolicyGeneration,
      encryptionEpoch: channel.encryptionEpoch,
      userLimit: 10000,
    );
    final stage = KaedeChannel(
      ref: channel.ref,
      type: ChannelType.stage,
      position: channel.position,
      permissions: channel.permissions,
      encryptionMode: channel.encryptionMode,
      encryptionState: channel.encryptionState,
      encryptionPolicyGeneration: channel.encryptionPolicyGeneration,
      encryptionEpoch: channel.encryptionEpoch,
      userLimit: 10000,
    );

    expect(voiceGrantMatchesChannelPolicy(highCapacityGrant, voice), isFalse);
    expect(voiceGrantMatchesChannelPolicy(highCapacityGrant, stage), isTrue);
  });
}
