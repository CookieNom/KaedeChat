import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/auth/turnstile_actions.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/features/auth/turnstile_challenge.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class AuthScreen extends ConsumerStatefulWidget {
  const AuthScreen({super.key});

  @override
  ConsumerState<AuthScreen> createState() => _AuthScreenState();
}

final class _AuthScreenState extends ConsumerState<AuthScreen> {
  final instance = TextEditingController(text: 'kaede.chat');
  final identifier = TextEditingController();
  final password = TextEditingController();
  final username = TextEditingController();
  final email = TextEditingController();
  final confirmation = TextEditingController();
  var register = false;
  var obscure = true;
  final Map<Domain, Map<String, Object?>> _configs = {};
  var _submitting = false;

  @override
  void dispose() {
    instance.dispose();
    identifier.dispose();
    password.dispose();
    username.dispose();
    email.dispose();
    confirmation.dispose();
    super.dispose();
  }

  Future<void> submit() async {
    if (_submitting) return;
    setState(() => _submitting = true);
    FocusScope.of(context).unfocus();
    try {
      final domain = Domain(instance.text);
      final serverConfig = await _config(domain);
      if (!mounted) return;
      if (register) {
        if (password.text != confirmation.text) {
          throw const FormatException('Passwords do not match.');
        }
        if (serverConfig['email_required'] == true &&
            email.text.trim().isEmpty) {
          throw const FormatException('This server requires an email address.');
        }
        String? challenge;
        final turnstile = serverConfig['turnstile'];
        if (turnstile is Map<Object?, Object?> &&
            turnstile['enabled'] == true) {
          challenge = await TurnstileChallenge.show(
            context,
            instance: domain,
            action: TurnstileActions.register,
          );
          if (challenge == null) return;
        }
        final created = await ref.read(repositoryProvider).register(
              instance: domain,
              username: username.text.trim(),
              email: email.text.trim(),
              password: password.text,
              turnstileToken: challenge,
            );
        if (!mounted) return;
        if (created['email_verification_required'] == true) {
          await _verificationDialog(domain);
        } else {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
                content: Text('Account created. You can sign in now.')),
          );
        }
        if (mounted) setState(() => register = false);
      } else {
        try {
          await ref.read(mobileControllerProvider.notifier).login(
                instance: domain,
                identifier: identifier.text.trim(),
                password: password.text,
              );
        } on KaedeException catch (error) {
          if (error.code != 'TURNSTILE_REQUIRED' || !mounted) rethrow;
          final challenge = await TurnstileChallenge.show(
            context,
            instance: domain,
            action: TurnstileActions.login,
          );
          if (challenge == null) return;
          await ref.read(mobileControllerProvider.notifier).login(
                instance: domain,
                identifier: identifier.text.trim(),
                password: password.text,
                turnstileToken: challenge,
              );
        }
      }
    } on MfaRequired catch (mfa) {
      if (!mounted) return;
      final code = await showDialog<String>(
          context: context, builder: (_) => const _MfaDialog());
      if (code != null) {
        try {
          await ref
              .read(mobileControllerProvider.notifier)
              .finishMfa(Domain(instance.text), mfa.ticket, code);
        } on Object catch (error) {
          if (mounted) {
            ScaffoldMessenger.of(context)
                .showSnackBar(SnackBar(content: Text('$error')));
          }
        }
      }
    } on Object catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$error')));
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<Map<String, Object?>> _config(Domain domain) async {
    if (_configs[domain] case final cached?) return cached;
    final loaded = await ref.read(repositoryProvider).authConfig(domain);
    if (mounted) setState(() => _configs[domain] = loaded);
    return loaded;
  }

  Future<void> _verificationDialog(Domain domain) async {
    final token = TextEditingController();
    try {
      await showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (dialogContext) => _EmailVerificationDialog(
          token: token,
          email: email.text.trim(),
          repository: ref.read(repositoryProvider),
        ),
      );
    } finally {
      token.dispose();
    }
  }

  Future<void> _forgotPassword() async {
    try {
      final domain = Domain(instance.text);
      final serverConfig = await _config(domain);
      if (serverConfig['password_recovery_enabled'] != true) {
        throw const FormatException(
            'Password recovery is not enabled on this server.');
      }
      if (!mounted) return;
      final emailAddress = await _prompt('Reset password', 'Account email');
      if (emailAddress == null) return;
      await ref.read(repositoryProvider).forgotPassword(emailAddress);
      if (!mounted) return;
      final token = await _prompt('Enter reset token', 'Token from your email');
      if (token == null) return;
      final replacement = await _prompt(
          'Choose a new password', 'At least 10 characters',
          secret: true);
      if (replacement == null) return;
      await ref.read(repositoryProvider).resetPassword(token, replacement);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
              content: Text('Password updated. You can sign in now.')),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$error')));
      }
    }
  }

  Future<String?> _prompt(String title, String hint,
      {bool secret = false}) async {
    final controller = TextEditingController();
    final result = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(title),
        content: TextField(
          controller: controller,
          autofocus: true,
          obscureText: secret,
          decoration: InputDecoration(hintText: hint),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, controller.text.trim()),
              child: const Text('Continue')),
        ],
      ),
    );
    controller.dispose();
    return result?.isEmpty == true ? null : result;
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(mobileControllerProvider);
    return Scaffold(
      body: SafeArea(
        child: CustomScrollView(
          slivers: [
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(24, 28, 24, 40),
              sliver: SliverList.list(
                children: [
                  const Row(
                    children: [
                      DecoratedBox(
                        decoration: BoxDecoration(
                            color: KaedeColors.coral,
                            borderRadius:
                                BorderRadius.all(Radius.circular(14))),
                        child: Padding(
                            padding: EdgeInsets.all(11),
                            child: Icon(Icons.forum_rounded,
                                color: KaedeColors.canvas)),
                      ),
                      SizedBox(width: 12),
                      Text('Kaede Chat',
                          style: TextStyle(
                              fontSize: 21, fontWeight: FontWeight.w800)),
                    ],
                  ),
                  const SizedBox(height: 48),
                  Text(register ? 'Create your account.' : 'Welcome back.',
                      style: Theme.of(context).textTheme.displayLarge),
                  const SizedBox(height: 12),
                  Text(
                    register
                        ? 'Choose the server that will own your identity. You can still join communities across the fediverse.'
                        : 'Sign in through your home server—the server where your account was created.',
                    style:
                        const TextStyle(color: KaedeColors.muted, fontSize: 17),
                  ),
                  const SizedBox(height: 30),
                  const _Label('HOME SERVER'),
                  TextField(
                    controller: instance,
                    keyboardType: TextInputType.url,
                    textInputAction: TextInputAction.next,
                    decoration: const InputDecoration(
                      hintText: 'kaede.chat',
                      helperText:
                          'Not sure? kaede.chat is the recommended public server.',
                      prefixIcon: Icon(Icons.language_rounded),
                    ),
                  ),
                  const SizedBox(height: 18),
                  if (register) ...[
                    const _Label('USERNAME'),
                    TextField(
                        controller: username,
                        textInputAction: TextInputAction.next),
                    const SizedBox(height: 18),
                    const _Label('EMAIL'),
                    TextField(
                        controller: email,
                        keyboardType: TextInputType.emailAddress,
                        textInputAction: TextInputAction.next),
                  ] else ...[
                    const _Label('USERNAME OR EMAIL'),
                    TextField(
                        controller: identifier,
                        textInputAction: TextInputAction.next),
                  ],
                  const SizedBox(height: 18),
                  const _Label('PASSWORD'),
                  TextField(
                    controller: password,
                    obscureText: obscure,
                    textInputAction:
                        register ? TextInputAction.next : TextInputAction.done,
                    onSubmitted: (_) => register ? null : submit(),
                    decoration: InputDecoration(
                      suffixIcon: IconButton(
                        onPressed: () => setState(() => obscure = !obscure),
                        icon: Icon(obscure
                            ? Icons.visibility_rounded
                            : Icons.visibility_off_rounded),
                      ),
                    ),
                  ),
                  if (register) ...[
                    const SizedBox(height: 18),
                    const _Label('CONFIRM PASSWORD'),
                    TextField(
                        controller: confirmation,
                        obscureText: obscure,
                        textInputAction: TextInputAction.done,
                        onSubmitted: (_) => submit()),
                  ],
                  if (state.error case final error?) ...[
                    const SizedBox(height: 16),
                    Text(error,
                        style: const TextStyle(color: KaedeColors.danger)),
                  ],
                  const SizedBox(height: 26),
                  FilledButton.icon(
                    onPressed: state.phase == SessionPhase.authenticating ||
                            _submitting
                        ? null
                        : submit,
                    icon: state.phase == SessionPhase.authenticating ||
                            _submitting
                        ? const SizedBox.square(
                            dimension: 18,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : Icon(register
                            ? Icons.person_add_alt_1_rounded
                            : Icons.login_rounded),
                    label: Text(register ? 'Create account' : 'Sign in'),
                  ),
                  const SizedBox(height: 10),
                  TextButton(
                    onPressed: () => setState(() => register = !register),
                    child: Text(register
                        ? 'Already have an account? Sign in'
                        : 'Need an account? Create one'),
                  ),
                  if (!register)
                    TextButton(
                      onPressed: _forgotPassword,
                      child: const Text('Forgot password?'),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

final class _EmailVerificationDialog extends StatefulWidget {
  const _EmailVerificationDialog({
    required this.token,
    required this.email,
    required this.repository,
  });

  final TextEditingController token;
  final String email;
  final KaedeRepository repository;

  @override
  State<_EmailVerificationDialog> createState() =>
      _EmailVerificationDialogState();
}

final class _EmailVerificationDialogState
    extends State<_EmailVerificationDialog> {
  var _busy = false;
  String? _error;

  Future<void> _run(Future<void> Function() operation) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await operation();
    } on Object catch (error) {
      if (mounted) setState(() => _error = '$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _resend() => _run(() async {
        await widget.repository.resendVerification(widget.email);
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('Verification email requested.')),
          );
        }
      });

  Future<void> _verify() => _run(() async {
        final value = widget.token.text.trim();
        if (value.isEmpty) {
          throw const FormatException('Enter the verification token.');
        }
        await widget.repository.verifyEmail(value);
        if (mounted) Navigator.pop(context);
      });

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Verify your email'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'We sent a verification link to your email. Open it, or paste its token below.',
            ),
            const SizedBox(height: 14),
            TextField(
              controller: widget.token,
              enabled: !_busy,
              decoration:
                  const InputDecoration(labelText: 'Verification token'),
              onSubmitted: (_) => _verify(),
            ),
            if (_error case final error?) ...[
              const SizedBox(height: 10),
              Text(error,
                  style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ],
            if (_busy) ...[
              const SizedBox(height: 12),
              const LinearProgressIndicator(),
            ],
          ],
        ),
        actions: [
          TextButton(
            onPressed: _busy ? null : _resend,
            child: const Text('Resend'),
          ),
          TextButton(
            onPressed: _busy ? null : () => Navigator.pop(context),
            child: const Text('Verify later'),
          ),
          FilledButton(
            onPressed: _busy ? null : _verify,
            child: const Text('Verify'),
          ),
        ],
      );
}

final class _Label extends StatelessWidget {
  const _Label(this.text);
  final String text;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(text,
            style: const TextStyle(
                color: KaedeColors.muted,
                fontWeight: FontWeight.w800,
                letterSpacing: 1.2)),
      );
}

final class _MfaDialog extends StatefulWidget {
  const _MfaDialog();
  @override
  State<_MfaDialog> createState() => _MfaDialogState();
}

final class _MfaDialogState extends State<_MfaDialog> {
  final controller = TextEditingController();
  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Two-factor authentication'),
        content: TextField(
            controller: controller,
            autofocus: true,
            keyboardType: TextInputType.number,
            decoration: const InputDecoration(
                hintText: '6-digit code or recovery code')),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, controller.text.trim()),
              child: const Text('Continue')),
        ],
      );
}
