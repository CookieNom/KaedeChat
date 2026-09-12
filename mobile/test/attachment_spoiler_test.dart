import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/chat/attachment_spoiler.dart';

void main() {
  test('spoiler filenames round trip and retain extensions at the size limit',
      () {
    expect(spoilerFilename('photo.png', true), 'SPOILER_photo.png');
    expect(spoilerFilename('SPOILER_photo.png', true), 'SPOILER_photo.png');
    expect(spoilerFilename('SPOILER_SPOILER_photo.png', false), 'photo.png');
    final long = spoilerFilename('${'a' * 251}.png', true);
    expect(long.length, 255);
    expect(long.endsWith('.png'), isTrue);
    expect(isAttachmentSpoiler(long), isTrue);
  });

  testWidgets('spoiler media is not built or exposed until explicitly revealed',
      (tester) async {
    var builds = 0;
    Widget app(String identity, String filename) => MaterialApp(
          home: Scaffold(
            body: AttachmentSpoiler(
              identity: identity,
              filename: filename,
              builder: (_) {
                builds++;
                return const Text('Private image contents');
              },
            ),
          ),
        );
    final semantics = tester.ensureSemantics();
    await tester.pumpWidget(app('first', 'SPOILER_photo.png'));
    expect(builds, 0);
    expect(
        find.text('Private image contents', skipOffstage: false), findsNothing);
    expect(find.bySemanticsLabel('Reveal spoiler attachment'), findsOneWidget);
    await tester.tap(find.text('SPOILER'));
    await tester.pump();
    expect(find.text('Private image contents'), findsOneWidget);
    await tester.pumpWidget(app('second', 'SPOILER_photo.png'));
    expect(
        find.text('Private image contents', skipOffstage: false), findsNothing);
    await tester.pumpWidget(app('second', 'photo.png'));
    expect(find.text('Private image contents'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    semantics.dispose();
  });

  testWidgets('queued attachment editor applies and removes spoiler marking',
      (tester) async {
    String filename = 'notes.pdf';
    await tester.pumpWidget(MaterialApp(
      home: Builder(
          builder: (context) => Scaffold(
                body: TextButton(
                  child: const Text('Edit attachment'),
                  onPressed: () async {
                    final result = await showAttachmentSpoilerEditor(context,
                        file: File('/unused.pdf'),
                        filename: filename,
                        contentType: 'application/pdf');
                    if (result != null) {
                      filename = spoilerFilename(filename, result);
                    }
                  },
                ),
              )),
    ));
    for (final expected in ['SPOILER_notes.pdf', 'notes.pdf']) {
      await tester.tap(find.text('Edit attachment'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(SwitchListTile));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Done'));
      await tester.pumpAndSettle();
      expect(filename, expected);
    }
  });
}
