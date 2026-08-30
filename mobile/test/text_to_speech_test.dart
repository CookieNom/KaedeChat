import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/text_to_speech.dart';

void main() {
  final domain = Domain('chat.example');
  final channel = EntityRef(Snowflake('10'), domain);
  final message = KaedeMessage.fromJson(<String, Object?>{
    'id': '20',
    'origin_domain': domain.value,
    'channel_id': channel.id.value,
    'channel_domain': channel.domain.value,
    'author_id': '30',
    'author_domain': domain.value,
    'content': 'hello',
    'tts': true,
    'message_type': 0,
    'flags': 0,
    'mention_user_refs': const <String>[],
    'created_at': '2026-08-28T00:00:00Z',
  });

  test('parses only the built-in tts command', () {
    expect(parseTtsCommand('/tts hello'), (matched: true, content: 'hello'));
    expect(parseTtsCommand('/TTS   hello there'),
        (matched: true, content: 'hello there'));
    expect(parseTtsCommand('/tts'), (matched: true, content: ''));
    expect(parseTtsCommand('/ttsx hello'), (matched: false, content: ''));
  });

  test('defaults to disabled and never speaks unexpectedly', () {
    final preferences = TtsPreferences.fromSettings(const <String, Object?>{});
    expect(preferences.enabled, isFalse);
    expect(preferences.playback, TtsPlaybackMode.never);
    expect(
      shouldPlayTtsMessage(
        message,
        selectedChannel: channel,
        preferences: preferences,
      ),
      isFalse,
    );
  });

  test('current channel mode only reads the selected conversation', () {
    const preferences = TtsPreferences(
      enabled: true,
      playback: TtsPlaybackMode.current,
    );
    expect(
      shouldPlayTtsMessage(
        message,
        selectedChannel: channel,
        preferences: preferences,
      ),
      isTrue,
    );
    expect(
      shouldPlayTtsMessage(
        message,
        selectedChannel: EntityRef(Snowflake('11'), domain),
        preferences: preferences,
      ),
      isFalse,
    );
  });

  test('never speaks peer-projected plaintext before E2EE verification', () {
    const preferences = TtsPreferences(
      enabled: true,
      playback: TtsPlaybackMode.all,
    );
    final injected = message.copyWith(
      e2ee: const <String, Object?>{'ciphertext': 'opaque'},
      e2eeVerified: false,
      content: 'peer-injected plaintext',
    );

    expect(
      shouldPlayTtsMessage(
        injected,
        selectedChannel: channel,
        preferences: preferences,
      ),
      isFalse,
    );
  });

  test('settings merge preserves unrelated notification keys', () {
    const preferences = TtsPreferences(
      enabled: true,
      playback: TtsPlaybackMode.all,
      rate: 1.4,
    );
    expect(
      preferences.mergeInto(const <String, Object?>{'mentions': true}),
      containsPair('mentions', true),
    );
    expect(preferences.mergeInto(null), containsPair('tts_playback', 'all'));
  });
}
