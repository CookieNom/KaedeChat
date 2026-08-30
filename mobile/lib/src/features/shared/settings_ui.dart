// Discord-flavoured settings building blocks: flat surfaces, uppercase
// section headers, hover rows and Discord-style toggles. The palette stays
// Kaede's, so these read as the same product on every surface.
import 'package:flutter/material.dart';

/// Theme-aware layers shared by every settings surface. These deliberately use
/// Material color roles so a stored light preference changes custom panes as
/// well as stock controls.
Color settingsSurface(BuildContext context) =>
    Theme.of(context).colorScheme.surfaceContainerLow;

Color settingsRowHover(BuildContext context) =>
    Theme.of(context).colorScheme.surfaceContainerHighest;

Color settingsDividerColor(BuildContext context) =>
    Theme.of(context).colorScheme.outlineVariant;

/// Uppercase section header, the way Discord labels each settings group.
class SettingsSectionHeader extends StatelessWidget {
  const SettingsSectionHeader(this.title,
      {super.key, this.subheading, this.top = 26});

  final String title;
  final String? subheading;
  final double top;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Padding(
      padding: EdgeInsets.fromLTRB(4, top, 4, 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title.toUpperCase(),
            style: TextStyle(
              color: colors.onSurfaceVariant,
              fontSize: 11.5,
              fontWeight: FontWeight.w700,
              letterSpacing: 1.05,
            ),
          ),
          if (subheading case final note?) ...[
            const SizedBox(height: 4),
            Text(
              note,
              style: TextStyle(
                color: colors.onSurfaceVariant,
                fontSize: 12.5,
                height: 1.4,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// Muted guidance paragraph inside a settings section.
class SettingsInfo extends StatelessWidget {
  const SettingsInfo(this.text, {super.key, this.padding});

  final String text;
  final EdgeInsets? padding;

  @override
  Widget build(BuildContext context) => Padding(
        padding: padding ?? const EdgeInsets.fromLTRB(4, 2, 4, 10),
        child: Text(
          text,
          style: TextStyle(
            color: Theme.of(context).colorScheme.onSurfaceVariant,
            fontSize: 12.5,
            height: 1.45,
          ),
        ),
      );
}

/// Reusable inline result state for settings and administration pages.
/// Keeping retry/error styling here avoids each resource screen inventing a
/// subtly different card for the same request lifecycle.
class SettingsStatusPanel extends StatelessWidget {
  const SettingsStatusPanel.error({
    required this.message,
    required this.onRetry,
    super.key,
  }) : isError = true;

  const SettingsStatusPanel.notice({
    required this.message,
    super.key,
  })  : isError = false,
        onRetry = null;

  final String message;
  final VoidCallback? onRetry;
  final bool isError;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Card(
      color: isError ? colors.errorContainer : colors.secondaryContainer,
      child: ListTile(
        leading: Icon(
          isError ? Icons.error_outline : Icons.check_circle_outline,
          color: isError ? colors.onErrorContainer : null,
        ),
        title: Text(message),
        trailing: onRetry == null
            ? null
            : TextButton(onPressed: onRetry, child: const Text('Retry')),
      ),
    );
  }
}

Future<String?> showSettingsTextDialog(
  BuildContext context, {
  required String title,
  required String label,
  String initialValue = '',
  int? maxLength,
}) async {
  final input = TextEditingController(text: initialValue);
  try {
    return await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(title),
        content: TextField(
          controller: input,
          autofocus: true,
          maxLength: maxLength,
          decoration: InputDecoration(labelText: label),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('Cancel'),
          ),
          ValueListenableBuilder<TextEditingValue>(
            valueListenable: input,
            builder: (context, value, child) => FilledButton(
              onPressed: value.text.trim().isEmpty
                  ? null
                  : () => Navigator.pop(dialogContext, value.text.trim()),
              child: const Text('Save'),
            ),
          ),
        ],
      ),
    );
  } finally {
    input.dispose();
  }
}

Future<bool> showSettingsConfirmation(
  BuildContext context, {
  required String message,
  String title = 'Confirm',
  String actionLabel = 'Continue',
}) async =>
    await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(title),
        content: Text(message),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(actionLabel),
          ),
        ],
      ),
    ) ??
    false;

/// One flat settings row: leading glyph, title, optional subtitle and a
/// trailing control. Pressed rows use the theme's highest container layer,
/// Discord style, instead of sitting in a bordered card.
class SettingsRow extends StatelessWidget {
  const SettingsRow({
    super.key,
    required this.title,
    this.subtitle,
    this.leading,
    this.trailing,
    this.onTap,
    this.minHeight = 50,
    this.divider = false,
    this.danger = false,
  });

  /// Chevron row used for settings that open a sheet or page.
  const SettingsRow.chevron({
    super.key,
    required this.title,
    this.subtitle,
    this.leading,
    this.onTap,
    this.minHeight = 50,
    this.divider = false,
    this.danger = false,
  }) : trailing = null;

