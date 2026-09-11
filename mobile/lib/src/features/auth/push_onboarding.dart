import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Lives inside the session gate so signing out or locking also removes it.
final class PushOnboarding extends ConsumerStatefulWidget {
  const PushOnboarding({super.key, required this.child});

  final Widget child;

  @override
  ConsumerState<PushOnboarding> createState() => _PushOnboardingState();
}

final class _PushOnboardingState extends ConsumerState<PushOnboarding> {
  bool _visible = false;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _offerNotifications();
  }

  Future<void> _offerNotifications() async {
    final controller = ref.read(mobileControllerProvider.notifier);
    final accountKey = controller.api.tokens?.accountKey;
    if (accountKey == null) return;
    try {
      final preferences = await SharedPreferences.getInstance();
      final key = 'kaede.push-onboarding-shown.$accountKey';
      if (preferences.getBool(key) == true ||
          !await controller.shouldOfferPushNotifications() ||
          !mounted ||
          controller.api.tokens?.accountKey != accountKey) {
        return;
      }
      await preferences.setBool(key, true);
      if (mounted && controller.api.tokens?.accountKey == accountKey) {
        setState(() => _visible = true);
      }
    } on Object {
      // An optional prompt must not prevent access to chat. Settings can retry.
    }
  }

  Future<void> _choose(bool enable) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final controller = ref.read(mobileControllerProvider.notifier);
    try {
      if (enable) {
        final enabled = await controller.enablePushNotifications();
        if (!mounted) return;
        if (!enabled) {
          setState(() => _error = controller.currentState.pushWarning ??
              'Notifications could not be enabled. Check system notification permissions and try again.');
          return;
        }
      } else {
        await controller.api.savePushOptIn(false);
      }
      if (mounted) setState(() => _visible = false);
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(error,
            summary: 'Could not enable background notifications'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final controller = ref.read(mobileControllerProvider.notifier);
    return PopScope(
      canPop: !_visible,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && _visible && !_busy) _choose(false);
      },
      child: Stack(
        fit: StackFit.expand,
        children: [
          ExcludeFocus(
            excluding: _visible,
            child: ExcludeSemantics(excluding: _visible, child: widget.child),
          ),
          if (_visible) ...[
            const ModalBarrier(dismissible: false, color: Colors.black54),
            AlertDialog(
              title: const Text('Enable notifications?'),
              scrollable: true,
              content: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                      'Get messages, mentions and incoming calls while Kaede is closed. You can change this later in Settings.'),
                  if (controller.usesPushRelay) ...[
                    const SizedBox(height: 12),
                    Text(
                        'Notifications for ${controller.api.tokens?.instance.value} use Kaede Push Relay (${controller.pushRelayHost}). Message content is never sent to the relay.'),
                  ],
                  if (_error case final error?) ...[
                    const SizedBox(height: 12),
                    Semantics(liveRegion: true, child: Text(error)),
                  ],
                ],
              ),
              actions: [
                TextButton(
                  onPressed: _busy ? null : () => _choose(false),
                  child: const Text('Not now'),
                ),
                FilledButton(
                  onPressed: _busy ? null : () => _choose(true),
                  child: Text(_busy ? 'Please wait…' : 'Enable notifications'),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}
