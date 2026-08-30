import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Palette shared with the web and desktop clients so a Kaede account looks
/// like the same product on every surface. Values mirror the `[data-theme=dark]`
/// custom properties in `frontend/src/styles.css`.
abstract final class KaedeColors {
  /// Deepest layer: the app background behind every pane.
  static const canvas = Color(0xFF181715);

  /// Guild rail on the far left.
  static const rail = Color(0xFF111110);

  /// Channel and conversation list column.
  static const sidebar = Color(0xFF1D1B19);

  /// Default surface for cards, sheets, dialogs and headers.
  static const panel = Color(0xFF211F1C);

  /// One step above [panel]: inputs, chips, composer.
  static const raised = Color(0xFF2A2723);

  /// Pressed or hovered rows that are not selected.
  static const hover = Color(0xFF332F2A);

  /// The selected row in a list.
  static const selected = Color(0xFF422D27);

  /// Hairlines strong enough to read against [panel].
  static const border = Color(0xFF39342F);

  /// Dividers that must stay visible on [raised].
  static const borderStrong = Color(0xFF514A43);

  static const text = Color(0xFFF4EEE5);
  static const textSoft = Color(0xFFCDC3B7);
  static const muted = Color(0xFFAAA096);

  /// Brand accent.
  static const coral = Color(0xFFEE765E);
  static const coralBright = Color(0xFFFF8A71);

  /// Accent-tinted fill for badges, callouts and selected accents.
  static const coralSoft = Color(0xFF4B2C26);

  /// Accent tuned for text on dark surfaces (meets contrast where coral does
  /// not).
  static const coralText = Color(0xFFFF9B85);

  /// Foreground on top of a coral fill.
  static const onCoral = Color(0xFF231F1B);

  /// Kept for older call sites that used it as an accent-tinted background.
  static const coralDark = coralSoft;

  static const mint = Color(0xFF68B69B);
  static const mintSoft = Color(0xFF203F35);
  static const purple = Color(0xFFB49BE4);
  static const purpleSoft = Color(0xFF352D48);
  static const warning = Color(0xFFE4B75C);
  static const warningSoft = Color(0xFF44361E);
  static const danger = Color(0xFFFF8175);
  static const dangerSoft = Color(0xFF4C2926);
  static const focus = Color(0xFF78B7FF);
}

/// Semantic Kaede palette resolved from the active Material color scheme.
/// Custom chat and administration widgets use this instead of dark-only
/// constants so account/system appearance applies to every reachable pane.
@immutable
final class KaedePalette {
  const KaedePalette(this.scheme);

  final ColorScheme scheme;

  Color get canvas => scheme.surfaceDim;
  Color get rail => scheme.surfaceContainerLowest;
  Color get sidebar => scheme.surfaceContainerLow;
  Color get panel => scheme.surface;
  Color get raised => scheme.surfaceContainerHigh;
  Color get hover => scheme.surfaceContainerHighest;
  Color get selected => scheme.primaryContainer;
  Color get border => scheme.outlineVariant;
  Color get borderStrong => scheme.outline;
  Color get text => scheme.onSurface;
  Color get textSoft => scheme.onSurfaceVariant;
  Color get muted => scheme.onSurfaceVariant;
  Color get coral => scheme.primary;
  Color get coralBright => scheme.primary;
  Color get coralSoft => scheme.primaryContainer;
  Color get coralText => scheme.onPrimaryContainer;
  Color get onCoral => scheme.onPrimary;
  Color get coralDark => scheme.primaryContainer;
  Color get mint => scheme.secondary;
  Color get mintSoft => scheme.secondaryContainer;
  Color get purple => scheme.tertiary;
  Color get onPurple => scheme.onTertiary;
  Color get purpleSoft => scheme.tertiaryContainer;
  Color get warning => scheme.brightness == Brightness.dark
      ? KaedeColors.warning
      : const Color(0xFF765A00);
  Color get warningSoft => scheme.brightness == Brightness.dark
      ? KaedeColors.warningSoft
      : const Color(0xFFFFE08A);
  Color get danger => scheme.error;
  Color get dangerSoft => scheme.errorContainer;
  Color get focus => scheme.primary;
}

