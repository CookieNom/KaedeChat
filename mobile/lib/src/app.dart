import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/domain/client_preferences.dart';
import 'package:kaede_mobile/src/features/auth/auth_screen.dart';
import 'package:kaede_mobile/src/features/auth/deep_link_screen.dart';
import 'package:kaede_mobile/src/features/home/mobile_shell.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class KaedeApp extends ConsumerStatefulWidget {
  const KaedeApp({super.key});

  @override
  ConsumerState<KaedeApp> createState() => _KaedeAppState();
}

final class _KaedeAppState extends ConsumerState<KaedeApp>
    with WidgetsBindingObserver {
  late final GoRouter _router;
  StreamSubscription<List<ConnectivityResult>>? _connectivitySubscription;
  bool? _networkAvailable;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _connectivitySubscription =
        Connectivity().onConnectivityChanged.listen(_handleConnectivity);
    _router = GoRouter(
      routes: <RouteBase>[
        GoRoute(
          path: '/',
          builder: (context, state) => const _SessionGate(),
        ),
        for (final path in <String>[
          '/invite/:code',
          '/verify',
          '/verify-email',
          '/reset-password',
          '/verify-email-change',
          '/applications/:applicationRef/install/:templateSlug',
          '/g/:guildId/:channelId',
          '/home/:dmId',
        ])
          GoRoute(
            path: path,
            builder: (context, state) {
              final link = MobileDeepLink.parse(state.uri);
              return link == null
                  ? const _InvalidLinkScreen()
                  : _DeepLinkGate(link: link);
            },
          ),
        GoRoute(
          path: '/open',
          builder: (context, state) => _SessionGate(
            destination: PushDestination.parse(state.uri.queryParameters),
          ),
        ),
      ],
      errorBuilder: (context, state) => const _SessionGate(),
    );
  }

  void _handleConnectivity(List<ConnectivityResult> results) {
    final available =
        results.any((result) => result != ConnectivityResult.none);
    final restored = _networkAvailable == false && available;
    _networkAvailable = available;
    if (restored) {
      unawaited(
        ref
            .read(mobileControllerProvider.notifier)
            .retryRealtime(foreground: true),
      );
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final active = state == AppLifecycleState.resumed;
    ref.read(mobileControllerProvider.notifier).setAppActive(active);
    final voice = ref.read(voiceSessionProvider);
    switch (state) {
      case AppLifecycleState.resumed:
        unawaited(voice.didResume());
        break;
      case AppLifecycleState.hidden:
      case AppLifecycleState.paused:
        voice.didEnterBackground();
        break;
      case AppLifecycleState.detached:
      case AppLifecycleState.inactive:
        // Inactive also covers temporary interruptions such as permission and
        // biometric prompts. It must not tear down a healthy call.
        break;
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _connectivitySubscription?.cancel();
    _router.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final preferences = ref.watch(mobileControllerProvider);
    ref.listen<MobileState>(mobileControllerProvider, (previous, next) {
      final voice = ref.read(voiceSessionProvider);
      if (next.phase == SessionPhase.signedOut &&
          previous?.phase != SessionPhase.signedOut) {
        voice.leave();
        return;
      }
      final activeVoice = voice.channel;
      if (activeVoice != null && next.phase == SessionPhase.ready) {
        final fresh = findVoiceSessionChannel(
          target: activeVoice.ref,
          directMessages: next.dms,
          guilds: next.guilds,
        );
        if (fresh == null) {
          voice.leave(reason: 'This voice channel is no longer available.');
        } else {
          voice.reconcilePermissions(fresh);
        }
      }
    });
    return MaterialApp.router(
      title: 'Kaede Chat',
      debugShowCheckedModeBanner: false,
      theme: kaedeTheme(brightness: Brightness.light),
      darkTheme: kaedeTheme(),
      themeMode: materialThemeMode(preferences.themePreference),
      locale: parseLocalePreference(preferences.localePreference),
      supportedLocales: kaedeSupportedLocales,
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      restorationScopeId: 'kaede-mobile',
      routerConfig: _router,
      builder: (context, child) => AnnotatedRegion<SystemUiOverlayStyle>(
        value: kaedeSystemOverlayFor(Theme.of(context).brightness),
        child: child ?? SizedBox.shrink(),
      ),
    );
  }
}

final class _DeepLinkGate extends ConsumerWidget {
  const _DeepLinkGate({required this.link});
  final MobileDeepLink link;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final phase = ref.watch(mobileControllerProvider).phase;
    if (phase == SessionPhase.restoring) return const _LaunchScreen();
    if (phase == SessionPhase.locked) return const _LockScreen();
    if ((phase == SessionPhase.signedOut ||
            phase == SessionPhase.authenticating) &&
        link.requiresSession) {
      return AuthScreen(
        initialInstance: link.instance.value,
        notice: link.signInNotice,
      );
    }
    if (link.kind == MobileLinkKind.applicationInstall) {
      return ApplicationInstallDeepLinkScreen(link: link);
    }
    return DeepLinkActionScreen(link: link);
  }
}

