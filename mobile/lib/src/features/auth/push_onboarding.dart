import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/push_diagnostics_button.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
            summary: L10n.of(context)
                .ui_could_not_enable_background_notifications_76cccb52));
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
              title: Text(L10n.of(context).ui_enable_notifications_aabb34b3),
              scrollable: true,
              content: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(L10n.of(context)
                      .ui_get_messages_mentions_and_incoming_calls_whil_b2d7822c),
                  if (controller.usesPushRelay) ...[
                    const SizedBox(height: 12),
                    Text(L10n.of(context)
                        .ui_notifications_for_value0_use_kaede_push_relay_d039f851(
                            (controller.api.tokens?.instance.value).toString(),
                            (controller.pushRelayHost).toString())),
                  ],
                  if (_error case final error?) ...[
                    const SizedBox(height: 12),
                    Semantics(
                        liveRegion: true,
                        child: SettingsStatusPanel.error(
                            message: error, onRetry: null)),
                    if (controller.pushSetupDiagnostics
                        case final diagnostics?) ...[
                      const SizedBox(height: 12),
                      PushDiagnosticsButton(diagnostics: diagnostics),
                    ],
                  ],
                ],
              ),
              actions: [
                ActionButton(
                  kind: ActionButtonKind.text,
                  onPressed: _busy ? null : () => _choose(false),
                  child: Text(L10n.of(context).ui_not_now_2c3d6fce),
                ),
                ActionButton(
                  onPressed: _busy ? null : () => _choose(true),
                  child: Text(_busy
                      ? L10n.of(context).ui_please_wait_ad7e2c40
                      : L10n.of(context).ui_enable_notifications_456c745e),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}
