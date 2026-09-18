import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

class PushDiagnosticsButton extends StatefulWidget {
  const PushDiagnosticsButton({super.key, required this.diagnostics});

  final String diagnostics;

  @override
  State<PushDiagnosticsButton> createState() => _PushDiagnosticsButtonState();
}

class _PushDiagnosticsButtonState extends State<PushDiagnosticsButton> {
  String? _copied;
  bool _failed = false;

  @override
  Widget build(BuildContext context) => ActionButton(
        kind: ActionButtonKind.outlined,
        icon: Icon(_copied == widget.diagnostics ? Icons.check : Icons.copy),
        label: Text(_failed
            ? L10n.of(context).push_diagnostics_copy_failed
            : _copied == widget.diagnostics
                ? L10n.of(context).push_diagnostics_copied
                : L10n.of(context).push_diagnostics_copy),
        onPressed: () async {
          final diagnostics = widget.diagnostics;
          try {
            await Clipboard.setData(ClipboardData(text: diagnostics));
            if (mounted) {
              setState(() {
                _copied = diagnostics;
                _failed = false;
              });
            }
          } on Object {
            if (mounted) setState(() => _failed = true);
          }
        },
      );
}
