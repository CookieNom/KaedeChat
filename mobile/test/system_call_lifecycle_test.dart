import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/platform/system_call_service.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:mocktail/mocktail.dart';
import 'package:sqflite/sqflite.dart';

class _Database extends Mock implements Database {}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('background call bridge works without a foreground service instance',
      () async {
    const channel = MethodChannel('chat.kaede.mobile/system_calls');
    final calls = <MethodCall>[];
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return null;
    });
    addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
    await SystemCallService.showIncomingNative(
      callId: '70@chat.example',
      callerName: 'Caller',
      channelRef: '50@chat.example',
      accountKey: 'session',
    );
    await SystemCallService.endNative('70@chat.example');
    expect(calls.map((call) => call.method), ['showIncoming', 'end']);
    expect(calls.first.arguments, {
      'callId': '70@chat.example',
      'callerName': 'Caller',
      'channelRef': '50@chat.example',
      'accountKey': 'session'
    });
    expect(calls.last.arguments, {'callId': '70@chat.example'});
  });

  test('native actions wait for the owning account and its acceptance',
      () async {
    const channel = MethodChannel('chat.kaede.mobile/system_calls');
    const codec = StandardMethodCodec();
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(channel, (_) async => null);
    final service = SystemCallService();
    addTearDown(() async {
      await service.dispose();
      messenger.setMockMethodCallHandler(channel, null);
    });
    Future<bool> answer(String accountKey) async {
      bool? result;
      await messenger.handlePlatformMessage(
          channel.name,
          codec.encodeMethodCall(MethodCall('answer', {
            'callId': '70@chat.example',
            'channelRef': '50@chat.example',
            'accountKey': accountKey,
          })), (reply) {
        result = codec.decodeEnvelope(reply!) as bool;
      });
      return result!;
    }

    final events = <SystemCallEvent>[];
    expect(await answer('first'), isFalse);
    var unlocked = false;
    await service.start('first', (event) async {
      if (!unlocked) return false;
      events.add(event);
      return true;
    });
    expect(await answer('first'), isFalse);
    expect(await answer('other'), isTrue);
    expect(events, isEmpty);
    unlocked = true;
    expect(await answer('first'), isTrue);
    expect(events.single.channelRef, '50@chat.example');
    await service.reset();
    expect(await answer('first'), isFalse);
  });

  test('call end releases native call after acceptance cleared incoming state',
      () async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    // Unit tests do not run Android's generated plugin registrant.
    AndroidFlutterLocalNotificationsPlugin.registerWith();
    final native = <MethodCall>[];
    final notifications = <MethodCall>[];
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    const callsChannel = MethodChannel('chat.kaede.mobile/system_calls');
    const notificationsChannel =
        MethodChannel('dexterous.com/flutter/local_notifications');
    messenger.setMockMethodCallHandler(callsChannel, (call) async {
      native.add(call);
      return null;
    });
    messenger.setMockMethodCallHandler(notificationsChannel, (call) async {
      notifications.add(call);
      if (call.method == 'cancel' &&
          notifications.where((item) => item.method == 'cancel').length == 1) {
        throw PlatformException(code: 'NOTIFICATION_UNAVAILABLE');
      }
      return null;
    });
    addTearDown(() {
      debugDefaultTargetPlatformOverride = null;
      messenger.setMockMethodCallHandler(callsChannel, null);
      messenger.setMockMethodCallHandler(notificationsChannel, null);
    });
    final requests = <RequestOptions>[];
    final http = Dio()
      ..interceptors.add(InterceptorsWrapper(onRequest: (options, handler) {
        requests.add(options);
        handler.resolve(Response(requestOptions: options, statusCode: 204));
      }));
    FlutterSecureStorage.setMockInitialValues({});
    final api = KaedeApiClient(vault: const SessionVault(), httpClient: http);
    await api.useTokens(SessionTokens(
      instance: Domain('chat.example'),
      userRef: EntityRef.parse('1@chat.example'),
      accessToken: 'access',
      refreshToken: 'refresh',
    ));
    final gateway = GatewayClient(tokens: (_) async => null);
    final push = PushService.test();
    final controller = MobileController(
      KaedeRepository(api),
      api,
      gateway,
      await LocalDatabase.openWithDatabase(_Database()),
      push,
    );
    addTearDown(() async {
      controller.dispose();
      await gateway.close();
      await push.dispose();
    });
    final incoming = IncomingCall(
      call: EntityRef.parse('70@chat.example'),
      channel: EntityRef.parse('50@chat.example'),
      caller: EntityRef.parse('30@chat.example'),
      callerName: 'Caller',
    );
    controller.state =
        MobileState(phase: SessionPhase.ready, incomingCall: incoming);
    await push.show(
      id: stableNotificationId(incoming.call.wire),
      kind: NotificationKind.call,
      title: incoming.callerName,
      body: 'Incoming Kaede call',
      payload: PushCallPayload(
        call: incoming.call,
        channel: incoming.channel,
        callerName: incoming.callerName,
      ).encode(),
    );
    final ring = notifications.singleWhere((call) => call.method == 'show');
    final details = ring.arguments['platformSpecifics'] as Map;
    expect(details['audioAttributesUsage'],
        AudioAttributesUsage.notificationRingtone.value);
    expect(details['additionalFlags'], contains(4)); // FLAG_INSISTENT
    expect(details['timeoutAfter'], 60000);
    const update = <String, Object?>{
      'id': '70',
      'authority_domain': 'chat.example',
    };
    await controller.answerIncomingCall();
    expect(requests.single.path, '/api/v1/calls/70@chat.example');
    expect(requests.single.data, {'action': 'accept'});
    expect(controller.currentState.pendingCallJoin, same(incoming));
    expect(controller.currentState.incomingCall, isNull);
    controller.applyCallUpdate(update);
    await Future<void>.delayed(Duration.zero);
    expect(native.map((call) => call.method), ['setActive', 'end']);
    expect(native.last.arguments, {'callId': incoming.call.wire});
    expect(notifications.where((call) => call.method == 'cancel').length, 2);

    // A background notification has no incoming-call record in this isolate.
    // Nor should a different authority with the same numeric ID clear it.
    controller.state = MobileState(incomingCall: incoming);
    controller
        .applyCallUpdate({'id': '70', 'authority_domain': 'other.example'});
    await Future<void>.delayed(Duration.zero);
    expect(controller.currentState.incomingCall, same(incoming));
    expect(native.last.arguments, {'callId': '70@other.example'});
  });
}
