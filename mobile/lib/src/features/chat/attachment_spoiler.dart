import 'dart:io';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

bool isAttachmentSpoiler(String filename) => filename.startsWith('SPOILER_');

String spoilerFilename(String filename, bool spoiler) {
  final stripped = filename.replaceFirst(RegExp(r'^(SPOILER_)+'), '');
  final name = stripped.isEmpty ? 'file' : stripped;
  if (!spoiler) return name;
  final dot = name.lastIndexOf('.');
  final extension = dot < 0 ? '' : name.substring(dot);
  final suffix = extension.length <= 16 ? extension : '';
  return 'SPOILER_${name.length > 247 ? name.substring(0, 247 - suffix.length) + suffix : name}';
}

Future<bool?> showAttachmentSpoilerEditor(
  BuildContext context, {
  required File file,
  required String filename,
  required String contentType,
}) {
  var spoiler = isAttachmentSpoiler(filename);
  return showModalBottomSheet<bool>(
    context: context,
    useSafeArea: true,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (context) => StatefulBuilder(
      builder: (context, setState) => Padding(
        padding: const EdgeInsets.all(20),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          if (contentType.startsWith('image/'))
            ConstrainedBox(
              constraints: BoxConstraints(
                  maxHeight: MediaQuery.sizeOf(context).height * .45),
              child: Image.file(file,
                  fit: BoxFit.contain,
                  errorBuilder: (_, __, ___) =>
                      const Icon(Icons.broken_image_outlined)),
            ),
          Text(filename, maxLines: 2, overflow: TextOverflow.ellipsis),
          SwitchListTile(
            title: Text(L10n.of(context).ui_mark_as_spoiler_ed256e32),
            subtitle: Text(L10n.of(context)
                .ui_others_must_tap_to_reveal_this_attachment_45975bcd),
            value: spoiler,
            onChanged: (value) => setState(() => spoiler = value),
          ),
          FilledButton(
              onPressed: () => Navigator.pop(context, spoiler),
              child: Text(L10n.of(context).ui_done_8dd31791)),
        ]),
      ),
    ),
  );
}

/// The media widget is built only after an explicit reveal, preventing both
/// accessibility leaks and hidden autoplay/downloads.
class AttachmentSpoiler extends StatefulWidget {
  const AttachmentSpoiler(
      {super.key,
      required this.filename,
      required this.identity,
      required this.builder,
      this.compact = false});
  final String filename;
  final String identity;
  final WidgetBuilder builder;
  final bool compact;

  @override
  State<AttachmentSpoiler> createState() => _AttachmentSpoilerState();
}

class _AttachmentSpoilerState extends State<AttachmentSpoiler> {
  String? _revealed;

  @override
  Widget build(BuildContext context) {
    final token = '${widget.identity}:${widget.filename}';
    if (!isAttachmentSpoiler(widget.filename) || _revealed == token) {
      return widget.builder(context);
    }
    return SizedBox(
      width: widget.compact ? null : 240,
      height: widget.compact ? null : 120,
      child: FilledButton.tonal(
        onPressed: () => setState(() => _revealed = token),
        child: Semantics(
            label: L10n.of(context).ui_reveal_spoiler_attachment_965a0ac3,
            child: ExcludeSemantics(
                child: Text(L10n.of(context).ui_spoiler_ea48d139))),
      ),
    );
  }
}
