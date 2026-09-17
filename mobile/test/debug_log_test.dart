import 'package:dio/dio.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/debug_log.dart';
import 'package:kaede_mobile/src/features/settings/debug_settings.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test(
      'logging is opt-in, bounded, cleared on disable, and preference persists',
      () async {
    final log = DebugLog();
    await log.initialize();
    log.record(DebugEvent.apiResponse, status: 200);
    expect(log.text, isEmpty);
    await log.setEnabled(true);
    for (var i = 0; i <= DebugLog.capacity; i++) {
      log.record(DebugEvent.tokenPending, attempt: i);
    }
    expect(log.text.split('\n'), hasLength(DebugLog.capacity));
    expect(log.text, isNot(contains('attempt=0\n')));
    expect(log.text, contains('attempt=200'));
    final restarted = DebugLog();
    await restarted.initialize();
    expect(restarted.enabled, isTrue);
    expect(restarted.text, contains('appStarted'));
    expect(restarted.text, isNot(contains('tokenPending')));
    await log.clear();
    expect(log.text, isEmpty);
    log.record(DebugEvent.apiResponse, status: 200);
    await log.setEnabled(false);
    await log.setEnabled(true);
    expect(log.text, isEmpty);
    await log.setEnabled(false);
    final disabled = DebugLog();
    await disabled.initialize();
    expect(disabled.enabled, isFalse);
  });

  test('only diagnostic metadata is retained from provider and network errors',
      () async {
    final log = DebugLog();
    await log.setEnabled(true);
    log.record(DebugEvent.tokenFailed,
        error: FirebaseException(
          plugin: 'firebase_messaging',
          code: 'apns-token-not-set',
          message: 'private-token-secret',
        ));
    log.record(DebugEvent.apiFailed,
        status: 403,
        error: DioException(
          requestOptions: RequestOptions(
            path:
                'https://private-server-secret/path?secret=private-query-secret',
            headers: {'Authorization': 'private-auth-secret'},
            data: {'message': 'private-message-secret'},
          ),
          message: 'private-exception-secret',
          error: 'private-error-secret',
          type: DioExceptionType.badResponse,
        ));
    expect(log.text, contains('code=apns-token-not-set'));
    expect(log.text, contains('code=badResponse'));
    expect(log.text, contains('status=403'));
    expect(log.text, isNot(contains('private-')));
    log.record(DebugEvent.tokenFailed,
        error: FirebaseException(
          plugin: 'firebase_messaging',
          code: 'unknown\nprivate-injected-secret',
        ));
    expect(log.text, isNot(contains('private-')));
  });
  for (final platform in [TargetPlatform.android, TargetPlatform.iOS]) {
    testWidgets('debug settings opt in, copy, clear and disable on $platform',
        (tester) async {
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      tester.view.devicePixelRatio = 1;
      final log = DebugLog.instance;
      await log.setEnabled(false);
      addTearDown(() => log.setEnabled(false));
      PackageInfo.setMockInitialValues(
          appName: 'Kaede',
          packageName: 'chat.kaede.mobile',
          version: '0.1.59',
          buildNumber: '1',
          buildSignature: 'test');
      String? copied;
      var failCopy = true;
      tester.binding.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          if (failCopy) throw PlatformException(code: 'clipboard-unavailable');
          copied = (call.arguments as Map)['text'] as String;
        }
        return null;
      });
      addTearDown(() => tester.binding.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null));
      await tester.pumpWidget(MaterialApp(
          theme: kaedeTheme().copyWith(platform: platform),
          home: const Scaffold(
              body: SingleChildScrollView(
                  padding: EdgeInsets.all(16), child: DebugSettings()))));
      expect(find.text('Copy debug log'), findsNothing);
      await tester.tap(find.text('Debug logging'));
      await tester.pumpAndSettle();
      expect(log.enabled, isTrue);
      expect(
          (await SharedPreferences.getInstance())
              .getBool(DebugLog.preferenceKey),
          isTrue);
      for (final size in [const Size(390, 844), const Size(1280, 900)]) {
        tester.view.physicalSize = size;
        await tester.pumpAndSettle();
        final copy = find.text('Copy debug log');
        final clear = find.text('Clear log');
        expect(tester.getRect(copy).right, lessThanOrEqualTo(size.width));
        expect(tester.getRect(clear).bottom, lessThanOrEqualTo(size.height));
        expect(tester.takeException(), isNull);
      }
      log.record(DebugEvent.tokenPending, attempt: 1);
      await tester.tap(find.text('Copy debug log'));
      await tester.pumpAndSettle();
      expect(find.text('Could not complete this action. Try again.'),
          findsOneWidget);
      failCopy = false;
      await tester.tap(find.text('Copy debug log'));
      await tester.pumpAndSettle();
      expect(copied, contains('Version: 0.1.59 (1)'));
      expect(copied, contains('tokenPending'));
      expect(find.text('Diagnostics copied'), findsOneWidget);
      await tester.tap(find.text('Clear log'));
      await tester.pumpAndSettle();
      expect(log.text, isEmpty);
      expect(find.text('Debug log cleared.'), findsOneWidget);
      await tester.tap(find.text('Debug logging'));
      await tester.pumpAndSettle();
      expect(log.enabled, isFalse);
      expect(find.text('Copy debug log'), findsNothing);
    });
  }
}
