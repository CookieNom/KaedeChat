import 'dart:async';

import 'package:flutter_tts/flutter_tts.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

enum TtsPlaybackMode { all, current, never }

final class TtsPreferences {
  const TtsPreferences({
    this.enabled = false,
    this.playback = TtsPlaybackMode.never,
    this.rate = 1,
  });

  factory TtsPreferences.fromSettings(Map<Object?, Object?>? settings) {
    final playback = switch (settings?['tts_playback']) {
      'all' => TtsPlaybackMode.all,
      'current' => TtsPlaybackMode.current,
      _ => TtsPlaybackMode.never,
    };
    final rawRate = settings?['tts_rate'];
    final rate = rawRate is num && rawRate.isFinite
        ? rawRate.toDouble().clamp(.5, 2).toDouble()
        : 1.0;
    return TtsPreferences(
      enabled: settings?['tts_enabled'] == true,
      playback: playback,
      rate: rate,
    );
  }

  final bool enabled;
  final TtsPlaybackMode playback;
  final double rate;

  TtsPreferences copyWith({
    bool? enabled,
    TtsPlaybackMode? playback,
    double? rate,
  }) =>
      TtsPreferences(
        enabled: enabled ?? this.enabled,
        playback: playback ?? this.playback,
        rate: (rate ?? this.rate).clamp(.5, 2).toDouble(),
      );

  Map<String, Object?> mergeInto(Map<Object?, Object?>? settings) =>
      <String, Object?>{
        if (settings != null)
          for (final entry in settings.entries) '${entry.key}': entry.value,
        'tts_enabled': enabled,
        'tts_playback': playback.name,
        'tts_rate': rate,
      };
}

({bool matched, String content}) parseTtsCommand(String value) {
  final match = RegExp(
    r'^/tts(?:\s+([\s\S]*))?$',
    caseSensitive: false,
    unicode: true,
  ).firstMatch(value.trim());
  return (
    matched: match != null,
    content: match?.group(1)?.trim() ?? '',
  );
}

bool shouldPlayTtsMessage(
  KaedeMessage message, {
  required EntityRef? selectedChannel,
  required TtsPreferences preferences,
}) {
  if (!preferences.enabled ||
      !message.tts ||
      !message.clientContentAvailable ||
      (message.content?.trim().isEmpty ?? true)) {
    return false;
  }
  return switch (preferences.playback) {
    TtsPlaybackMode.all => true,
    TtsPlaybackMode.current => selectedChannel == message.channelRef,
    TtsPlaybackMode.never => false,
  };
}

/// Device-local speech playback. The durable message only carries `tts`; voice
/// selection and playback policy stay on the receiving client.
final class MobileTextToSpeech {
  MobileTextToSpeech({FlutterTts? engine}) : _engine = engine ?? FlutterTts();

  final FlutterTts _engine;
  TtsPreferences _preferences = const TtsPreferences();

  TtsPreferences get preferences => _preferences;

  Future<void> applySettings(Map<Object?, Object?>? settings) async {
    _preferences = TtsPreferences.fromSettings(settings);
    // flutter_tts uses a normalized platform rate where 0.5 is approximately
    // the system's normal speed. Kaede exposes Discord-style 0.5x..2x.
    try {
      await _engine.setSpeechRate(
        (_preferences.rate * .5).clamp(.25, 1).toDouble(),
      );
    } on Object {
      // Unsupported/missing platform speech engines leave playback disabled at
      // the device boundary without changing the account preference.
    }
  }

  void speak(KaedeMessage message, {required EntityRef? selectedChannel}) {
    if (!shouldPlayTtsMessage(
      message,
      selectedChannel: selectedChannel,
      preferences: _preferences,
    )) {
      return;
    }
    final content = message.content;
    if (content == null) return;
    final author = message.author?.name;
    final spoken =
        author == null || author.isEmpty ? content : '$author said: $content';
    unawaited(_engine.speak(spoken).catchError((Object _) => null));
  }
}

final mobileTextToSpeech = MobileTextToSpeech();
