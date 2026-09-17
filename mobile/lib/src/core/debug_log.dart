import 'dart:collection';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';

enum DebugEvent {
  appStarted,
  appReady,
  appFailed,
  firebaseStarting,
  firebaseReady,
  firebaseFailed,
  permissionRequested,
  permissionGranted,
  permissionDenied,
  tokenRequested,
  tokenReady,
  tokenPending,
  tokenFailed,
  pushRegistrationStarted,
  pushRegistrationReady,
  pushRegistrationFailed,
  apiResponse,
  apiFailed,
  gatewayConnecting,
  gatewayConnected,
  gatewayReconnecting,
  gatewayOffline,
}

/// Device-local, opt-in diagnostics. Never accepts messages, URLs or payloads.
final class DebugLog extends ChangeNotifier {
  static final instance = DebugLog();
  static const preferenceKey = 'kaede.debug-logging';
  static const capacity = 200;
  static const _native = MethodChannel('chat.kaede.mobile/push_state');
  final _entries = Queue<String>();
  bool _enabled = false;
  bool get enabled => _enabled;

  Future<void> initialize() async {
    try {
      _enabled =
          (await SharedPreferences.getInstance()).getBool(preferenceKey) ??
              false;
    } on Object {
      _enabled = false;
    }
    record(DebugEvent.appStarted);
  }

  Future<void> setEnabled(bool value) async {
    final preferences = await SharedPreferences.getInstance();
    if (!await preferences.setBool(preferenceKey, value)) {
      throw const UserInputException(
          'Could not save the debug logging setting. Try again.');
    }
    _enabled = value;
    if (!value) await clear();
    notifyListeners();
  }

  Future<void> clear() async {
    _entries.clear();
    if (Platform.isIOS) {
      try {
        await _native
            .invokeMethod<void>('clearDebugLog')
            .timeout(const Duration(seconds: 2));
      } on Object {
        /* Clearing Dart logs must work without the native bridge. */
      }
    }
  }

  void record(DebugEvent event,
      {Object? error, int? status, int? attempt, String? step}) {
    if (!_enabled) return;
    final code = switch (error) {
      KaedeException() => error.code,
      FirebaseException() => error.code,
      PlatformException() => error.code,
      DioException() => error.type.name,
      _ => null,
    };
    _entries.add([
      DateTime.now().toUtc().toIso8601String(),
      event.name,
      if (step != null) 'step=${_code(step)}',
      if (status != null) 'status=$status',
      if (attempt != null) 'attempt=$attempt',
      if (error != null) 'error=${error.runtimeType}',
      if (code != null) 'code=${_code(code)}',
      if (error is KaedeException && error.details['provider_code'] != null)
        'provider=${_code(error.details['provider_code'])}',
    ].join(' '));
    while (_entries.length > capacity) {
      _entries.removeFirst();
    }
  }

  static String _code(Object? value) => value is String &&
          RegExp(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$').hasMatch(value)
      ? value
      : 'unknown';

  String get text => _enabled ? _entries.join('\n') : '';

  Future<String> export() async {
    var version = 'unavailable';
    try {
      final info =
          await PackageInfo.fromPlatform().timeout(const Duration(seconds: 2));
      version = '${info.version} (${info.buildNumber})';
    } on Object {/* Version metadata is optional. */}
    var native = 'Not applicable';
    if (Platform.isIOS && _enabled) {
      try {
        // Native code returns only event names, timestamps, booleans and codes.
        native = await _native
                .invokeMethod<String>('debugLog')
                .timeout(const Duration(seconds: 2)) ??
            'Unavailable';
      } on Object {
        native = 'Unavailable';
      }
    }
    if (!_enabled) return 'Debug logging is off.';
    return 'Kaede debug log\nVersion: $version\nPlatform: ${Platform.operatingSystem}\n'
        'Recent app events (up to $capacity; current session):\n$text\n'
        'Native notification registration:\n$native';
  }
}
