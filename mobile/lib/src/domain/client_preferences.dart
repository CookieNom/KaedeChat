import 'package:flutter/material.dart';

enum KaedeThemePreference { system, light, dark }

const kaedeSupportedLocales = <Locale>[
  Locale('en', 'US'),
  Locale('ja', 'JP'),
];

KaedeThemePreference parseThemePreference(Object? value) =>
    KaedeThemePreference.values.firstWhere(
      (item) => item.name == value,
      orElse: () => KaedeThemePreference.system,
    );

Locale parseLocalePreference(Object? value) {
  final normalized = '$value'.trim().replaceAll('_', '-');
  for (final locale in kaedeSupportedLocales) {
    if (locale.toLanguageTag().toLowerCase() == normalized.toLowerCase()) {
      return locale;
    }
  }
  return kaedeSupportedLocales.first;
}

bool parseDeveloperMode(Object? notificationSettings) =>
    notificationSettings is Map<Object?, Object?> &&
    notificationSettings['developer_mode'] == true;

ThemeMode materialThemeMode(KaedeThemePreference preference) =>
    switch (preference) {
      KaedeThemePreference.system => ThemeMode.system,
      KaedeThemePreference.light => ThemeMode.light,
      KaedeThemePreference.dark => ThemeMode.dark,
    };
