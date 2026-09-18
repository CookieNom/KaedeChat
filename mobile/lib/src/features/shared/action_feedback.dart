import 'dart:async';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

void showActionFeedback(BuildContext context, String message,
    {bool error = false}) {
  if (!context.mounted || message.isEmpty) return;
  final colors = Theme.of(context).colorScheme;
  ScaffoldMessenger.of(context)
    ..clearSnackBars()
    ..showSnackBar(SnackBar(
      content: Text(message),
      backgroundColor: error ? colors.error : null,
      duration: Duration(seconds: error ? 6 : 4),
      showCloseIcon: true,
    ));
}

/// Keeps the actual future so actions cannot be submitted twice while pending.
class AsyncAction extends StatefulWidget {
  const AsyncAction({super.key, required this.onAction, required this.builder});

  final FutureOr<void> Function()? onAction;
  final Widget Function(BuildContext, VoidCallback?, bool) builder;

  @override
  State<AsyncAction> createState() => _AsyncActionState();
}

class _AsyncActionState extends State<AsyncAction> {
  bool _busy = false;

  Future<void> _run() async {
    final action = widget.onAction;
    if (_busy || action == null) return;
    _busy = true;
    try {
      final result = action();
      if (result is Future) {
        if (mounted) setState(() {});
        await result;
      }
    } on Object catch (error) {
      if (mounted) {
        showActionFeedback(context, userFacingError(error), error: true);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => widget.builder(
      context, _busy || widget.onAction == null ? null : _run, _busy);
}

class ActionProgress extends StatelessWidget {
  const ActionProgress({super.key});

  @override
  Widget build(BuildContext context) => SizedBox.square(
        dimension: 18,
        child: TickerMode(
          enabled: ModalRoute.of(context)?.isCurrent ?? true,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            semanticsLabel: L10n.of(context).ui_please_wait_ad7e2c40,
          ),
        ),
      );
}

enum ActionButtonKind { filled, outlined, text, icon, tonal }

/// Standard Material buttons with progress and duplicate-submission protection.
class ActionButton extends StatelessWidget {
  const ActionButton({
    super.key,
    required this.onPressed,
    this.child,
    this.label,
    this.icon,
    this.style,
    this.tooltip,
    this.iconSize,
    this.color,
    this.padding,
    this.constraints,
    this.visualDensity,
    this.kind = ActionButtonKind.filled,
  });

  final FutureOr<void> Function()? onPressed;
  final Widget? child;
  final Widget? label;
  final Widget? icon;
  final ButtonStyle? style;
  final String? tooltip;
  final double? iconSize;
  final Color? color;
  final EdgeInsetsGeometry? padding;
  final BoxConstraints? constraints;
  final VisualDensity? visualDensity;
  final ActionButtonKind kind;

  @override
  Widget build(BuildContext context) => AsyncAction(
        onAction: onPressed,
        builder: (context, run, busy) {
          final content = label ?? child ?? const SizedBox.shrink();
          final glyph = busy ? const ActionProgress() : icon;
          final body = glyph == null
              ? content
              : Row(mainAxisSize: MainAxisSize.min, children: [
                  glyph,
                  const SizedBox(width: 8),
                  Flexible(child: content),
                ]);
          return switch (kind) {
            ActionButtonKind.filled =>
              FilledButton(onPressed: run, style: style, child: body),
            ActionButtonKind.tonal =>
              FilledButton.tonal(onPressed: run, style: style, child: body),
            ActionButtonKind.outlined =>
              OutlinedButton(onPressed: run, style: style, child: body),
            ActionButtonKind.text =>
              TextButton(onPressed: run, style: style, child: body),
            ActionButtonKind.icon => IconButton(
                onPressed: run,
                tooltip: tooltip,
                iconSize: iconSize,
                color: color,
                padding: padding,
                constraints: constraints,
                visualDensity: visualDensity,
                style: style,
                icon: glyph ?? content,
              ),
          };
        },
      );
}

/// List actions use the same pending state as buttons, including modal openers.
class ActionTile extends StatelessWidget {
  const ActionTile({
    super.key,
    this.title,
    this.subtitle,
    this.leading,
    this.trailing,
    this.onTap,
    this.contentPadding,
    this.selected = false,
    this.enabled = true,
    this.dense,
    this.isThreeLine = false,
  });

  final Widget? title;
  final Widget? subtitle;
  final Widget? leading;
  final Widget? trailing;
  final FutureOr<void> Function()? onTap;
  final EdgeInsetsGeometry? contentPadding;
  final bool selected;
  final bool enabled;
  final bool? dense;
  final bool isThreeLine;

  @override
  Widget build(BuildContext context) => AsyncAction(
        onAction: enabled ? onTap : null,
        builder: (context, run, busy) => ListTile(
          title: title,
          subtitle: subtitle,
          leading: leading,
          trailing: busy ? const ActionProgress() : trailing,
          onTap: run,
          contentPadding: contentPadding,
          selected: selected,
          enabled: enabled && !busy,
          dense: dense,
          isThreeLine: isThreeLine,
        ),
      );
}
