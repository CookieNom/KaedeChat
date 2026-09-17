import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/core/debug_log.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

class DebugSettings extends StatefulWidget {
  const DebugSettings({super.key});

  @override
  State<DebugSettings> createState() => _DebugSettingsState();
}

class _DebugSettingsState extends State<DebugSettings> {
  bool _busy = false;
  String? _message;

  Future<void> _run(Future<void> Function() action, String success) async {
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      await action();
      if (mounted) setState(() => _message = success);
    } on Object {
      if (mounted) {
        setState(() => _message = L10n.of(context).debug_action_failed);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final log = DebugLog.instance;
    final l10n = L10n.of(context);
    return ListenableBuilder(
      listenable: log,
      builder: (context, _) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SettingsSectionHeader(l10n.debug_section),
          SettingsSwitchRow(
            title: l10n.debug_logging,
            subtitle: l10n.debug_logging_description,
            value: log.enabled,
            onChanged: (value) {
              if (!_busy) _run(() => log.setEnabled(value), '');
            },
          ),
          if (log.enabled) ...[
            SettingsInfo(l10n.debug_logging_instructions),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton.icon(
                  icon: const Icon(Icons.copy),
                  label: Text(l10n.debug_copy),
                  onPressed: _busy
                      ? null
                      : () => _run(() async {
                            final report = await log.export();
                            await Clipboard.setData(
                                ClipboardData(text: report));
                          }, l10n.push_diagnostics_copied),
                ),
                OutlinedButton.icon(
                  icon: const Icon(Icons.delete_outline),
                  label: Text(l10n.debug_clear),
                  onPressed:
                      _busy ? null : () => _run(log.clear, l10n.debug_cleared),
                ),
              ],
            ),
          ],
          if (_message case final message? when message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Semantics(liveRegion: true, child: Text(message)),
            ),
        ],
      ),
    );
  }
}
