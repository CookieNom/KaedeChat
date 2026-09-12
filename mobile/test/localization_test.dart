import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/l10n/generated/app_localizations.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    languageChoiceHandled.value = false;
    appLanguage.value = 'en-US';
  });

  test('English devices never receive the suggestion', () {
    for (final locale in [
      const Locale('en'),
      const Locale('en', 'US'),
      const Locale('en', 'GB')
    ]) {
      expect(
          suggestedLanguage(
              [locale, const Locale('ja')], const Locale('en'), false),
          isNull);
    }
    expect(
        suggestedLanguage(
            [const Locale('ja', 'JP')], const Locale('en'), false),
        const Locale('ja'));
    expect(suggestedLanguage([const Locale('ja')], const Locale('ja'), false),
        isNull);
    expect(suggestedLanguage([const Locale('ja')], const Locale('en'), true),
        isNull);
    expect(
        suggestedLanguage([const Locale('fr'), const Locale('ja')],
            const Locale('en'), false),
        isNull);
    expect(suggestedLanguage([], const Locale('en'), false), isNull);
  });

  test('choice and dismissal survive a restart, system remains a preference',
      () async {
    await setAppLanguage('system', explicit: true);
    appLanguage.value = 'en-US';
    languageChoiceHandled.value = false;
    await initializeLanguage();
    expect(appLanguage.value, 'system');
    expect(languageChoiceHandled.value, true);
    expect(preferredAppLocale('system'), isNull);
    expect(preferredAppLocale('ja_JP'), const Locale('ja', 'JP'));
    expect(preferredAppLocale('unsupported'), const Locale('en', 'US'));
  });

  testWidgets('existing widgets update with the language without losing state',
      (tester) async {
    await tester.pumpWidget(ValueListenableBuilder<String>(
      valueListenable: appLanguage,
      builder: (context, value, child) => MaterialApp(
        locale: preferredAppLocale(value),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const _Screen(),
      ),
    ));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'Keep this draft');
    expect(find.text('Language'), findsOneWidget);
    await setAppLanguage('ja-JP');
    await tester.pumpAndSettle();
    expect(find.text('言語'), findsOneWidget);
    expect(find.text('Keep this draft'), findsOneWidget);
    await setAppLanguage('system');
    tester.binding.platformDispatcher.localesTestValue = [
      const Locale('en', 'GB')
    ];
    await tester.pumpAndSettle();
    expect(find.text('Language'), findsOneWidget);
    tester.binding.platformDispatcher.localesTestValue = [
      const Locale('ja', 'JP')
    ];
    await tester.pumpAndSettle();
    expect(find.text('言語'), findsOneWidget);
    tester.binding.platformDispatcher.clearLocalesTestValue();
  });
}

class _Screen extends StatelessWidget {
  const _Screen();
  @override
  Widget build(BuildContext context) => Scaffold(
          body: Column(children: [
        Text(L10n.of(context).language_settings),
        const TextField(),
      ]));
}
