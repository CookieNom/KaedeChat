import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

Future<void> copyDeveloperId(
  BuildContext context, {
  required String value,
  required String label,
}) async {
  await Clipboard.setData(ClipboardData(text: value));
  if (!context.mounted) return;
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
        content: Text(
            L10n.of(context).ui_value0_id_copied_c4b8f437((label).toString()))),
  );
}

ListTile developerIdAction({
  required BuildContext context,
  required String value,
  required String label,
  VoidCallback? closeBeforeCopy,
}) =>
    ListTile(
      leading: const Icon(Icons.badge_outlined),
      title:
          Text(L10n.of(context).ui_copy_value0_id_b3fee256((label).toString())),
      onTap: () {
        closeBeforeCopy?.call();
        copyDeveloperId(context, value: value, label: label);
      },
    );