extension KaedeThemeContext on BuildContext {
  KaedePalette get kaede => KaedePalette(Theme.of(this).colorScheme);
}

Color readableForeground(Color background) =>
    ThemeData.estimateBrightnessForColor(background) == Brightness.dark
        ? Colors.white
        : Colors.black87;

/// Corner radii, kept in one place so panels, sheets and controls agree.
abstract final class KaedeRadius {
  static const small = 8.0;
  static const medium = 12.0;
  static const large = 18.0;
  static const pill = 999.0;
}

/// Status bar and navigation bar styling for the dark shell.
const kaedeSystemOverlay = SystemUiOverlayStyle(
  statusBarColor: Colors.transparent,
  statusBarIconBrightness: Brightness.light,
  statusBarBrightness: Brightness.dark,
  systemNavigationBarColor: KaedeColors.canvas,
  systemNavigationBarIconBrightness: Brightness.light,
  systemNavigationBarDividerColor: Colors.transparent,
);

SystemUiOverlayStyle kaedeSystemOverlayFor(Brightness brightness) =>
    brightness == Brightness.dark
        ? kaedeSystemOverlay
        : const SystemUiOverlayStyle(
            statusBarColor: Colors.transparent,
            statusBarIconBrightness: Brightness.dark,
            statusBarBrightness: Brightness.light,
            systemNavigationBarColor: Color(0xFFF5F2ED),
            systemNavigationBarIconBrightness: Brightness.dark,
            systemNavigationBarDividerColor: Colors.transparent,
          );

const _fontFamily = 'Inter';
const _fontFallback = <String>['Roboto', 'SF Pro Text', 'sans-serif'];

ColorScheme _scheme(Brightness brightness) => ColorScheme(
      brightness: brightness,
      primary: KaedeColors.coral,
      onPrimary: KaedeColors.onCoral,
      primaryContainer: KaedeColors.coralSoft,
      onPrimaryContainer: KaedeColors.coralText,
      secondary: KaedeColors.mint,
      onSecondary: KaedeColors.onCoral,
      secondaryContainer: KaedeColors.mintSoft,
      onSecondaryContainer: KaedeColors.mint,
      tertiary: KaedeColors.purple,
      onTertiary: KaedeColors.onCoral,
      tertiaryContainer: KaedeColors.purpleSoft,
      onTertiaryContainer: KaedeColors.purple,
      error: KaedeColors.danger,
      onError: KaedeColors.onCoral,
      errorContainer: KaedeColors.dangerSoft,
      onErrorContainer: KaedeColors.danger,
      surface: KaedeColors.panel,
      onSurface: KaedeColors.text,
      surfaceDim: KaedeColors.canvas,
      surfaceBright: KaedeColors.hover,
      surfaceContainerLowest: KaedeColors.rail,
      surfaceContainerLow: KaedeColors.sidebar,
      surfaceContainer: KaedeColors.panel,
      surfaceContainerHigh: KaedeColors.raised,
      surfaceContainerHighest: KaedeColors.hover,
      onSurfaceVariant: KaedeColors.muted,
      outline: KaedeColors.borderStrong,
      outlineVariant: KaedeColors.border,
      inverseSurface: KaedeColors.text,
      onInverseSurface: KaedeColors.canvas,
      inversePrimary: KaedeColors.coralSoft,
      shadow: Colors.black,
      scrim: Colors.black,
      surfaceTint: Colors.transparent,
    );

