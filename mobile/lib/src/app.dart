import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
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
          child: Padding(
            padding: const EdgeInsets.all(28),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Card(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.lock_rounded,
                          color: KaedeColors.coral, size: 52),
                      const SizedBox(height: 16),
                      Text('Kaede is locked',
                          style: Theme.of(context).textTheme.headlineSmall),
                      const SizedBox(height: 8),
                      const Text(
                        'Use your biometrics or device passcode to continue.',
                        textAlign: TextAlign.center,
                        style: TextStyle(color: KaedeColors.muted),
                      ),
                      if (state.error != null) ...[
                        const SizedBox(height: 12),
                        Text(state.error!,
                            textAlign: TextAlign.center,
                            style: const TextStyle(color: KaedeColors.danger)),
                      ],
                      const SizedBox(height: 22),
                      SizedBox(
                        width: double.infinity,
                        child: FilledButton.icon(
                          onPressed: () => ref
                              .read(mobileControllerProvider.notifier)
                              .unlock(),
                          icon: const Icon(Icons.fingerprint_rounded),
                          label: const Text('Unlock'),
                        ),
                      ),
                      TextButton(
                        onPressed: () => ref
                            .read(mobileControllerProvider.notifier)
                            .logout(),
                        child: const Text('Sign out instead'),
                      ),
                    ],
                  ),
                ),
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
              Icon(Icons.forum_rounded, color: KaedeColors.coral, size: 58),
              SizedBox(height: 18),
              Text('Kaede',
                  style: TextStyle(fontSize: 30, fontWeight: FontWeight.w800)),
              SizedBox(height: 24),
              CircularProgressIndicator(),
            ],
          ),
        ),
      );
}
