import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/features/auth/auth_screen.dart';
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
      theme: kaedeTheme(),
      restorationScopeId: 'kaede-mobile',
      routerConfig: _router,
      builder: (context, child) => AnnotatedRegion<SystemUiOverlayStyle>(
        value: kaedeSystemOverlay,
        child: child ?? const SizedBox.shrink(),
      ),
    );
  }
}

final class _SessionGate extends ConsumerWidget {
  const _SessionGate({this.destination});

  final PushDestination? destination;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return switch (ref.watch(mobileControllerProvider).phase) {
      SessionPhase.restoring => const _LaunchScreen(),
      SessionPhase.locked => const _LockScreen(),
      SessionPhase.signedOut ||
      SessionPhase.authenticating =>
        const AuthScreen(),
      SessionPhase.ready => destination == null
          ? const MobileShell()
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
            padding: const EdgeInsets.all(28),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Center(
                    child: Container(
                      width: 64,
                      height: 64,
                      decoration: BoxDecoration(
                        color: KaedeColors.coralSoft,
                        borderRadius: BorderRadius.circular(KaedeRadius.large),
                      ),
                      child: const Icon(Icons.lock_rounded,
                          color: KaedeColors.coralText, size: 30),
                    ),
                  ),
                  const SizedBox(height: 22),
                  Text(
                    'Kaede is locked',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.headlineMedium,
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    'Use your biometrics or device passcode to continue.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: KaedeColors.muted, height: 1.4),
                  ),
                  if (state.error != null) ...[
                    const SizedBox(height: 16),
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: KaedeColors.dangerSoft,
                        borderRadius: BorderRadius.circular(KaedeRadius.medium),
                      ),
                      child: Text(
                        state.error!,
                        textAlign: TextAlign.center,
                        style: const TextStyle(
                          color: KaedeColors.danger,
                          fontSize: 13,
                        ),
                      ),
                    ),
                  ],
                  const SizedBox(height: 26),
                  FilledButton.icon(
                    onPressed: () =>
                        ref.read(mobileControllerProvider.notifier).unlock(),
                    icon: const Icon(Icons.fingerprint_rounded),
                    label: const Text('Unlock'),
                  ),
                  const SizedBox(height: 6),
                  TextButton(
                    onPressed: () =>
                        ref.read(mobileControllerProvider.notifier).logout(),
                    child: const Text('Sign out instead'),
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
  Widget build(BuildContext context) => const Scaffold(
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
          color: KaedeColors.coral,
          borderRadius: BorderRadius.circular(size * .32),
        ),
        child: Icon(
          Icons.forum_rounded,
          color: KaedeColors.onCoral,
          size: size * .48,
        ),
      );
}
