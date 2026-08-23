import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/e2ee/store.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

enum MobileLinkKind {
  invite,
  verifyEmail,
  resetPassword,
  emailChange,
  message,
}

final class MobileDeepLink {
  const MobileDeepLink({
    required this.kind,
    required this.instance,
    this.token,
    this.code,
    this.destination,
  });

  final MobileLinkKind kind;
  final Domain instance;
  final String? token;
  final String? code;
  final PushDestination? destination;

  bool get requiresSession =>
      kind == MobileLinkKind.invite ||
      kind == MobileLinkKind.emailChange ||
      kind == MobileLinkKind.message;

  String get signInNotice => switch (kind) {
        MobileLinkKind.invite => 'Sign in to review and accept this invite.',
        MobileLinkKind.emailChange => 'Sign in to confirm your email change.',
        MobileLinkKind.message => 'Sign in to open the linked message.',
        _ => 'Sign in to continue.',
      };

  static MobileDeepLink? parse(Uri uri) {
    try {
      final instanceText = uri.scheme == 'https' || uri.scheme == 'http'
          ? uri.host
          : uri.queryParameters['instance'];
      const configuredHost = String.fromEnvironment(
        'KAEDE_APP_LINK_HOST',
        defaultValue: 'kaede.chat',
      );
      final instance = Domain(
        instanceText?.isNotEmpty == true ? instanceText! : configuredHost,
      );
      final parts = uri.pathSegments;
      final fragment = uri.fragment.isEmpty
          ? const <String, String>{}
          : Uri.splitQueryString(uri.fragment);
      final token = uri.queryParameters['token'] ?? fragment['token'];
      if (parts.length == 2 && parts.first == 'invite') {
        return MobileDeepLink(
            kind: MobileLinkKind.invite, instance: instance, code: parts[1]);
      }
      if (parts.length == 1 &&
          (parts.first == 'verify' || parts.first == 'verify-email') &&
          token?.isNotEmpty == true) {
        return MobileDeepLink(
            kind: MobileLinkKind.verifyEmail, instance: instance, token: token);
      }
      if (parts.length == 1 &&
          parts.first == 'reset-password' &&
          token?.isNotEmpty == true) {
        return MobileDeepLink(
            kind: MobileLinkKind.resetPassword,
            instance: instance,
            token: token);
      }
      if (parts.length == 1 &&
          parts.first == 'verify-email-change' &&
          token?.isNotEmpty == true) {
        return MobileDeepLink(
            kind: MobileLinkKind.emailChange, instance: instance, token: token);
      }
      final around = uri.queryParameters['around'];
      if (parts.length == 3 && parts.first == 'g') {
        final channel = EntityRef.parse(parts[2]);
        return MobileDeepLink(
          kind: MobileLinkKind.message,
          instance: instance,
          destination: PushDestination(
            channel: channel,
            message:
                around?.isNotEmpty == true ? EntityRef.parse(around!) : null,
          ),
        );
      }
      if (parts.length == 2 && parts.first == 'home') {
        final channel = EntityRef.parse(parts[1]);
        return MobileDeepLink(
          kind: MobileLinkKind.message,
          instance: instance,
          destination: PushDestination(
            channel: channel,
            message:
                around?.isNotEmpty == true ? EntityRef.parse(around!) : null,
          ),
        );
      }
    } on Object {
      return null;
    }
    return null;
  }
}

final class DeepLinkActionScreen extends ConsumerStatefulWidget {
  const DeepLinkActionScreen({super.key, required this.link});
  final MobileDeepLink link;

  @override
  ConsumerState<DeepLinkActionScreen> createState() =>
      _DeepLinkActionScreenState();
}

