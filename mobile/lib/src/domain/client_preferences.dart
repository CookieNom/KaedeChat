import 'package:flutter/material.dart';
import 'package:kaede_mobile/l10n/generated/app_localizations.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

enum KaedeThemePreference { system, light, dark }

const kaedeSupportedLocales = AppLocalizations.supportedLocales;

KaedeThemePreference parseThemePreference(Object? value) =>
    KaedeThemePreference.values.firstWhere(
      (item) => item.name == value,
      orElse: () => KaedeThemePreference.system,
    );

Locale? parseLocalePreference(Object? value) =>
    preferredAppLocale(normalizeLanguage(value));

bool parseDeveloperMode(Object? notificationSettings) =>
    notificationSettings is Map<Object?, Object?> &&
    notificationSettings['developer_mode'] == true;

ThemeMode materialThemeMode(KaedeThemePreference preference) =>
    switch (preference) {
      KaedeThemePreference.system => ThemeMode.system,
      KaedeThemePreference.light => ThemeMode.light,
      KaedeThemePreference.dark => ThemeMode.dark,
    };
