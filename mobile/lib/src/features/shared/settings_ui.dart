// Discord-flavoured settings building blocks: flat surfaces, uppercase
// section headers, hover rows and Discord-style toggles. The palette stays
// Kaede's, so these read as the same product on every surface.
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

/// Flat page background for settings panes. Matches the shell section
/// screens, so pushed settings read as one continuous surface.
const kSettingsSurface = KaedeColors.sidebar;

/// Hover/press fill for settings rows: one step above the surface, flat.
const kSettingsRowHover = KaedeColors.hover;

/// Hairline used between list rows.
const kSettingsDividerColor = KaedeColors.border;

/// Uppercase section header, the way Discord labels each settings group.
class SettingsSectionHeader extends StatelessWidget {
  const SettingsSectionHeader(this.title,
      {super.key, this.subheading, this.top = 26});

  final String title;
  final String? subheading;
  final double top;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(4, top, 4, 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              title.toUpperCase(),
              style: const TextStyle(
                color: KaedeColors.muted,
                fontSize: 11.5,
                fontWeight: FontWeight.w700,
                letterSpacing: 1.05,
              ),
            ),
            if (subheading case final note?) ...[
              const SizedBox(height: 4),
              Text(
                note,
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 12.5,
                  height: 1.4,
                ),
              ),
            ],
          ],
        ),
      );
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
          style: const TextStyle(
            color: KaedeColors.muted,
            fontSize: 12.5,
            height: 1.45,
          ),
        ),
      );
}

/// One flat settings row: leading glyph, title, optional subtitle and a
/// trailing control. Pressed rows fill flat with [kSettingsRowHover],
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
                          color: danger ? KaedeColors.danger : KaedeColors.text,
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
                          style: const TextStyle(
                            color: KaedeColors.muted,
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
          const Padding(
            padding: EdgeInsets.symmetric(horizontal: 4),
            child: SizedBox(
              height: 1,
              child: DecoratedBox(
                decoration: BoxDecoration(color: kSettingsDividerColor),
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
          color: on ? KaedeColors.text : KaedeColors.raised,
          borderRadius: BorderRadius.circular(14),
          border: on ? null : Border.all(color: KaedeColors.borderStrong),
        ),
        child: Align(
          alignment: on ? Alignment.centerRight : Alignment.centerLeft,
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 150),
            curve: Curves.easeOut,
            width: 16,
            height: 16,
            decoration: BoxDecoration(
              color: on ? KaedeColors.canvas : KaedeColors.muted,
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
                style: const TextStyle(
                  color: KaedeColors.muted,
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
                          style: const TextStyle(
                            color: KaedeColors.muted,
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
                  color: selected ? KaedeColors.coralText : KaedeColors.muted,
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
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(
              child: Text(
                display,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 13.5,
                ),
              ),
            ),
            const SizedBox(width: 2),
            const Icon(Icons.chevron_right_rounded,
                size: 18, color: KaedeColors.muted),
          ],
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
            foregroundColor: KaedeColors.danger,
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
            style: const TextStyle(
              color: KaedeColors.textSoft,
              fontSize: 12.5,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          TextField(
            controller: controller,
            decoration: InputDecoration(
              hintText: hint,
              hintStyle: const TextStyle(color: KaedeColors.muted),
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
        color: KaedeColors.canvas.withValues(alpha: .78),
        borderRadius: BorderRadius.circular(size / 2),
        child: InkWell(
          onTap: onPressed,
          customBorder: const CircleBorder(),
          child: Center(
            child: Padding(
              padding: EdgeInsets.all(size * .24),
              child: Tooltip(
                message: tooltip,
                child: Icon(icon, size: size * .5, color: KaedeColors.text),
              ),
            ),
          ),
        ),
      );
}
