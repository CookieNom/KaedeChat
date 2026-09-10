import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/chat/voice_message_recorder.dart';
import 'package:mocktail/mocktail.dart';
import 'package:record/record.dart';

class _Recorder extends Mock implements RecordPlatform {}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late _Recorder platform;
  late RecordPlatform previous;
  late Directory directory;
  late String path;
  late List<VoiceRecording> sent;
  late List<String> errors;

  setUpAll(() => registerFallbackValue(const RecordConfig()));
  setUp(() async {
    previous = RecordPlatform.instance;
    platform = _Recorder();
    RecordPlatform.instance = platform;
    directory = await Directory.systemTemp.createTemp('voice-recorder-test-');
    path = '${directory.path}/voice.m4a';
    await File(path).writeAsBytes([1, 2, 3]);
    sent = [];
    errors = [];
    when(() => platform.create(any())).thenAnswer((_) async {});
    when(() => platform.hasPermission(any())).thenAnswer((_) async => true);
    when(() => platform.isEncoderSupported(any(), AudioEncoder.aacLc))
        .thenAnswer((_) async => true);
    when(() => platform.start(any(), any(), path: any(named: 'path')))
        .thenAnswer((_) async {});
    when(() => platform.onStateChanged(any()))
        .thenAnswer((_) => const Stream<RecordState>.empty());
    when(() => platform.isRecording(any())).thenAnswer((_) async => true);
    when(() => platform.getAmplitude(any()))
        .thenAnswer((_) async => Amplitude(current: -12, max: -6));
    when(() => platform.stop(any())).thenAnswer((_) async => path);
    when(() => platform.cancel(any())).thenAnswer((_) async {});
    when(() => platform.dispose(any())).thenAnswer((_) async {});
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (_) async => directory.path,
    );
  });
  tearDown(() async {
    RecordPlatform.instance = previous;
    await directory.delete(recursive: true);
  });

  Future<void> mount(WidgetTester tester) async {
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
      body: Align(
        alignment: Alignment.bottomRight,
        child: VoiceMessageRecorder(
          enabled: true,
          busy: false,
          onRecorded: (recording) async => sent.add(recording),
          onError: errors.add,
        ),
      ),
    )));
  }

  Future<void> recordForAMoment(WidgetTester tester) async {
    await tester.pump();
    await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 350)));
    await tester.pump(const Duration(milliseconds: 150));
  }

  Future<void> settleFiles(WidgetTester tester) async {
    // File.exists and File.delete each complete outside Flutter's fake clock.
    for (var i = 0; i < 4; i++) {
      await tester.pump();
      await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 20)));
    }
    await tester.pumpAndSettle();
  }

  testWidgets('hold shows waveform; release sends audio/mp4 once',
      (tester) async {
    await mount(tester);
    final gesture = await tester
        .startGesture(tester.getCenter(find.byIcon(Icons.mic_none_rounded)));
    await recordForAMoment(tester);
    expect(find.text('Release to send · Swipe up to lock'), findsOneWidget);
    expect(find.byType(CustomPaint), findsWidgets);
    await gesture.up();
    await settleFiles(tester);
    expect(sent, hasLength(1));
    expect(sent.single.contentType, 'audio/mp4');
    expect(sent.single.waveform, isNot('AA=='));
    verify(() => platform.getAmplitude(any())).called(greaterThan(0));
    expect(errors, isEmpty);
    await tester.pumpWidget(const SizedBox());
    await settleFiles(tester);
    verify(() => platform.dispose(any())).called(1);
  });

  testWidgets('swipe up locks; stop retains draft until send', (tester) async {
    tester.view.physicalSize = const Size(320, 640);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await mount(tester);
    final gesture = await tester
        .startGesture(tester.getCenter(find.byIcon(Icons.mic_none_rounded)));
    await recordForAMoment(tester);
    await gesture.moveBy(const Offset(0, -80));
    await tester.pump();
    await gesture.up();
    await tester.pump();
    expect(sent, isEmpty);
    expect(find.text('Recording locked'), findsOneWidget);
    await tester.tap(find.byTooltip('Stop recording'));
    await settleFiles(tester);
    expect(find.text('Ready to send'), findsOneWidget);
    expect(sent, isEmpty);
    verify(() => platform.stop(any())).called(1);
    await tester.tap(find.byTooltip('Send voice message'));
    await settleFiles(tester);
    expect(sent, hasLength(1));
    expect(errors, isEmpty);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    await settleFiles(tester);
    verify(() => platform.dispose(any())).called(1);
  });

  testWidgets('drag left discards instead of sending', (tester) async {
    await mount(tester);
    final gesture = await tester
        .startGesture(tester.getCenter(find.byIcon(Icons.mic_none_rounded)));
    await recordForAMoment(tester);
    await gesture.moveBy(const Offset(-100, 0));
    await gesture.up();
    await settleFiles(tester);
    expect(sent, isEmpty);
    verify(() => platform.cancel(any())).called(1);
    await tester.pumpWidget(const SizedBox());
    await settleFiles(tester);
    verify(() => platform.dispose(any())).called(1);
  });

  testWidgets('microphone denial explains how to enable access in Settings',
      (tester) async {
    when(() => platform.hasPermission(any())).thenAnswer((_) async => false);
    await mount(tester);
    final gesture = await tester
        .startGesture(tester.getCenter(find.byIcon(Icons.mic_none_rounded)));
    await settleFiles(tester);
    await gesture.up();
    expect(errors, [
      'Microphone access was denied. Open your phone’s Settings, find Kaede, '
          'and allow microphone access to record voice messages.',
    ]);
    verifyNever(() => platform.start(any(), any(), path: any(named: 'path')));
    expect(sent, isEmpty);
    await tester.pumpWidget(const SizedBox());
    await settleFiles(tester);
  });

  testWidgets('release during permission prompt never starts recording',
      (tester) async {
    final permission = Completer<bool>();
    when(() => platform.hasPermission(any()))
        .thenAnswer((_) => permission.future);
    await mount(tester);
    final gesture = await tester
        .startGesture(tester.getCenter(find.byIcon(Icons.mic_none_rounded)));
    await tester.pump();
    await gesture.up();
    permission.complete(true);
    await settleFiles(tester);
    verifyNever(() => platform.start(any(), any(), path: any(named: 'path')));
    expect(sent, isEmpty);
    expect(find.text('Starting microphone…'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    await settleFiles(tester);
    verify(() => platform.dispose(any())).called(1);
  });
  testWidgets('locking during startup survives release and can be discarded',
      (tester) async {
    final started = Completer<void>();
    when(() => platform.start(any(), any(), path: any(named: 'path')))
        .thenAnswer((_) => started.future);
    await mount(tester);
    final gesture = await tester
        .startGesture(tester.getCenter(find.byIcon(Icons.mic_none_rounded)));
    await tester.pump();
    await gesture.moveBy(const Offset(0, -80));
    await gesture.up();
    started.complete();
    await recordForAMoment(tester);
    expect(find.text('Recording locked'), findsOneWidget);
    expect(sent, isEmpty);
    await tester.tap(find.byTooltip('Stop recording'));
    await settleFiles(tester);
    expect(find.text('Ready to send'), findsOneWidget);
    await tester.tap(find.byTooltip('Discard voice message'));
    await settleFiles(tester);
    expect(sent, isEmpty);
    expect(await tester.runAsync(() => File(path).exists()), isFalse);
    expect(errors, isEmpty);
    await tester.pumpWidget(const SizedBox());
    await settleFiles(tester);
  });

  testWidgets('leaving while permission is pending releases the recorder',
      (tester) async {
    final permission = Completer<bool>();
    when(() => platform.hasPermission(any()))
        .thenAnswer((_) => permission.future);
    await mount(tester);
    final gesture = await tester
        .startGesture(tester.getCenter(find.byIcon(Icons.mic_none_rounded)));
    await tester.pump();
    await tester.pumpWidget(const SizedBox());
    permission.complete(true);
    await gesture.up();
    await settleFiles(tester);
    verifyNever(() => platform.start(any(), any(), path: any(named: 'path')));
    verify(() => platform.dispose(any())).called(1);
    expect(sent, isEmpty);
    expect(errors, isEmpty);
  });
}