final class _InvalidLinkScreen extends StatelessWidget {
  const _InvalidLinkScreen();
  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text('Invalid link')),
        body: Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.link_off_rounded, size: 42),
                SizedBox(height: 12),
                Text('This Kaede link is incomplete or malformed.'),
                SizedBox(height: 16),
                FilledButton(
                  onPressed: () => context.go('/'),
                  child: Text('Open Kaede'),
                ),
              ],
            ),
          ),
        ),
      );
}

final class _SessionGate extends ConsumerWidget {
  const _SessionGate({this.destination});

  final PushDestination? destination;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return switch (ref.watch(mobileControllerProvider).phase) {
      SessionPhase.restoring => const _LaunchScreen(),
      SessionPhase.locked => const _LockScreen(),
      SessionPhase.signedOut || SessionPhase.authenticating => AuthScreen(),
      SessionPhase.ready => destination == null
          ? MobileShell()
          : _DestinationGate(destination: destination!),
    };
  }
}

final class _DestinationGate extends ConsumerStatefulWidget {
  const _DestinationGate({required this.destination});

  final PushDestination destination;

  @override
  ConsumerState<_DestinationGate> createState() => _DestinationGateState();
}

final class _DestinationGateState extends ConsumerState<_DestinationGate> {
  var _opening = false;

  @override
  Widget build(BuildContext context) {
    if (!_opening) {
      _opening = true;
      WidgetsBinding.instance.addPostFrameCallback((_) async {
        await ref
            .read(mobileControllerProvider.notifier)
            .openPushDestination(widget.destination);
        if (context.mounted) context.go('/');
      });
    }
    return const _LaunchScreen();
  }
}

final class _LockScreen extends ConsumerWidget {
  const _LockScreen();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(mobileControllerProvider);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: EdgeInsets.all(28),
            child: ConstrainedBox(
              constraints: BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Center(
                    child: Container(
                      width: 64,
                      height: 64,
                      decoration: BoxDecoration(
                        color: context.kaede.coralSoft,
                        borderRadius: BorderRadius.circular(KaedeRadius.large),
                      ),
                      child: Icon(Icons.lock_rounded,
                          color: context.kaede.coralText, size: 30),
                    ),
                  ),
                  SizedBox(height: 22),
                  Text(
                    'Kaede is locked',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.headlineMedium,
                  ),
                  SizedBox(height: 8),
                  Text(
                    'Use your biometrics or device passcode to continue.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: context.kaede.muted, height: 1.4),
                  ),
                  if (state.error != null) ...[
                    SizedBox(height: 16),
                    Container(
                      padding: EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: context.kaede.dangerSoft,
                        borderRadius: BorderRadius.circular(KaedeRadius.medium),
                      ),
                      child: Text(
                        state.error!,
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: context.kaede.danger,
                          fontSize: 13,
                        ),
                      ),
                    ),
                  ],
                  SizedBox(height: 26),
                  FilledButton.icon(
                    onPressed: () =>
                        ref.read(mobileControllerProvider.notifier).unlock(),
                    icon: Icon(Icons.fingerprint_rounded),
                    label: Text('Unlock'),
                  ),
                  SizedBox(height: 6),
                  TextButton(
                    onPressed: () =>
                        ref.read(mobileControllerProvider.notifier).logout(),
                    child: Text('Sign out instead'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

final class _LaunchScreen extends StatelessWidget {
  const _LaunchScreen();

  @override
  Widget build(BuildContext context) => Scaffold(
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _BrandMark(),
              SizedBox(height: 18),
              Text(
                'Kaede',
                style: TextStyle(
                  fontSize: 26,
                  fontWeight: FontWeight.w800,
                  letterSpacing: -.5,
                ),
              ),
              SizedBox(height: 28),
              SizedBox.square(
                dimension: 20,
                child: CircularProgressIndicator(strokeWidth: 2.4),
              ),
            ],
          ),
        ),
      );
}

/// Coral app mark reused by the launch screen and the sign-in header.
final class _BrandMark extends StatelessWidget {
  const _BrandMark();

  static const size = 62.0;

  @override
  Widget build(BuildContext context) => Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          color: context.kaede.coral,
          borderRadius: BorderRadius.circular(size * .32),
        ),
        child: Icon(
          Icons.forum_rounded,
          color: context.kaede.onCoral,
          size: size * .48,
        ),
      );
}