  final String title;
  final String? subtitle;
  final Widget? leading;
  final Widget? trailing;
  final VoidCallback? onTap;
  final double minHeight;
  final bool divider;
  final bool danger;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final hasSubtitle = subtitle?.isNotEmpty == true;
    final row = InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: ConstrainedBox(
        constraints: BoxConstraints(minHeight: minHeight),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 4),
          child: Row(
            children: [
              if (leading case final Widget icon) ...[
                icon,
                const SizedBox(width: 12),
              ],
              Expanded(
                child: Padding(
                  padding: EdgeInsets.symmetric(vertical: hasSubtitle ? 9 : 11),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: danger ? colors.error : colors.onSurface,
                          fontSize: 15,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      if (hasSubtitle) ...[
                        const SizedBox(height: 1),
                        Text(
                          subtitle!,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: colors.onSurfaceVariant,
                            fontSize: 12.5,
                            height: 1.35,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
              if (trailing case final Widget control) ...[
                const SizedBox(width: 8),
                control,
              ],
            ],
          ),
        ),
      ),
    );
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        row,
        if (divider)
          Padding(
            padding: EdgeInsets.symmetric(horizontal: 4),
            child: SizedBox(
              height: 1,
              child: DecoratedBox(
                decoration: BoxDecoration(color: colors.outlineVariant),
              ),
            ),
          ),
      ],
    );
  }
}

/// Discord-style toggle: white track with a dark knob when on, a dark
/// outlined track with a muted knob when off.
class DiscordSwitch extends StatefulWidget {
  const DiscordSwitch({super.key, required this.value, this.onChanged});

  final bool value;
  final ValueChanged<bool>? onChanged;

  @override
  State<DiscordSwitch> createState() => _DiscordSwitchState();
}

class _DiscordSwitchState extends State<DiscordSwitch> {
  @override
  Widget build(BuildContext context) {
    final on = widget.value;
    final colors = Theme.of(context).colorScheme;
    return InkWell(
      onTap: widget.onChanged == null ? null : () => widget.onChanged!(!on),
      borderRadius: BorderRadius.circular(14),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        curve: Curves.easeOut,
        width: 42,
        height: 24,
        padding: const EdgeInsets.all(3),
        decoration: BoxDecoration(
          color: on ? colors.primary : colors.surfaceContainerHigh,
          borderRadius: BorderRadius.circular(14),
          border: on ? null : Border.all(color: colors.outline),
        ),
        child: Align(
          alignment: on ? Alignment.centerRight : Alignment.centerLeft,
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 150),
            curve: Curves.easeOut,
            width: 16,
            height: 16,
            decoration: BoxDecoration(
              color: on ? colors.onPrimary : colors.onSurfaceVariant,
              shape: BoxShape.circle,
            ),
          ),
        ),
      ),
    );
  }
}

/// Toggle row: tapping the row or the switch flips the value.
class SettingsSwitchRow extends StatelessWidget {
  const SettingsSwitchRow({
    super.key,
    required this.title,
    required this.value,
    required this.onChanged,
    this.subtitle,
    this.leading,
    this.divider = false,
  });

  final String title;
  final String? subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;
  final Widget? leading;
  final bool divider;

  @override
  Widget build(BuildContext context) => SettingsRow(
        title: title,
        subtitle: subtitle,
        leading: leading,
        divider: divider,
        minHeight: subtitle?.isNotEmpty == true ? 56 : 50,
        trailing: DiscordSwitch(value: value, onChanged: onChanged),
        onTap: () => onChanged(!value),
      );
}

/// One option in a [showSettingsChoiceSheet] list.
class SettingsChoice {
  const SettingsChoice(this.value, this.label, {this.hint});

  final String value;
  final String label;
  final String? hint;
}

/// Bottom sheet of radio-style options, Discord mobile's way of presenting
/// a choice instead of a dropdown.
Future<String?> showSettingsChoiceSheet(
  BuildContext context, {
  required String title,
  String? description,
  required List<SettingsChoice> choices,
  String? selected,
}) {
  return showModalBottomSheet<String>(
    context: context,
    isScrollControlled: true,
    builder: (sheetContext) => Padding(
      padding: EdgeInsets.fromLTRB(
          20, 8, 20, MediaQuery.of(sheetContext).viewInsets.bottom + 24),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 560),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: Theme.of(sheetContext).textTheme.headlineSmall),
            if (description case final note?) ...[
              const SizedBox(height: 6),
              Text(
                note,
                style: TextStyle(
                  color: Theme.of(sheetContext).colorScheme.onSurfaceVariant,
                  fontSize: 13,
                  height: 1.4,
                ),
              ),
            ],
            const SizedBox(height: 18),
            for (final choice in choices)
              _ChoiceTile(
                choice: choice,
                selected: choice.value == selected,
                onSelected: () => Navigator.pop(sheetContext, choice.value),
              ),
          ],
        ),
      ),
    ),
  );
}

class _ChoiceTile extends StatelessWidget {
  const _ChoiceTile({
    required this.choice,
    required this.selected,
    required this.onSelected,
  });

  final SettingsChoice choice;
  final bool selected;
  final VoidCallback onSelected;

