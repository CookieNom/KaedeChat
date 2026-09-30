import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

void main() {
  for (final platform in [TargetPlatform.iOS, TargetPlatform.android]) {
    testWidgets(
        'actions block duplicate taps and recover after failure on $platform',
        (tester) async {
      var calls = 0;
      var pending = Completer<void>();
      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme().copyWith(platform: platform),
        home: Scaffold(
            body: ActionButton(
          onPressed: () {
            calls++;
            return pending.future;
          },
          child: const Text('Save'),
        )),
      ));
      final press =
          tester.widget<FilledButton>(find.byType(FilledButton)).onPressed!;
      press();
      press();
      await tester.pump();
      expect(calls, 1);
      expect(tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
          isNull);
      expect(find.byType(ActionProgress), findsOneWidget);
      pending.completeError(StateError('test failure'));
      await tester.pumpAndSettle();
      expect(find.byType(SnackBar), findsOneWidget);
      expect(find.byType(ActionProgress), findsNothing);
      expect(tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
          isNotNull);
      pending = Completer<void>();
      await tester.tap(find.text('Save'));
      expect(calls, 2);
      // Completing after navigation must not update a disposed control.
      await tester.pumpWidget(const SizedBox());
      pending.complete();
      await tester.pump();
      expect(tester.takeException(), isNull);
    });

    testWidgets(
        'Save tracks edits, reverts, pending saves and retries on $platform',
        (tester) async {
      final input = TextEditingController(text: 'Original');
      addTearDown(input.dispose);
      var saved = input.text;
      var pending = Completer<void>();
      var calls = 0;
      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme().copyWith(platform: platform),
        home: Scaffold(
            body: SaveButton(
          controllers: [input],
          hasChanges: () => input.text.trim() != saved,
          isValid: () => input.text.trim().isNotEmpty,
          onPressed: () async {
            calls++;
            final submitted = input.text.trim();
            await pending.future;
            saved = submitted;
          },
          child: const Text('Save'),
        )),
      ));
      bool enabled() =>
          tester.widget<FilledButton>(find.byType(FilledButton)).onPressed !=
          null;
      expect(enabled(), isFalse);
      input.text = '   ';
      await tester.pump();
      expect(enabled(), isFalse);
      input.text = 'Edited';
      await tester.pump();
      expect(enabled(), isTrue);
      input.text = 'Original';
      await tester.pump();
      expect(enabled(), isFalse);
      input.text = 'Edited';
      await tester.pump();
      await tester.tap(find.text('Save'));
      await tester.pump();
      expect(enabled(), isFalse);
      expect(calls, 1);
      pending.complete();
      await tester.pumpAndSettle();
      expect(enabled(), isFalse);
      input.text = 'Another edit';
      await tester.pump();
      pending = Completer<void>();
      await tester.tap(find.text('Save'));
      pending.completeError(StateError('Offline'));
      await tester.pumpAndSettle();
      expect(enabled(), isTrue);
      expect(input.text, 'Another edit');
      pending = Completer<void>();
      await tester.tap(find.text('Save'));
      input.text = 'Edited while saving';
      pending.complete();
      await tester.pumpAndSettle();
      expect(saved, 'Another edit');
      expect(enabled(), isTrue);
    });

    testWidgets('text dialogs close safely with a keyboard on $platform',
        (tester) async {
      addTearDown(tester.view.resetViewInsets);
      String? result;
      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme().copyWith(platform: platform),
        home: Builder(
            builder: (context) => Scaffold(
                  body: FilledButton(
                    onPressed: () async {
                      result = await showSettingsTextDialog(context,
                          title: 'Create a guild',
                          label: 'Guild name',
                          actionLabel: 'Continue');
                    },
                    child: const Text('Open'),
                  ),
                )),
      ));
      for (var attempt = 0; attempt < 2; attempt++) {
        result = null;
        await tester.tap(find.text('Open'));
        await tester.pumpAndSettle();
        final submit = find.widgetWithText(FilledButton, 'Continue');
        expect(tester.widget<FilledButton>(submit).onPressed, isNull);
        await tester.enterText(find.byType(TextField), '   ');
        await tester.pump();
        expect(tester.widget<FilledButton>(submit).onPressed, isNull);
        await tester.enterText(find.byType(TextField), 'Guild $attempt');
        tester.view.viewInsets = const FakeViewPadding(bottom: 240);
        await tester.pumpAndSettle();
        await tester.tap(submit);
        await tester.pump();
        // Rebuild the departing field while the route is animating out.
        tester.view.viewInsets = const FakeViewPadding(bottom: 120);
        await tester.pump(const Duration(milliseconds: 40));
        expect(result, isNull);
        tester.view.resetViewInsets();
        await tester.pumpAndSettle();
        expect(result, 'Guild $attempt');
        expect(tester.takeException(), isNull);
      }
    });

    testWidgets('settings switch and choice await their changes on $platform',
        (tester) async {
      var switches = 0;
      var choices = 0;
      final switched = Completer<void>();
      final chosen = Completer<void>();
      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme().copyWith(platform: platform),
        home: Scaffold(
            body: Column(children: [
          SettingsSwitchRow(
              title: 'Notifications',
              value: false,
              onChanged: (_) {
                switches++;
                return switched.future;
              }),
          SettingsChoiceRow(
              title: 'Theme',
              value: 'system',
              display: 'System',
              onSelected: (_) {
                choices++;
                return chosen.future;
              }),
          const SettingsSwitchRow(
              title: 'Unavailable', value: false, onChanged: null),
        ])),
      ));
      // The switch is a visual child; the containing row owns its tap target.
      await tester.tapAt(tester.getCenter(find.byType(DiscordSwitch).first));
      await tester.pump();
      await tester.tap(find.text('Notifications'));
      await tester.tap(find.text('Theme'));
      await tester.pump();
      await tester.tap(find.text('Theme'));
      await tester.tap(find.text('Unavailable'));
      expect(switches, 1);
      expect(choices, 1);
      expect(find.byType(ActionProgress), findsNWidgets(2));
      switched.complete();
      chosen.complete();
      await tester.pumpAndSettle();
      expect(find.byType(ActionProgress), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }
}
