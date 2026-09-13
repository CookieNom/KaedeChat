import 'package:flutter/widgets.dart';
import 'package:kaede_mobile/l10n/generated/app_localizations.dart';
import 'package:shared_preferences/shared_preferences.dart';

final appLanguage = ValueNotifier<String>('en-US');
final languageChoiceHandled = ValueNotifier<bool>(false);

String? matchLanguage(String value) {
  final normalized = value.replaceAll('_', '-').toLowerCase();
  for (final locale in AppLocalizations.supportedLocales) {
    if (locale.toLanguageTag().toLowerCase() == normalized) {
      return locale.toLanguageTag();
    }
  }
  for (final locale in AppLocalizations.supportedLocales) {
    if (locale.languageCode == normalized.split('-').first) {
      return locale.toLanguageTag();
    }
  }
  return null;
}

String normalizeLanguage(Object? value) => value == 'system'
    ? 'system'
    : (matchLanguage('$value') != null ? '$value' : 'en-US');

Locale? preferredAppLocale(String value) {
  if (value == 'system') return null;
  final parts = value.replaceAll('_', '-').split('-');
  if (matchLanguage(value) == null) return const Locale('en', 'US');
  return Locale.fromSubtags(
    languageCode: parts.first.toLowerCase(),
    scriptCode: parts.skip(1).where((part) => part.length == 4).firstOrNull,
    countryCode: parts
        .skip(1)
        .where((part) => part.length == 2 || part.length == 3)
        .firstOrNull
        ?.toUpperCase(),
  );
}

String preferredInteractionLocale() {
  if (appLanguage.value != 'system') {
    return preferredAppLocale(appLanguage.value)!.toLanguageTag();
  }
  final locales = WidgetsBinding.instance.platformDispatcher.locales;
  final resolved =
      basicLocaleListResolution(locales, AppLocalizations.supportedLocales);
  return locales
          .where((locale) =>
              matchLanguage(locale.toLanguageTag()) == resolved.toLanguageTag())
          .firstOrNull
          ?.toLanguageTag() ??
      resolved.toLanguageTag();
}

Locale? suggestedLanguage(List<Locale> system, Locale current, bool handled) {
  if (handled || system.isEmpty || system.first.languageCode == 'en') {
    return null;
  }
  final match = matchLanguage(system.first.toLanguageTag());
  if (match == null ||
      match == 'en' ||
      match == matchLanguage(current.toLanguageTag())) {
    return null;
  }
  return preferredAppLocale(match);
}

Future<void> initializeLanguage() async {
  try {
    final preferences = await SharedPreferences.getInstance();
    appLanguage.value =
        normalizeLanguage(preferences.getString('kaede.locale'));
    languageChoiceHandled.value =
        preferences.getBool('kaede.language-choice') ?? false;
  } on Object {/* Defaults remain usable when storage is unavailable. */}
}

Future<void> setAppLanguage(String value, {bool explicit = false}) async {
  appLanguage.value = normalizeLanguage(value);
  if (explicit) languageChoiceHandled.value = true;
  try {
    final preferences = await SharedPreferences.getInstance();
    await preferences.setString('kaede.locale', appLanguage.value);
    if (explicit) await preferences.setBool('kaede.language-choice', true);
  } on Object {/* The in-memory preference still applies. */}
}

Future<void> dismissLanguageSuggestion() async {
  languageChoiceHandled.value = true;
  try {
    final preferences = await SharedPreferences.getInstance();
    await preferences.setBool('kaede.language-choice', true);
  } on Object {/* Still dismissed for this session. */}
}

/// Use context in widgets; current is for notifications and non-widget services.
abstract final class L10n {
  static AppLocalizations current = lookupAppLocalizations(const Locale('en'));
  static AppLocalizations of(BuildContext context) =>
      Localizations.of<AppLocalizations>(context, AppLocalizations) ?? current;
}
