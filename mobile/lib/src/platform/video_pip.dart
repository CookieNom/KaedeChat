import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Native PiP owns its visibility; a backgrounded app alone is not a viewer.
final class VideoPip {
  VideoPip({required this.onChanged}) {
    _channel.setMethodCallHandler((call) async {
      if (call.method == 'state') {
        active = call.arguments == true;
        onChanged();
      }
    });
    unawaited(_load());
  }

  static const _channel = MethodChannel('chat.kaede.mobile/video_pip');
  static const preferenceKey = 'voice.autoPictureInPicture';
  final VoidCallback onChanged;
  bool enabled = true;
  bool active = false;
  bool _disposed = false;
  bool _ready = false;
  VideoTrack? _track;
  String? _configuration;
  Future<void> _pending = Future.value();

  Future<void> _load() async {
    final storage = await SharedPreferences.getInstance();
    if (_disposed) return;
    enabled = storage.getBool(preferenceKey) ?? true;
    _ready = true;
    configure(_track);
    onChanged();
  }

  Future<void> setEnabled(bool value) async {
    final storage = await SharedPreferences.getInstance();
    if (!await storage.setBool(preferenceKey, value)) {
      throw StateError('Could not save picture-in-picture preference');
    }
    if (_disposed) return;
    enabled = value;
    configure(_track);
    onChanged();
  }

  void configure(VideoTrack? track) {
    _track = track;
    if (_disposed || !_ready || kIsWeb) return;
    final trackId = track?.mediaStreamTrack.id;
    final allow = enabled && trackId != null;
    final configuration = '$allow:$trackId';
    if (_configuration == configuration) return;
    _configuration = configuration;
    _pending = _pending.then((_) async {
      if (_disposed) return;
      try {
        await _channel.invokeMethod<bool>('configure', {
          'enabled': allow,
          'trackId': trackId,
        });
      } on PlatformException {
        _configuration = null;
      } on MissingPluginException {
        // Unsupported platforms simply pause incoming video in the background.
      }
    });
  }

  void dispose() {
    _disposed = true;
    _channel.setMethodCallHandler(null);
    unawaited(_pending.then((_) async {
      try {
        await _channel.invokeMethod<bool>('configure', {'enabled': false});
      } on PlatformException {
        /* Engine is stopping. */
      } on MissingPluginException {/* Unsupported platform. */}
    }));
  }
}
