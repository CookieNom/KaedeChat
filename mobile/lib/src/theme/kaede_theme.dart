import 'package:flutter/material.dart';

abstract final class KaedeColors {
  static const canvas = Color(0xFF08090B);
  static const rail = Color(0xFF0D0E10);
  static const panel = Color(0xFF111214);
  static const raised = Color(0xFF1B1C1F);
  static const selected = Color(0xFF27282C);
  static const border = Color(0xFF2A2C31);
  static const text = Color(0xFFF2F3F5);
  static const muted = Color(0xFFA4A7AE);
  static const coral = Color(0xFFF4775F);
  static const coralDark = Color(0xFF9E493A);
  static const mint = Color(0xFF55B998);
  static const warning = Color(0xFFF2B84B);
  static const danger = Color(0xFFF36B6B);
}

ThemeData kaedeTheme({Brightness brightness = Brightness.dark}) {
  final scheme = ColorScheme.fromSeed(
    seedColor: KaedeColors.coral,
    brightness: brightness,
    surface: KaedeColors.panel,
    error: KaedeColors.danger,
  );
  return ThemeData(
    useMaterial3: true,
    brightness: brightness,
    colorScheme: scheme,
    scaffoldBackgroundColor: KaedeColors.canvas,
    canvasColor: KaedeColors.panel,
    dividerColor: KaedeColors.border,
    textTheme: const TextTheme(
      displayLarge:
          TextStyle(fontSize: 44, fontWeight: FontWeight.w800, height: 1),
      headlineLarge: TextStyle(fontSize: 29, fontWeight: FontWeight.w800),
      headlineMedium: TextStyle(fontSize: 22, fontWeight: FontWeight.w700),
      titleLarge: TextStyle(fontSize: 19, fontWeight: FontWeight.w700),
      bodyLarge: TextStyle(fontSize: 16, height: 1.32),
      bodyMedium: TextStyle(fontSize: 14, height: 1.32),
      labelLarge: TextStyle(fontSize: 14, fontWeight: FontWeight.w700),
    ).apply(bodyColor: KaedeColors.text, displayColor: KaedeColors.text),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: KaedeColors.raised,
      hintStyle: const TextStyle(color: KaedeColors.muted),
      border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12), borderSide: BorderSide.none),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: KaedeColors.coral, width: 2),
      ),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
    ),
    cardTheme: CardThemeData(
      color: KaedeColors.panel,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: const BorderSide(color: KaedeColors.border),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: KaedeColors.coral,
        foregroundColor: KaedeColors.canvas,
        minimumSize: const Size(48, 48),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        textStyle: const TextStyle(fontWeight: FontWeight.w800),
      ),
    ),
    navigationBarTheme: const NavigationBarThemeData(
      backgroundColor: KaedeColors.panel,
      indicatorColor: Color(0xFF4B302A),
      labelTextStyle:
          WidgetStatePropertyAll(TextStyle(fontWeight: FontWeight.w700)),
    ),
  );
}
