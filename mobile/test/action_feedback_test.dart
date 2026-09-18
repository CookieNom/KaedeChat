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
