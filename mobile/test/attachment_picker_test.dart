import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/shared/attachment_picker.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final channel = MethodChannel(
    'miguelruivo.flutter.plugins.filepicker',
    Platform.isLinux || Platform.isWindows || Platform.isMacOS
        ? const JSONMethodCodec()
        : const StandardMethodCodec(),
  );
  final calls = <MethodCall>[];

  setUp(() {
    calls.clear();
    FilePickerIO.registerWith();
    debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return null;
    });
  });

  tearDown(() {
    debugDefaultTargetPlatformOverride = null;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  Future<void> open(WidgetTester tester, {FileType type = FileType.any}) async {
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Scaffold(
        body: Builder(
            builder: (context) => FilledButton(
                  onPressed: () => pickAttachments(
                    context,
                    type: type,
                    allowMultiple: true,
                    withReadStream: true,
                  ),
                  child: const Text('Attach'),
                )),
      ),
    ));
    await tester.tap(find.text('Attach'));
    await tester.pumpAndSettle();
  }

  for (final size in [const Size(360, 640), const Size(1024, 768)]) {
    testWidgets('iOS attachment sources route to Photos or Files at $size',
        (tester) async {
      await tester.binding.setSurfaceSize(size);
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await open(tester);
      expect(calls, isEmpty);
      expect(find.text('Photos and videos'), findsOneWidget);
      expect(find.text('Files'), findsOneWidget);
      expect(tester.takeException(), isNull);

      await tester.tap(find.text('Photos and videos'));
      await tester.pumpAndSettle();
      expect(calls.single.method, 'media');
      expect(calls.single.arguments['allowMultipleSelection'], isTrue);

      calls.clear();
      await tester.tap(find.text('Attach'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Files'));
      await tester.pumpAndSettle();
      expect(calls.single.method, 'any');

      calls.clear();
      await tester.tap(find.text('Attach'));
      await tester.pumpAndSettle();
      Navigator.of(tester.element(find.text('Files'))).pop();
      await tester.pumpAndSettle();
      expect(calls, isEmpty);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('filtered requests retain their picker type', (tester) async {
    await open(tester, type: FileType.image);
    expect(calls.single.method, 'image');
    expect(find.text('Photos and videos'), findsNothing);
  });

  testWidgets('Android continues directly to its file picker', (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    await open(tester);
    expect(calls.single.method, 'any');
    expect(find.text('Photos and videos'), findsNothing);
  });
}
