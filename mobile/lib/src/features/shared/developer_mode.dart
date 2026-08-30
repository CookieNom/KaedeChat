import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

Future<void> copyDeveloperId(
  BuildContext context, {
  required String value,
  required String label,
}) async {
  await Clipboard.setData(ClipboardData(text: value));
  if (!context.mounted) return;
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(content: Text('$label ID copied.')),
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
      title: Text('Copy $label ID'),
      onTap: () {
        closeBeforeCopy?.call();
        copyDeveloperId(context, value: value, label: label);
      },
    );
