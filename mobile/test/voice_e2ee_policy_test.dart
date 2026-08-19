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
}
