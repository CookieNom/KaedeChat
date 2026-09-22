import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

Future<FilePickerResult?> pickAttachments(
  BuildContext context, {
  FileType type = FileType.any,
  List<String>? allowedExtensions,
  bool allowMultiple = false,
  bool withData = false,
  bool withReadStream = false,
}) async {
  // On iOS, the unrestricted document picker cannot browse the Photos library.
  if (!kIsWeb &&
      defaultTargetPlatform == TargetPlatform.iOS &&
      type == FileType.any) {
    final source = await showModalBottomSheet<FileType>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        top: false,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              SettingsRow.chevron(
                leading: const Icon(Icons.photo_library_outlined),
                title: L10n.of(context).attachmentPhotosAndVideos,
                onTap: () => Navigator.pop(context, FileType.media),
              ),
              SettingsRow.chevron(
                leading: const Icon(Icons.folder_outlined),
                title: L10n.of(context).attachmentFiles,
                onTap: () => Navigator.pop(context, FileType.any),
              ),
              const SizedBox(height: 4),
            ],
          ),
        ),
      ),
    );
    if (source == null || !context.mounted) return null;
    type = source;
  }
  return FilePicker.platform.pickFiles(
    type: type,
    allowedExtensions: allowedExtensions,
    allowMultiple: allowMultiple,
    withData: withData,
    withReadStream: withReadStream,
  );
}