  @override
  Widget build(BuildContext context) {
    final hint = choice.hint;
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        InkWell(
          onTap: onSelected,
          borderRadius: BorderRadius.circular(10),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        choice.label,
                        style: const TextStyle(
                          fontSize: 15,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      if (hint case final note?) ...[
                        const SizedBox(height: 2),
                        Text(
                          note,
                          style: TextStyle(
                            color:
                                Theme.of(context).colorScheme.onSurfaceVariant,
                            fontSize: 12.5,
                            height: 1.35,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                Icon(
                  selected
                      ? Icons.check_circle_rounded
                      : Icons.radio_button_unchecked_rounded,
                  color: selected
                      ? Theme.of(context).colorScheme.primary
                      : Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 4),
      ],
    );
  }
}

/// Row that shows the current value and opens a choice sheet.
class SettingsChoiceRow extends StatelessWidget {
  const SettingsChoiceRow({
    super.key,
    required this.title,
    required this.value,
    required this.display,
    required this.onSelected,
    this.subtitle,
    this.leading,
    this.divider = false,
  });

  final String title;
  final String value;
  final String display;
  final ValueChanged<String> onSelected;
  final String? subtitle;
  final Widget? leading;
  final bool divider;

  @override
  Widget build(BuildContext context) => SettingsRow(
        title: title,
        subtitle: subtitle,
        leading: leading,
        divider: divider,
        trailing: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 148),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Expanded(
                child: Text(
                  display,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  textAlign: TextAlign.end,
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                    fontSize: 13.5,
                  ),
                ),
              ),
              const SizedBox(width: 2),
              Icon(
                Icons.chevron_right_rounded,
                size: 18,
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ],
          ),
        ),
        onTap: () => onSelected(value),
      );
}

/// Full-width primary action inside a settings section.
class SettingsPrimaryButton extends StatelessWidget {
  const SettingsPrimaryButton(
    this.label, {
    super.key,
    required this.onPressed,
    this.icon,
  });

  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;

  @override
  Widget build(BuildContext context) => SizedBox(
        width: double.infinity,
        child: icon == null
            ? FilledButton(onPressed: onPressed, child: Text(label))
            : FilledButton.icon(
                onPressed: onPressed,
                icon: Icon(icon),
                label: Text(label),
              ),
      );
}

/// Full-width destructive text action, Discord's red "Log out" style.
class SettingsDangerButton extends StatelessWidget {
  const SettingsDangerButton(this.label, {super.key, required this.onPressed});

  final String label;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => SizedBox(
        width: double.infinity,
        child: TextButton(
          onPressed: onPressed,
          style: TextButton.styleFrom(
            foregroundColor: Theme.of(context).colorScheme.error,
            minimumSize: const Size(0, 44),
          ),
          child: Text(label,
              style:
                  const TextStyle(fontWeight: FontWeight.w700, fontSize: 14.5)),
        ),
      );
}

/// Static-label text input, Discord's profile fields: a small label above a
/// filled field rather than a floating label.
class SettingsField extends StatelessWidget {
  const SettingsField({
    super.key,
    required this.label,
    required this.controller,
    this.hint,
    this.maxLines = 1,
    this.maxLength,
    this.keyboardType,
    this.obscureText = false,
    this.enabled = true,
    this.onSubmitted,
    this.autofocus = false,
    this.autofillHints = const <String>[],
  });

  final String label;
  final TextEditingController controller;
  final String? hint;
  final int maxLines;
  final int? maxLength;
  final TextInputType? keyboardType;
  final bool obscureText;
  final bool enabled;
  final ValueChanged<String>? onSubmitted;
  final bool autofocus;
  final Iterable<String> autofillHints;

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: TextStyle(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
              fontSize: 12.5,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          TextField(
            controller: controller,
            decoration: InputDecoration(
              hintText: hint,
              hintStyle: TextStyle(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ),
            maxLines: maxLines == 1 ? null : maxLines,
            maxLength: maxLength,
            keyboardType: keyboardType,
            obscureText: obscureText,
            enabled: enabled,
            onSubmitted: onSubmitted,
            autofocus: autofocus,
            autofillHints: autofillHints.toList(),
          ),
        ],
      );
}

/// Small circular icon button overlaid on an avatar or guild icon, the way
/// Discord offers "change" directly on the image.
class SettingsImageOverlayButton extends StatelessWidget {
  const SettingsImageOverlayButton({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onPressed,
    this.size = 28,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;
  final double size;

  @override
  Widget build(BuildContext context) => Material(
        color: Theme.of(context).colorScheme.surfaceDim.withValues(alpha: .86),
        borderRadius: BorderRadius.circular(size / 2),
        child: InkWell(
          onTap: onPressed,
          customBorder: const CircleBorder(),
          child: Center(
            child: Padding(
              padding: EdgeInsets.all(size * .24),
              child: Tooltip(
                message: tooltip,
                child: Icon(
                  icon,
                  size: size * .5,
                  color: Theme.of(context).colorScheme.onSurface,
                ),
              ),
            ),
          ),
        ),
      );
}