final class _DeepLinkActionScreenState
    extends ConsumerState<DeepLinkActionScreen> {
  final _password = TextEditingController();
  final _confirmation = TextEditingController();
  var _running = false;
  String? _success;
  Object? _error;

  @override
  void initState() {
    super.initState();
    if (widget.link.kind != MobileLinkKind.resetPassword) {
      WidgetsBinding.instance.addPostFrameCallback((_) => unawaited(_run()));
    }
  }

  @override
  void dispose() {
    _password.dispose();
    _confirmation.dispose();
    super.dispose();
  }

  Future<void> _run() async {
    if (_running) return;
    if (widget.link.kind == MobileLinkKind.resetPassword) {
      if (_password.text.length < 10) {
        setState(() => _error = const UserInputException(
            'Use a password with at least 10 characters.'));
        return;
      }
      if (_password.text != _confirmation.text) {
        setState(
            () => _error = const UserInputException('Passwords do not match.'));
        return;
      }
    }
    setState(() {
      _running = true;
      _error = null;
    });
    try {
      final repository = ref.read(repositoryProvider);
      final controller = ref.read(mobileControllerProvider.notifier);
      switch (widget.link.kind) {
        case MobileLinkKind.invite:
          final home = controller.api.tokens?.instance;
          final code = home != null && home != widget.link.instance
              ? '${widget.link.code}@${widget.link.instance.value}'
              : widget.link.code!;
          await repository.acceptInvite(code);
          await controller.refreshNavigation();
          _success = 'Invite accepted.';
          break;
        case MobileLinkKind.verifyEmail:
          final restoreInstance = controller.api.tokens?.instance;
          repository.api.selectInstance(widget.link.instance);
          try {
            await repository.verifyEmail(widget.link.token!);
          } finally {
            if (restoreInstance != null) {
              repository.api.selectInstance(restoreInstance);
            }
          }
          _success = 'Email address verified. You can sign in now.';
          break;
        case MobileLinkKind.resetPassword:
          final accountRef = await repository.resetPassword(
            widget.link.instance,
            widget.link.token!,
            _password.text,
          );
          await const MobileE2EEStore().rebaseAfterPasswordReset(accountRef);
          _success = 'Password updated. Sign in with your new password.';
          break;
        case MobileLinkKind.emailChange:
          _requireMatchingSession(controller);
          await repository.confirmEmailChange(widget.link.token!);
          _success = 'Email change confirmed.';
          break;
        case MobileLinkKind.message:
          _requireMatchingSession(controller);
          final opened =
              await controller.openPushDestination(widget.link.destination!);
          if (!opened) {
            throw const UserInputException('That message is unavailable.');
          }
          if (mounted) context.go('/');
          return;
      }
    } on Object catch (error) {
      _error = error;
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  void _requireMatchingSession(MobileController controller) {
    if (controller.api.tokens?.instance != widget.link.instance) {
      throw UserInputException(
        'This link belongs to ${widget.link.instance.value}. Sign in to that home instance first.',
      );
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text(_title)),
        body: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 440),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Icon(_icon, size: 48, color: KaedeColors.coral),
                    const SizedBox(height: 18),
                    Text(_title,
                        textAlign: TextAlign.center,
                        style: Theme.of(context).textTheme.headlineMedium),
                    const SizedBox(height: 8),
                    Text(widget.link.instance.value,
                        textAlign: TextAlign.center,
                        style: const TextStyle(color: KaedeColors.muted)),
                    if (widget.link.kind == MobileLinkKind.resetPassword &&
                        _success == null) ...[
                      const SizedBox(height: 22),
                      TextField(
                        controller: _password,
                        obscureText: true,
                        decoration:
                            const InputDecoration(labelText: 'New password'),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: _confirmation,
                        obscureText: true,
                        decoration: const InputDecoration(
                            labelText: 'Confirm password'),
                      ),
                    ],
                    if (_error case final error?) ...[
                      const SizedBox(height: 16),
                      Text(userFacingError(error),
                          textAlign: TextAlign.center,
                          style: const TextStyle(color: KaedeColors.danger)),
                    ],
                    if (_success case final success?) ...[
                      const SizedBox(height: 16),
                      Text(success,
                          textAlign: TextAlign.center,
                          style: const TextStyle(color: KaedeColors.mint)),
                    ],
                    const SizedBox(height: 22),
                    if (_success != null)
                      FilledButton(
                        onPressed: () => context.go('/'),
                        child: const Text('Continue'),
                      )
                    else
                      FilledButton(
                        onPressed: _running ? null : _run,
                        child: _running
                            ? const SizedBox.square(
                                dimension: 18,
                                child:
                                    CircularProgressIndicator(strokeWidth: 2))
                            : Text(
                                widget.link.kind == MobileLinkKind.resetPassword
                                    ? 'Reset password'
                                    : 'Try again'),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );

  String get _title => switch (widget.link.kind) {
        MobileLinkKind.invite => 'Joining guild',
        MobileLinkKind.verifyEmail => 'Verify email',
        MobileLinkKind.resetPassword => 'Reset password',
        MobileLinkKind.emailChange => 'Confirm email change',
        MobileLinkKind.message => 'Opening message',
      };

  IconData get _icon => switch (widget.link.kind) {
        MobileLinkKind.invite => Icons.group_add_outlined,
        MobileLinkKind.verifyEmail ||
        MobileLinkKind.emailChange =>
          Icons.mark_email_read_outlined,
        MobileLinkKind.resetPassword => Icons.password_rounded,
        MobileLinkKind.message => Icons.chat_bubble_outline_rounded,
      };
}