TextTheme _textTheme() => const TextTheme(
      displayLarge: TextStyle(
          fontSize: 40,
          fontWeight: FontWeight.w800,
          height: 1.08,
          letterSpacing: -1.1),
      displayMedium: TextStyle(
          fontSize: 33,
          fontWeight: FontWeight.w800,
          height: 1.12,
          letterSpacing: -.8),
      displaySmall: TextStyle(
          fontSize: 28,
          fontWeight: FontWeight.w800,
          height: 1.16,
          letterSpacing: -.6),
      headlineLarge: TextStyle(
          fontSize: 26,
          fontWeight: FontWeight.w800,
          height: 1.2,
          letterSpacing: -.6),
      headlineMedium: TextStyle(
          fontSize: 22,
          fontWeight: FontWeight.w700,
          height: 1.22,
          letterSpacing: -.4),
      headlineSmall: TextStyle(
          fontSize: 19,
          fontWeight: FontWeight.w700,
          height: 1.26,
          letterSpacing: -.3),
      titleLarge: TextStyle(
          fontSize: 17,
          fontWeight: FontWeight.w700,
          height: 1.3,
          letterSpacing: -.2),
      titleMedium: TextStyle(
          fontSize: 15,
          fontWeight: FontWeight.w600,
          height: 1.32,
          letterSpacing: -.1),
      titleSmall:
          TextStyle(fontSize: 13.5, fontWeight: FontWeight.w600, height: 1.34),
      bodyLarge: TextStyle(fontSize: 15.5, height: 1.42),
      bodyMedium: TextStyle(fontSize: 14, height: 1.42),
      bodySmall: TextStyle(fontSize: 12.5, height: 1.38),
      labelLarge: TextStyle(
          fontSize: 14, fontWeight: FontWeight.w600, letterSpacing: .1),
      labelMedium: TextStyle(
          fontSize: 12.5, fontWeight: FontWeight.w600, letterSpacing: .1),
      labelSmall: TextStyle(
          fontSize: 11, fontWeight: FontWeight.w700, letterSpacing: .55),
    );

