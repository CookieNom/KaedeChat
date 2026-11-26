// CallKit audio ownership uses the vendored LiveKit audio-session API.
// ignore_for_file: experimental_member_use

import 'package:flutter/services.dart';
import 'package:livekit_client/livekit_client.dart';

enum SystemCallAction { incoming, answer, decline, ended }

final class SystemCallEvent {
  const SystemCallEvent(
    this.callId,
    this.action, {
    this.channelRef,
    this.callerName,
  });

  final String callId;
  final SystemCallAction action;
  final String? channelRef;
  final String? callerName;
}

/// Small, platform-neutral bridge to Android Telecom and iOS CallKit.
///
/// Call state remains authoritative on the Kaede home. Native call surfaces
/// only report user intent back to the controller, which performs the REST
/// transition before joining or ending media.
final class SystemCallService {
  SystemCallService() {
    _channel.setMethodCallHandler(_handleNativeAction);
  }

  static const _channel = MethodChannel('chat.kaede.mobile/system_calls');
  Future<bool> Function(SystemCallEvent)? _onAction;
  String? _accountKey;

  Future<void> start(String accountKey,
      Future<bool> Function(SystemCallEvent) onAction) async {
    _accountKey = accountKey;
    _onAction = onAction;
    try {
      await _channel.invokeMethod<void>('ready', {'accountKey': accountKey});
    } on MissingPluginException {
      // Other targets have no system call presentation.
    }
  }

  Future<void> reset() async {
    _onAction = null;
    _accountKey = null;
    try {
      await _channel.invokeMethod<void>('reset');
    } on MissingPluginException {
      // Other targets have no system call presentation.
    }
  }

  static Future<bool> showIncomingNative({
    required String callId,
    required String callerName,
    required String channelRef,
    required String accountKey,
  }) async {
    try {
      return await _channel.invokeMethod<bool>('showIncoming', {
            'callId': callId,
            'callerName': callerName,
            'channelRef': channelRef,
            'accountKey': accountKey,
          }) ??
          true;
    } on MissingPluginException {
      return false;
    } on PlatformException {
      // The operating system can refuse presentation, e.g. during an emergency.
      return true;
    }
  }

  static Future<void> deferLegacyDecline(
      SystemCallEvent event, String accountKey) async {
    await _channel.invokeMethod<void>('deferDecline', {
      'callId': event.callId,
      'accountKey': accountKey,
      'channelRef': event.channelRef,
      'callerName': event.callerName,
    });
  }

  Future<void> setActive(String callId) async {
    try {
      await _channel.invokeMethod<void>(
        'setActive',
        <String, String>{'callId': callId},
      );
    } on MissingPluginException {
      // The current platform does not expose a native call surface.
    }
  }

  Future<void> end(String callId) => endNative(callId);

  static Future<void> endNative(String callId) async {
    try {
      await _channel.invokeMethod<void>(
        'end',
        <String, String>{'callId': callId},
      );
    } on MissingPluginException {
      // The current platform does not expose a native call surface.
    }
  }

  Future<void> _audioUpdate = Future.value();
  (bool, bool)? _audioState;
  Future<bool> _handleNativeAction(MethodCall call) async {
    final args = call.arguments;
    if (call.method == 'audio' && args is Map) {
      final state = (args['external'] == true, args['active'] == true);
      if (state == _audioState || (_audioState == null && !state.$1)) {
        return true;
      }
      _audioState = state;
      _audioUpdate = _audioUpdate.then((_) async {
        final manager = AudioManager.instance;
        await manager.setAudioSessionManagementMode(state.$1
            ? AudioSessionManagementMode.externalCallSystem
            : AudioSessionManagementMode.automatic);
        await manager.setEngineAvailability(!state.$1 || state.$2
            ? AudioEngineAvailability.defaultAvailability
            : AudioEngineAvailability.none);
      }).catchError((Object _) {
        _audioState = null;
      });
      await _audioUpdate;
      return true;
    }
    final handle = _onAction;
    if (args is! Map || handle == null) return false;
    if (args['accountKey'] != null && args['accountKey'] != _accountKey) {
      return true;
    }
    final callId = args['callId'];
    if (callId is! String || callId.isEmpty) return true;
    final action = switch (call.method) {
      'answer' => SystemCallAction.answer,
      'decline' => SystemCallAction.decline,
      'ended' => SystemCallAction.ended,
      _ => null,
    };
    if (action == null) return false;
    return handle(SystemCallEvent(callId, action,
        channelRef: args['channelRef'] as String?,
        callerName: args['callerName'] as String?));
  }

  Future<void> dispose() async {
    _onAction = null;
    _channel.setMethodCallHandler(null);
  }
}
