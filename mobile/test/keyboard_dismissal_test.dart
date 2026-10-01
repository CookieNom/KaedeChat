import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/shared/keyboard_dismissal.dart';

void main() {
  for (final platform in [TargetPlatform.iOS, TargetPlatform.android]) {
    testWidgets('outside taps dismiss without swallowing controls on $platform',
        (tester) async {
      final first = FocusNode();
      final second = FocusNode();
      addTearDown(first.dispose);
      addTearDown(second.dispose);
      var presses = 0;
      final editor = Column(mainAxisSize: MainAxisSize.min, children: [
        TextField(focusNode: first),
        TextFormField(focusNode: second),
        TextFieldTapRegion(
          child: GestureDetector(
            onTap: () => presses++,
            child: const Text('Field control'),
          ),
        ),
        FilledButton(onPressed: () => presses++, child: const Text('Action')),
        const ColoredBox(
          key: ValueKey('outside'),
          color: Colors.transparent,
          child: SizedBox(height: 48, width: 200),
        ),
      ]);
      Future<void> checkTaps() async {
        await tester.tap(find.byType(TextField).first);
        await tester.pumpAndSettle();
        expect(first.hasFocus, isTrue);
        expect(tester.testTextInput.isVisible, isTrue);
        await tester.tap(find.byType(TextField).first);
        await tester.tap(find.text('Field control'));
        await tester.pumpAndSettle();
        expect(first.hasFocus, isTrue);
        await tester.tap(find.byType(TextFormField));
        await tester.pumpAndSettle();
        expect(second.hasFocus, isTrue);
        expect(tester.testTextInput.isVisible, isTrue);
        await tester.tap(find.byKey(const ValueKey('outside')));
        await tester.pumpAndSettle();
        expect(second.hasFocus, isFalse);
        expect(tester.testTextInput.isVisible, isFalse);
        await tester.tap(find.byType(TextField).first);
        await tester.pumpAndSettle();
        final before = presses;
        await tester.tap(find.text('Action'));
        await tester.pumpAndSettle();
        expect(presses, before + 1);
        expect(first.hasFocus, isFalse);
        expect(tester.testTextInput.isVisible, isFalse);
        expect(tester.takeException(), isNull);
      }

      for (final surface in ['page', 'dialog', 'sheet']) {
        await tester.pumpWidget(MaterialApp(
          builder: (_, child) => KeyboardDismissal(child: child!),
          home: Scaffold(body: surface == 'page' ? editor : const SizedBox()),
        ));
        final context = tester.element(find.byType(Scaffold));
        if (surface == 'dialog') {
          showDialog<void>(
            context: context,
            builder: (_) => AlertDialog(content: editor),
          );
        } else if (surface == 'sheet') {
          showModalBottomSheet<void>(context: context, builder: (_) => editor);
        }
        await tester.pumpAndSettle();
        await checkTaps();
        await tester.pumpWidget(const SizedBox.shrink());
      }
    }, variant: TargetPlatformVariant.only(platform));
  }
}
