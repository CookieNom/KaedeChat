import 'dart:async';

import 'package:flutter/services.dart';

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
  final _events = StreamController<SystemCallEvent>.broadcast();

  Stream<SystemCallEvent> get events => _events.stream;

  Future<void> showIncoming({
    required String callId,
    required String callerName,
  }) async {
    try {
      await _channel.invokeMethod<void>('showIncoming', <String, Object?>{
        'callId': callId,
        'callerName': callerName,
      });
    } on MissingPluginException {
      // Tests and unsupported desktop targets retain the in-app presentation.
    }
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

  Future<void> end(String callId) async {
    try {
      await _channel.invokeMethod<void>(
        'end',
        <String, String>{'callId': callId},
      );
    } on MissingPluginException {
      // The current platform does not expose a native call surface.
    }
  }

  Future<void> _handleNativeAction(MethodCall call) async {
    final arguments = call.arguments;
    if (arguments is! Map) return;
    final callId = '${arguments['callId'] ?? ''}';
    if (callId.isEmpty) return;
    final action = switch (call.method) {
      'answer' => SystemCallAction.answer,
      'decline' => SystemCallAction.decline,
      'ended' => SystemCallAction.ended,
      _ => null,
    };
    if (action != null && !_events.isClosed) {
      _events.add(SystemCallEvent(
        callId,
        action,
        channelRef: arguments['channelRef'] as String?,
        callerName: arguments['callerName'] as String?,
      ));
    }
  }

  Future<void> dispose() async {
    _channel.setMethodCallHandler(null);
    await _events.close();
  }
}
