import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/platform/voice_background_service.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:permission_handler/permission_handler.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('keeps transient background voice states joined', () {
    expect(
      resolveVoiceConnectionPhase(
        connecting: false,
        hasRoom: true,
        connectionState: ConnectionState.reconnecting,
        recoverableDisconnect: false,
      ),
      VoiceConnectionPhase.reconnecting,
    );
    expect(
      resolveVoiceConnectionPhase(
        connecting: false,
        hasRoom: true,
        connectionState: ConnectionState.disconnected,
        recoverableDisconnect: true,
      ),
      VoiceConnectionPhase.reconnecting,
    );
    expect(
      voiceDisconnectIsRecoverable(
        DisconnectReason.reconnectAttemptsExceeded,
      ),
      isTrue,
    );
  });

  test('does not auto-restore authoritative voice removals', () {
    expect(
      voiceDisconnectIsRecoverable(DisconnectReason.participantRemoved),
      isFalse,
    );
    expect(
      resolveVoiceConnectionPhase(
        connecting: false,
        hasRoom: true,
        connectionState: ConnectionState.disconnected,
        recoverableDisconnect: false,
      ),
      VoiceConnectionPhase.idle,
    );
  });

  test('retains direct-message calls during session reconciliation', () {
    final directMessage = KaedeChannel.fromJson(<String, Object?>{
      'id': '91',
      'origin_domain': 'alpha.example',
      'type': 1,
      'position': 0,
      'recipients': <Object?>[],
    });

    expect(
      findVoiceSessionChannel(
        target: directMessage.ref,
        directMessages: <KaedeChannel>[directMessage],
        guilds: const <KaedeGuild>[],
      ),
      same(directMessage),
    );
  });

  test('restores camera and screen intent independently', () async {
    final published = <String>[];

    final restored = await restoreVoiceMediaIntent(
      canStream: true,
      cameraRequested: true,
      screenRequested: true,
      publishCamera: () async => published.add('camera'),
      publishScreen: () async => published.add('screen'),
    );

    expect(published, <String>['camera', 'screen']);
    expect(restored.camera, isTrue);
    expect(restored.screen, isTrue);
    expect(restored.failed, isFalse);
  });

  test('resets only media whose reconnect publication fails', () async {
    final restored = await restoreVoiceMediaIntent(
      canStream: true,
      cameraRequested: true,
      screenRequested: true,
      publishCamera: () => Future<void>.error(StateError('camera lost')),
      publishScreen: () async {},
    );

    expect(restored.camera, isFalse);
    expect(restored.cameraFailed, isTrue);
    expect(restored.screen, isTrue);
    expect(restored.screenFailed, isFalse);

    var published = false;
    final revoked = await restoreVoiceMediaIntent(
      canStream: false,
      cameraRequested: true,
      screenRequested: true,
      publishCamera: () async {
        published = true;
      },
      publishScreen: () async {
        published = true;
      },
    );
    expect(published, isFalse);
    expect(revoked.camera, isFalse);
    expect(revoked.screen, isFalse);
  });

  test('resume starts protection without reconnecting a healthy room', () {
    expect(
      voiceCanBeginProtectedJoin(isAndroid: true, appActive: false),
      isFalse,
    );
    expect(
      voiceCanBeginProtectedJoin(isAndroid: true, appActive: true),
      isTrue,
    );
    expect(
      voiceCanBeginProtectedJoin(isAndroid: false, appActive: false),
      isTrue,
    );
    expect(
      resolveVoiceResumeAction(
        connecting: false,
        hasRoom: false,
        connectionState: null,
        recoverableDisconnect: false,
        pendingJoin: true,
      ),
      VoiceResumeAction.retryProtectedJoin,
    );
    expect(
      resolveVoiceResumeAction(
        connecting: false,
        hasRoom: true,
        connectionState: ConnectionState.connected,
        recoverableDisconnect: false,
      ),
      VoiceResumeAction.restoreConnectedMedia,
    );
    expect(
      resolveVoiceResumeAction(
        connecting: false,
        hasRoom: true,
        connectionState: ConnectionState.reconnecting,
        recoverableDisconnect: true,
      ),
      VoiceResumeAction.activateBackgroundService,
    );
    expect(
      resolveVoiceResumeAction(
        connecting: false,
        hasRoom: true,
        connectionState: ConnectionState.disconnected,
        recoverableDisconnect: true,
      ),
      VoiceResumeAction.recoverDisconnectedRoom,
    );
    expect(
      resolveVoiceResumeAction(
        connecting: true,
        hasRoom: true,
        connectionState: ConnectionState.connected,
        recoverableDisconnect: false,
      ),
      VoiceResumeAction.none,
    );
  });

  test('terminal cleanup disposes every owner without disconnect recursion',
      () async {
    final operations = <String>[];

    final errors = await disposeTerminalVoiceResources(
      stopBackgroundService: () async {
        operations.add('service');
        throw StateError('native service already stopped');
      },
      disposeEvents: () async => operations.add('events'),
      disposeRoom: () async => operations.add('room'),
    );

    expect(operations, <String>['service', 'events', 'room']);
    expect(errors, hasLength(1));
  });

  test('Bluetooth routing requires the Android Nearby Devices grant', () {
    expect(
      voiceBluetoothPermissionsGranted(const <PermissionStatus>[
        PermissionStatus.granted,
        PermissionStatus.granted,
      ]),
      isTrue,
    );
    expect(
      voiceBluetoothPermissionsGranted(const <PermissionStatus>[
        PermissionStatus.granted,
        PermissionStatus.denied,
      ]),
      isFalse,
    );
    expect(
      voiceBluetoothPermissionMessage(const <PermissionStatus>[
        PermissionStatus.permanentlyDenied,
      ]),
      contains('system settings'),
    );
    expect(
      voiceBluetoothPermissionMessage(const <PermissionStatus>[
        PermissionStatus.denied,
      ]),
      contains('Nearby devices'),
    );
  });

  test('Android voice service receives its current microphone intent',
      () async {
    const channel = MethodChannel('chat.kaede.mobile/voice_lifecycle');
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    final calls = <MethodCall>[];
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return true;
    });
    addTearDown(() {
      messenger.setMockMethodCallHandler(channel, null);
      debugDefaultTargetPlatformOverride = null;
    });

    const service = VoiceBackgroundService();
    expect(await service.setActive(true, microphone: true), isTrue);
    expect(await service.setActive(false), isTrue);

    expect(calls, hasLength(2));
    expect(calls.first.method, 'setCallActive');
    expect(calls.first.arguments, <String, Object?>{
      'active': true,
      'microphone': true,
    });
    expect(calls.last.arguments, <String, Object?>{
      'active': false,
      'microphone': false,
    });
  });
}