ThemeData kaedeTheme({Brightness brightness = Brightness.dark}) {
  if (brightness == Brightness.light) return _kaedeLightTheme();
  final scheme = _scheme(brightness);
  final textTheme = _textTheme()
      .apply(
        fontFamily: _fontFamily,
        fontFamilyFallback: _fontFallback,
        bodyColor: KaedeColors.text,
        displayColor: KaedeColors.text,
      )
      .copyWith(
        bodySmall: _textTheme().bodySmall?.copyWith(
              fontFamily: _fontFamily,
              fontFamilyFallback: _fontFallback,
              color: KaedeColors.muted,
            ),
        labelSmall: _textTheme().labelSmall?.copyWith(
              fontFamily: _fontFamily,
              fontFamilyFallback: _fontFallback,
              color: KaedeColors.muted,
            ),
      );

  return ThemeData(
    useMaterial3: true,
    brightness: brightness,
    colorScheme: scheme,
    fontFamily: _fontFamily,
    fontFamilyFallback: _fontFallback,
    textTheme: textTheme,
    scaffoldBackgroundColor: KaedeColors.canvas,
    canvasColor: KaedeColors.canvas,
    dividerColor: KaedeColors.border,
    splashFactory: InkRipple.splashFactory,
    highlightColor: KaedeColors.text.withValues(alpha: .04),
    splashColor: KaedeColors.text.withValues(alpha: .06),
    visualDensity: VisualDensity.standard,
    iconTheme: const IconThemeData(color: KaedeColors.textSoft, size: 22),
    primaryIconTheme: const IconThemeData(color: KaedeColors.text),
    appBarTheme: AppBarTheme(
      backgroundColor: KaedeColors.canvas,
      foregroundColor: KaedeColors.text,
      surfaceTintColor: Colors.transparent,
      shadowColor: Colors.transparent,
      scrolledUnderElevation: 0,
      elevation: 0,
      centerTitle: false,
      systemOverlayStyle: kaedeSystemOverlay,
      titleTextStyle: textTheme.titleLarge,
      iconTheme: const IconThemeData(color: KaedeColors.textSoft, size: 22),
      actionsIconTheme:
          const IconThemeData(color: KaedeColors.textSoft, size: 22),
    ),
    dividerTheme: const DividerThemeData(
      color: KaedeColors.border,
      thickness: 1,
      space: 1,
    ),
    listTileTheme: ListTileThemeData(
      iconColor: KaedeColors.muted,
      textColor: KaedeColors.text,
      titleTextStyle: textTheme.titleMedium,
      subtitleTextStyle: textTheme.bodySmall,
      minVerticalPadding: 8,
      shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(KaedeRadius.medium)),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: KaedeColors.raised,
      hintStyle: textTheme.bodyMedium?.copyWith(color: KaedeColors.muted),
      labelStyle: textTheme.bodyMedium?.copyWith(color: KaedeColors.muted),
      floatingLabelStyle: textTheme.labelMedium?.copyWith(
        color: KaedeColors.coralText,
      ),
      helperStyle: textTheme.bodySmall,
      helperMaxLines: 3,
      errorStyle: textTheme.bodySmall?.copyWith(color: KaedeColors.danger),
      errorMaxLines: 3,
      prefixIconColor: KaedeColors.muted,
      suffixIconColor: KaedeColors.muted,
      counterStyle: textTheme.bodySmall,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        borderSide: const BorderSide(color: KaedeColors.border),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        borderSide: const BorderSide(color: KaedeColors.border),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        borderSide: const BorderSide(color: KaedeColors.coral, width: 1.6),
      ),
      errorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        borderSide: const BorderSide(color: KaedeColors.danger),
      ),
      focusedErrorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        borderSide: const BorderSide(color: KaedeColors.danger, width: 1.6),
      ),
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
    ),
    cardTheme: CardThemeData(
      color: KaedeColors.panel,
      surfaceTintColor: Colors.transparent,
      shadowColor: Colors.transparent,
      elevation: 0,
      margin: EdgeInsets.zero,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.large),
        side: const BorderSide(color: KaedeColors.border),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: KaedeColors.coral,
        foregroundColor: KaedeColors.onCoral,
        disabledBackgroundColor: KaedeColors.raised,
        disabledForegroundColor: KaedeColors.muted,
        minimumSize: const Size(0, 46),
        padding: const EdgeInsets.symmetric(horizontal: 18),
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(KaedeRadius.medium)),
        textStyle: textTheme.labelLarge?.copyWith(fontWeight: FontWeight.w700),
      ).copyWith(
        overlayColor: const WidgetStatePropertyAll(Color(0x1F231F1B)),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: KaedeColors.text,
        disabledForegroundColor: KaedeColors.muted,
        minimumSize: const Size(0, 46),
        padding: const EdgeInsets.symmetric(horizontal: 18),
        side: const BorderSide(color: KaedeColors.borderStrong),
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(KaedeRadius.medium)),
        textStyle: textTheme.labelLarge,
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: KaedeColors.coralText,
        disabledForegroundColor: KaedeColors.muted,
        minimumSize: const Size(0, 44),
        padding: const EdgeInsets.symmetric(horizontal: 12),
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(KaedeRadius.small)),
        textStyle: textTheme.labelLarge,
      ),
    ),
    iconButtonTheme: IconButtonThemeData(
      style: IconButton.styleFrom(
        foregroundColor: KaedeColors.textSoft,
        disabledForegroundColor: KaedeColors.muted.withValues(alpha: .5),
        highlightColor: KaedeColors.text.withValues(alpha: .07),
      ),
    ),
    segmentedButtonTheme: SegmentedButtonThemeData(
      style: ButtonStyle(
        backgroundColor: WidgetStateProperty.resolveWith((states) =>
            states.contains(WidgetState.selected)
                ? KaedeColors.coralSoft
                : Colors.transparent),
        foregroundColor: WidgetStateProperty.resolveWith((states) =>
            states.contains(WidgetState.selected)
                ? KaedeColors.coralText
                : KaedeColors.muted),
        side:
            const WidgetStatePropertyAll(BorderSide(color: KaedeColors.border)),
        textStyle: WidgetStatePropertyAll(textTheme.labelMedium),
      ),
    ),
    chipTheme: ChipThemeData(
      backgroundColor: KaedeColors.raised,
      selectedColor: KaedeColors.coralSoft,
      disabledColor: KaedeColors.raised,
      surfaceTintColor: Colors.transparent,
      checkmarkColor: KaedeColors.coralText,
      labelStyle: textTheme.labelMedium,
      secondaryLabelStyle: textTheme.labelMedium,
      side: const BorderSide(color: KaedeColors.border),
      shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(KaedeRadius.small)),
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
    ),
    floatingActionButtonTheme: FloatingActionButtonThemeData(
      backgroundColor: KaedeColors.coral,
      foregroundColor: KaedeColors.onCoral,
      splashColor: const Color(0x22231F1B),
      elevation: 3,
      focusElevation: 3,
      hoverElevation: 4,
      highlightElevation: 5,
      extendedTextStyle: const TextStyle(
        fontFamily: _fontFamily,
        fontSize: 14,
        fontWeight: FontWeight.w700,
      ),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.large),
      ),
    ),
    badgeTheme: BadgeThemeData(
      backgroundColor: KaedeColors.danger,
      textColor: KaedeColors.onCoral,
      textStyle: textTheme.labelSmall?.copyWith(
        color: KaedeColors.onCoral,
        fontWeight: FontWeight.w800,
        letterSpacing: 0,
      ),
      padding: const EdgeInsets.symmetric(horizontal: 5),
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: KaedeColors.panel,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      shadowColor: Colors.black,
      iconColor: KaedeColors.coralText,
      titleTextStyle: textTheme.headlineSmall,
      contentTextStyle: textTheme.bodyMedium?.copyWith(
        color: KaedeColors.textSoft,
      ),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.large),
        side: const BorderSide(color: KaedeColors.border),
      ),
      actionsPadding: const EdgeInsets.fromLTRB(14, 4, 14, 14),
    ),
    bottomSheetTheme: const BottomSheetThemeData(
      backgroundColor: KaedeColors.panel,
      surfaceTintColor: Colors.transparent,
      modalBackgroundColor: KaedeColors.panel,
      elevation: 0,
      modalElevation: 0,
      dragHandleColor: KaedeColors.borderStrong,
      dragHandleSize: Size(38, 4),
      showDragHandle: false,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(
          top: Radius.circular(22),
        ),
      ),
    ),
    popupMenuTheme: PopupMenuThemeData(
      color: KaedeColors.raised,
      surfaceTintColor: Colors.transparent,
      elevation: 8,
      shadowColor: Colors.black54,
      textStyle: textTheme.bodyMedium,
      labelTextStyle: WidgetStatePropertyAll(textTheme.bodyMedium),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        side: const BorderSide(color: KaedeColors.border),
      ),
    ),
    menuTheme: MenuThemeData(
      style: MenuStyle(
        backgroundColor: const WidgetStatePropertyAll(KaedeColors.raised),
        surfaceTintColor: const WidgetStatePropertyAll(Colors.transparent),
        shape: WidgetStatePropertyAll(RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          side: const BorderSide(color: KaedeColors.border),
        )),
      ),
    ),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: KaedeColors.raised,
      contentTextStyle: textTheme.bodyMedium?.copyWith(color: KaedeColors.text),
      actionTextColor: KaedeColors.coralText,
      behavior: SnackBarBehavior.floating,
      elevation: 6,
      insetPadding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        side: const BorderSide(color: KaedeColors.border),
      ),
    ),
    tooltipTheme: TooltipThemeData(
      decoration: BoxDecoration(
        color: KaedeColors.raised,
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        border: Border.all(color: KaedeColors.border),
      ),
      textStyle: textTheme.bodySmall?.copyWith(color: KaedeColors.text),
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
      waitDuration: const Duration(milliseconds: 500),
    ),
    expansionTileTheme: const ExpansionTileThemeData(
      iconColor: KaedeColors.muted,
      collapsedIconColor: KaedeColors.muted,
      textColor: KaedeColors.text,
      collapsedTextColor: KaedeColors.text,
      backgroundColor: Colors.transparent,
      collapsedBackgroundColor: Colors.transparent,
      shape: Border(),
      collapsedShape: Border(),
    ),
    switchTheme: SwitchThemeData(
      thumbColor: WidgetStateProperty.resolveWith((states) =>
          states.contains(WidgetState.selected)
              ? KaedeColors.onCoral
              : KaedeColors.textSoft),
      trackColor: WidgetStateProperty.resolveWith((states) =>
          states.contains(WidgetState.selected)
              ? KaedeColors.coral
              : KaedeColors.raised),
      trackOutlineColor: WidgetStateProperty.resolveWith((states) =>
          states.contains(WidgetState.selected)
              ? Colors.transparent
              : KaedeColors.borderStrong),
    ),
    checkboxTheme: CheckboxThemeData(
      fillColor: WidgetStateProperty.resolveWith((states) =>
          states.contains(WidgetState.selected)
              ? KaedeColors.coral
              : Colors.transparent),
      checkColor: const WidgetStatePropertyAll(KaedeColors.onCoral),
      side: const BorderSide(color: KaedeColors.borderStrong, width: 1.6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(5)),
    ),
    radioTheme: RadioThemeData(
      fillColor: WidgetStateProperty.resolveWith((states) =>
          states.contains(WidgetState.selected)
              ? KaedeColors.coral
              : KaedeColors.borderStrong),
    ),
    sliderTheme: const SliderThemeData(
      activeTrackColor: KaedeColors.coral,
      inactiveTrackColor: KaedeColors.raised,
      thumbColor: KaedeColors.coral,
    ),
    progressIndicatorTheme: const ProgressIndicatorThemeData(
      color: KaedeColors.coral,
      linearTrackColor: KaedeColors.raised,
      circularTrackColor: Colors.transparent,
      strokeWidth: 2.4,
    ),
    scrollbarTheme: ScrollbarThemeData(
      thumbColor: WidgetStatePropertyAll(
        KaedeColors.textSoft.withValues(alpha: .28),
      ),
      radius: const Radius.circular(KaedeRadius.pill),
      thickness: const WidgetStatePropertyAll(3),
    ),
    textSelectionTheme: TextSelectionThemeData(
      cursorColor: KaedeColors.coral,
      selectionColor: KaedeColors.coral.withValues(alpha: .34),
      selectionHandleColor: KaedeColors.coral,
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: KaedeColors.sidebar,
      surfaceTintColor: Colors.transparent,
      indicatorColor: KaedeColors.coralSoft,
      elevation: 0,
      height: 62,
      labelTextStyle: WidgetStatePropertyAll(textTheme.labelMedium),
    ),
    drawerTheme: const DrawerThemeData(
      backgroundColor: KaedeColors.sidebar,
      surfaceTintColor: Colors.transparent,
    ),
    dropdownMenuTheme: DropdownMenuThemeData(
      textStyle: textTheme.bodyMedium,
      menuStyle: MenuStyle(
        backgroundColor: const WidgetStatePropertyAll(KaedeColors.raised),
        surfaceTintColor: const WidgetStatePropertyAll(Colors.transparent),
        shape: WidgetStatePropertyAll(RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          side: const BorderSide(color: KaedeColors.border),
        )),
      ),
    ),
    pageTransitionsTheme: const PageTransitionsTheme(
      builders: <TargetPlatform, PageTransitionsBuilder>{
        TargetPlatform.android: CupertinoPageTransitionsBuilder(),
        TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
      },
    ),
  );
}

ThemeData _kaedeLightTheme() {
  const canvas = Color(0xFFF5F2ED);
  const panel = Color(0xFFFFFCF8);
  const raised = Color(0xFFECE6DF);
  const text = Color(0xFF261E1A);
  const muted = Color(0xFF6F625A);
  const border = Color(0xFFD8CFC6);
  const coral = Color(0xFFB84331);
  final scheme = ColorScheme.fromSeed(
    seedColor: coral,
    brightness: Brightness.light,
    surface: panel,
  ).copyWith(
    primary: coral,
    onPrimary: Colors.white,
    primaryContainer: const Color(0xFFFFDAD2),
    onPrimaryContainer: const Color(0xFF5F170D),
    secondary: const Color(0xFF397761),
    secondaryContainer: const Color(0xFFC2EBD8),
    onSecondaryContainer: const Color(0xFF0D392B),
    tertiary: const Color(0xFF72549A),
    tertiaryContainer: const Color(0xFFEBDDFF),
    error: const Color(0xFFBA1A1A),
    errorContainer: const Color(0xFFFFDAD6),
    surfaceDim: canvas,
    surfaceContainerLowest: Colors.white,
    surfaceContainerLow: const Color(0xFFF8F4EF),
    surfaceContainer: panel,
    surfaceContainerHigh: raised,
    surfaceContainerHighest: const Color(0xFFE2DBD3),
    onSurface: text,
    onSurfaceVariant: muted,
    outline: const Color(0xFF81746C),
    outlineVariant: border,
    surfaceTint: Colors.transparent,
  );
  final textTheme = _textTheme().apply(
    fontFamily: _fontFamily,
    fontFamilyFallback: _fontFallback,
    bodyColor: text,
    displayColor: text,
  );
  return ThemeData(
    useMaterial3: true,
    brightness: Brightness.light,
    colorScheme: scheme,
    fontFamily: _fontFamily,
    fontFamilyFallback: _fontFallback,
    textTheme: textTheme,
    scaffoldBackgroundColor: canvas,
    canvasColor: canvas,
    dividerColor: border,
    appBarTheme: AppBarTheme(
      backgroundColor: canvas,
      foregroundColor: text,
      elevation: 0,
      scrolledUnderElevation: 1,
      surfaceTintColor: Colors.transparent,
      titleTextStyle: textTheme.titleLarge,
      systemOverlayStyle: kaedeSystemOverlayFor(Brightness.light),
    ),
    cardTheme: const CardThemeData(
      color: panel,
      elevation: 0,
      margin: EdgeInsets.zero,
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: raised,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        borderSide: const BorderSide(color: border),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        borderSide: const BorderSide(color: border),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        borderSide: const BorderSide(color: coral, width: 1.6),
      ),
    ),
    dividerTheme: const DividerThemeData(color: border, thickness: 1),
    bottomSheetTheme: const BottomSheetThemeData(
      backgroundColor: panel,
      modalBackgroundColor: panel,
      showDragHandle: true,
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: panel,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(KaedeRadius.large),
      ),
    ),
    navigationBarTheme: const NavigationBarThemeData(
      backgroundColor: Color(0xFFF8F4EF),
      indicatorColor: Color(0xFFFFDAD2),
    ),
    navigationRailTheme: const NavigationRailThemeData(
      backgroundColor: Color(0xFFF8F4EF),
      indicatorColor: Color(0xFFFFDAD2),
    ),
    snackBarTheme: const SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      backgroundColor: Color(0xFF342A25),
      contentTextStyle: TextStyle(color: Colors.white),
    ),
  );
}
