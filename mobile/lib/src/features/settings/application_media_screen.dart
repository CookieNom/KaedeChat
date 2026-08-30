import 'dart:async';
import 'dart:io';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/application_media_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_media.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

/// Native entry point for the subset of the Developer Portal concerned with
/// application-owned images and emoji.
final class ApplicationMediaScreen extends ConsumerStatefulWidget {
  const ApplicationMediaScreen({super.key, this.repository});

  /// Test and embed hook. Normal navigation uses the signed-in repository.
  final KaedeRepository? repository;

  @override
  ConsumerState<ApplicationMediaScreen> createState() =>
      _ApplicationMediaScreenState();
}

final class _ApplicationMediaScreenState
    extends ConsumerState<ApplicationMediaScreen> {
  List<DeveloperApplication> _applications = const [];
  var _loading = true;
  String? _error;

  KaedeRepository get _repository =>
      widget.repository ??
      ref.read(mobileControllerProvider.notifier).repository;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final applications = await _repository.developerApplications();
      if (!mounted) return;
      setState(() {
        _applications = applications;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(
          error,
          summary: 'Could not load your developer applications',
        );
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: settingsSurface(context),
        appBar: AppBar(
          backgroundColor: settingsSurface(context),
          title: Text('Application media'),
        ),
        body: SafeArea(
          child: RefreshIndicator(
            onRefresh: _load,
            child: ListView(
              key: Key('application-media-app-list'),
              physics: AlwaysScrollableScrollPhysics(),
              padding: EdgeInsets.fromLTRB(16, 0, 16, 36),
              children: [
                SettingsSectionHeader(
                  'Developer applications',
                  subheading:
                      'Choose an application you own personally or through a developer team.',
                ),
                if (_loading)
                  const _MediaState(
                    icon: Icons.hourglass_top_rounded,
                    title: 'Loading applications…',
                    progress: true,
                  )
                else if (_error case final message?)
                  _MediaState(
                    icon: Icons.cloud_off_rounded,
                    title: 'Applications are unavailable',
                    detail: message,
                    actionLabel: 'Try again',
                    onAction: _load,
                  )
                else if (_applications.isEmpty)
                  const _MediaState(
                    icon: Icons.developer_board_outlined,
                    title: 'No developer applications',
                    detail:
                        'Create an application in the Web or Desktop Developer Portal, or ask a team owner to add you. It will then appear here.',
                  )
                else
                  for (var index = 0; index < _applications.length; index += 1)
                    SettingsRow.chevron(
                      title: _applications[index].name,
                      subtitle: [
                        if (_applications[index]
                                .description
                                ?.trim()
                                .isNotEmpty ==
                            true)
                          _applications[index].description!.trim(),
                        _applications[index].status,
                      ].join(' · '),
                      leading: _ApplicationIcon(
                        application: _applications[index],
                      ),
                      divider: index != _applications.length - 1,
                      onTap: () => Navigator.of(context).push(
                        MaterialPageRoute<void>(
                          builder: (_) => ApplicationMediaManagerScreen(
                            application: _applications[index],
                            repository: _repository,
                          ),
                        ),
                      ),
                    ),
                SettingsInfo(
                  'Application media is public wherever the application is used. Uploaded images are safety-scanned before they can be published.',
                ),
              ],
            ),
          ),
        ),
      );
}

final class ApplicationMediaManagerScreen extends StatefulWidget {
  const ApplicationMediaManagerScreen({
    required this.application,
    required this.repository,
    super.key,
  });

  final DeveloperApplication application;
  final KaedeRepository repository;

  @override
  State<ApplicationMediaManagerScreen> createState() =>
      _ApplicationMediaManagerScreenState();
}

final class _ApplicationMediaManagerScreenState
    extends State<ApplicationMediaManagerScreen> {
  List<ApplicationAsset> _assets = const [];
  List<ApplicationEmoji> _emojis = const [];
  var _loading = true;
  var _permissionDenied = false;
  String? _error;
  String? _notice;
  String? _busy;
  int _uploadPercent = 0;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _permissionDenied = false;
      _error = null;
    });
    try {
      final results = await Future.wait<Object>([
        widget.repository.applicationAssets(widget.application.ref),
        widget.repository.applicationEmojis(widget.application.ref),
      ]);
      if (!mounted) return;
      setState(() {
        _assets = results[0] as List<ApplicationAsset>;
        _emojis = results[1] as List<ApplicationEmoji>;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _permissionDenied = error is KaedeException && error.status == 403;
        _error = _permissionDenied
            ? 'You can view this team application, but only team owners, administrators, and developers can manage its media.'
            : userFacingError(
                error,
                summary: 'Could not load application media',
              );
        _loading = false;
      });
    }
  }

  void _progress(int sent, int total) {
    if (!mounted || total < 1) return;
    setState(() => _uploadPercent = (sent * 100 / total).round().clamp(0, 100));
  }

  Future<_PickedApplicationImage?> _pickImage() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: const ['png', 'jpg', 'jpeg', 'gif', 'webp'],
      allowMultiple: false,
      withData: false,
    );
    final selected = result?.files.singleOrNull;
    if (selected == null || !mounted) return null;
    final contentType = imageUploadContentType(selected.name);
    final validation = applicationImageValidation(
      filename: selected.name,
      contentType: contentType,
      size: selected.size,
    );
    if (selected.path == null || validation != null) {
      _showError(
        selected.path == null
            ? 'Choose an image stored on this device.'
            : validation!,
      );
      return null;
    }
    return _PickedApplicationImage(
      file: File(selected.path!),
      filename: selected.name,
      contentType: contentType!,
    );
  }

  Future<void> _createAsset() async {
    if (_busy != null) return;
    final image = await _pickImage();
    if (image == null || !mounted) return;
    final draft = await showApplicationAssetEditor(
      context,
      title: 'Add application asset',
      action: 'Upload asset',
      initial: ApplicationAssetDraft(
        name: _basename(image.filename),
        kind: ApplicationAssetKind.other,
      ),
    );
    if (draft == null || !mounted) return;
    setState(() {
      _busy = 'asset-create';
      _uploadPercent = 0;
      _error = null;
      _notice = null;
    });
    try {
      final asset = await widget.repository.uploadApplicationAsset(
        application: widget.application.ref,
        draft: draft,
        filename: image.filename,
        contentType: image.contentType,
        file: image.file,
        onProgress: _progress,
      );
      if (!mounted) return;
      setState(() {
        _assets = [..._assets.where((item) => item.id != asset.id), asset]
          ..sort(_compareAssets);
        _notice = '${asset.name} is ready.';
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary: 'Could not create the application asset',
      ));
    } finally {
      if (mounted) {
        setState(() {
          _busy = null;
          _uploadPercent = 0;
        });
      }
    }
  }

  Future<void> _createEmoji() async {
    if (_busy != null) return;
    final image = await _pickImage();
    if (image == null || !mounted) return;
    final draft = await showApplicationEmojiEditor(
      context,
      title: 'Add application emoji',
      action: 'Upload emoji',
      initial: ApplicationEmojiDraft(name: _basename(image.filename)),
    );
    if (draft == null || !mounted) return;
    setState(() {
      _busy = 'emoji-create';
      _uploadPercent = 0;
      _error = null;
      _notice = null;
    });
    try {
      final emoji = await widget.repository.uploadApplicationEmoji(
        application: widget.application.ref,
        draft: draft,
        filename: image.filename,
        contentType: image.contentType,
        file: image.file,
        onProgress: _progress,
      );
      if (!mounted) return;
      setState(() {
        _emojis = [..._emojis.where((item) => item.id != emoji.id), emoji]
          ..sort((left, right) => left.name.compareTo(right.name));
        _notice = ':${emoji.name}: is ready.';
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary: 'Could not create the application emoji',
      ));
    } finally {
      if (mounted) {
        setState(() {
          _busy = null;
          _uploadPercent = 0;
        });
      }
    }
  }

  Future<void> _editAsset(ApplicationAsset asset) async {
    if (_busy != null) return;
    final draft = await showApplicationAssetEditor(
      context,
      title: 'Edit asset',
      action: 'Save changes',
      initial: ApplicationAssetDraft(name: asset.name, kind: asset.kind),
    );
    if (draft == null || !mounted) return;
    setState(() {
      _busy = 'asset-${asset.id.value}';
      _error = null;
    });
    try {
      final updated = await widget.repository.updateApplicationAsset(
        widget.application.ref,
        asset.id,
        draft,
      );
      if (!mounted) return;
      setState(() {
        _assets = _assets
            .map((item) => item.id == updated.id ? updated : item)
            .toList()
          ..sort(_compareAssets);
        _notice = '${updated.name} was updated.';
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary: 'Could not update the application asset',
      ));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _editEmoji(ApplicationEmoji emoji) async {
    if (_busy != null) return;
    final draft = await showApplicationEmojiEditor(
      context,
      title: 'Edit emoji',
      action: 'Save changes',
      initial: ApplicationEmojiDraft(name: emoji.name),
    );
    if (draft == null || !mounted) return;
    await _updateEmoji(emoji, draft);
  }

  Future<void> _updateEmoji(
    ApplicationEmoji emoji,
    ApplicationEmojiDraft draft,
  ) async {
    if (_busy != null) return;
    setState(() {
      _busy = 'emoji-${emoji.id.value}';
      _error = null;
    });
    try {
      final updated = await widget.repository.updateApplicationEmoji(
        widget.application.ref,
        emoji.id,
        draft,
      );
      if (!mounted) return;
      setState(() {
        _emojis = _emojis
            .map((item) => item.id == updated.id ? updated : item)
            .toList()
          ..sort((left, right) => left.name.compareTo(right.name));
        _notice = ':${updated.name}: was updated.';
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary: 'Could not update the application emoji',
      ));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _deleteAsset(ApplicationAsset asset) async {
    if (_busy != null ||
        !await _confirmDelete(
          title: 'Delete ${asset.name}?',
          detail:
              'Existing references to this application asset will stop resolving.',
        )) {
      return;
    }
    setState(() => _busy = 'asset-${asset.id.value}');
    try {
      await widget.repository.deleteApplicationAsset(
        widget.application.ref,
        asset.id,
      );
      if (!mounted) return;
      setState(() {
        _assets = _assets.where((item) => item.id != asset.id).toList();
        _notice = '${asset.name} was deleted.';
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary: 'Could not delete the application asset',
      ));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _deleteEmoji(ApplicationEmoji emoji) async {
    if (_busy != null ||
        !await _confirmDelete(
          title: 'Delete :${emoji.name}:?',
          detail: 'Existing uses of this emoji may stop rendering.',
        )) {
      return;
    }
    setState(() => _busy = 'emoji-${emoji.id.value}');
    try {
      await widget.repository.deleteApplicationEmoji(
        widget.application.ref,
        emoji.id,
      );
      if (!mounted) return;
      setState(() {
        _emojis = _emojis.where((item) => item.id != emoji.id).toList();
        _notice = ':${emoji.name}: was deleted.';
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary: 'Could not delete the application emoji',
      ));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<bool> _confirmDelete({
    required String title,
    required String detail,
  }) async =>
      await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: Text(title),
          content: Text(detail),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: Text('Cancel'),
            ),
            FilledButton(
              key: Key('confirm-application-media-delete'),
              style:
                  FilledButton.styleFrom(backgroundColor: context.kaede.danger),
              onPressed: () => Navigator.pop(dialogContext, true),
              child: Text('Delete'),
            ),
          ],
        ),
      ) ??
      false;

  void _showError(String message) {
    if (!mounted) return;
    setState(() {
      _error = message;
      _notice = null;
    });
  }

  @override
  Widget build(BuildContext context) => DefaultTabController(
        length: 2,
        child: Scaffold(
          backgroundColor: settingsSurface(context),
          appBar: AppBar(
            backgroundColor: settingsSurface(context),
            title: Text(widget.application.name),
            bottom: TabBar(
              tabs: [
                Tab(text: 'Assets', icon: Icon(Icons.image_outlined)),
                Tab(text: 'Emoji', icon: Icon(Icons.emoji_emotions_outlined)),
              ],
            ),
          ),
          body: SafeArea(child: _body()),
        ),
      );

  Widget _body() {
    if (_loading) {
      return const _MediaState(
        icon: Icons.hourglass_top_rounded,
        title: 'Loading application media…',
        progress: true,
      );
    }
    if (_permissionDenied) {
      return _MediaState(
        icon: Icons.lock_outline_rounded,
        title: 'Media management is restricted',
        detail: _error,
      );
    }
    if (_error != null && _assets.isEmpty && _emojis.isEmpty) {
      return _MediaState(
        icon: Icons.cloud_off_rounded,
        title: 'Application media is unavailable',
        detail: _error,
        actionLabel: 'Try again',
        onAction: _load,
      );
    }
    return Column(
      children: [
        if (_error case final message?)
          _InlineNotice(message: message, error: true),
        if (_notice case final message?) _InlineNotice(message: message),
        if (_busy?.endsWith('create') == true)
          LinearProgressIndicator(
            key: Key('application-media-upload-progress'),
            value: _uploadPercent == 0 ? null : _uploadPercent / 100,
          ),
        Expanded(
          child: TabBarView(
            children: [
              _assetList(),
              _emojiList(),
            ],
          ),
        ),
      ],
    );
  }

  Widget _assetList() => ListView(
        key: Key('application-assets-list'),
        padding: EdgeInsets.fromLTRB(16, 8, 16, 36),
        children: [
          SettingsRow(
            title: 'Add asset',
            subtitle:
                'Icons, covers, store art, achievements, or activity art.',
            leading: Icon(Icons.add_photo_alternate_outlined),
            onTap: _busy == null ? _createAsset : null,
          ),
          if (_assets.isEmpty)
            const _MediaState(
              icon: Icons.image_not_supported_outlined,
              title: 'No application assets yet',
              detail: 'Add an image to publish it with this application.',
              compact: true,
            )
          else
            for (final asset in _assets)
              _ApplicationMediaTile(
                key: Key('application-asset-${asset.id.value}'),
                image:
                    _mediaImage(asset.mediaHash, asset.applicationRef.domain),
                title: asset.name,
                subtitle:
                    '${asset.kind.label} · ${asset.dimensions} · v${asset.version}',
                busy: _busy == 'asset-${asset.id.value}',
                onEdit: () => _editAsset(asset),
                onDelete: () => _deleteAsset(asset),
              ),
        ],
      );

  Widget _emojiList() => ListView(
        key: Key('application-emojis-list'),
        padding: EdgeInsets.fromLTRB(16, 8, 16, 36),
        children: [
          SettingsRow(
            title: 'Add emoji',
            subtitle: 'PNG, JPEG, GIF, or WebP. Names follow :emoji_name:.',
            leading: Icon(Icons.add_reaction_outlined),
            onTap: _busy == null ? _createEmoji : null,
          ),
          if (_emojis.isEmpty)
            const _MediaState(
              icon: Icons.emoji_emotions_outlined,
              title: 'No application emoji yet',
              detail: 'Add an emoji for messages and interaction responses.',
              compact: true,
            )
          else
            for (final emoji in _emojis)
              _ApplicationMediaTile(
                key: Key('application-emoji-${emoji.id.value}'),
                image:
                    _mediaImage(emoji.mediaHash, emoji.applicationRef.domain),
                title: ':${emoji.name}:',
                subtitle:
                    '${emoji.animated ? 'Animated' : 'Static'} · v${emoji.version}',
                busy: _busy == 'emoji-${emoji.id.value}',
                availability: Text(
                  key: Key('application-emoji-availability-${emoji.id.value}'),
                  emoji.available ? 'Available' : 'Unavailable',
                  style: Theme.of(context).textTheme.labelMedium?.copyWith(
                        color: emoji.available
                            ? context.kaede.mint
                            : context.kaede.muted,
                      ),
                ),
                onEdit: () => _editEmoji(emoji),
                onDelete: () => _deleteEmoji(emoji),
              ),
        ],
      );

  Widget _mediaImage(String hash, Domain domain) {
    final uri = publicAssetUri(domain, hash, variant: 'thumbnail_128');
    if (uri == null) {
      return Icon(Icons.broken_image_outlined, color: context.kaede.muted);
    }
    return CachedNetworkImage(
      imageUrl: '$uri',
      fit: BoxFit.cover,
      placeholder: (_, __) => Center(
        child: SizedBox.square(
          dimension: 18,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      ),
      errorWidget: (_, __, ___) =>
          Icon(Icons.broken_image_outlined, color: context.kaede.muted),
    );
  }
}

Future<ApplicationAssetDraft?> showApplicationAssetEditor(
  BuildContext context, {
  required String title,
  required String action,
  required ApplicationAssetDraft initial,
}) =>
    showDialog<ApplicationAssetDraft>(
      context: context,
      builder: (_) => _ApplicationAssetEditorDialog(
        title: title,
        action: action,
        initial: initial,
      ),
    );

Future<ApplicationEmojiDraft?> showApplicationEmojiEditor(
  BuildContext context, {
  required String title,
  required String action,
  required ApplicationEmojiDraft initial,
}) =>
    showDialog<ApplicationEmojiDraft>(
      context: context,
      builder: (_) => _ApplicationEmojiEditorDialog(
        title: title,
        action: action,
        initial: initial,
      ),
    );

final class _ApplicationAssetEditorDialog extends StatefulWidget {
  const _ApplicationAssetEditorDialog({
    required this.title,
    required this.action,
    required this.initial,
  });

  final String title;
  final String action;
  final ApplicationAssetDraft initial;

  @override
  State<_ApplicationAssetEditorDialog> createState() =>
      _ApplicationAssetEditorDialogState();
}

final class _ApplicationAssetEditorDialogState
    extends State<_ApplicationAssetEditorDialog> {
  late final TextEditingController _name;
  late ApplicationAssetKind _kind;
  String? _validation;

  @override
  void initState() {
    super.initState();
    _name = TextEditingController(text: widget.initial.name);
    _kind = widget.initial.kind;
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  void _save() {
    final draft = ApplicationAssetDraft(name: _name.text, kind: _kind);
    final message = draft.validationMessage;
    if (message != null) {
      setState(() => _validation = message);
      return;
    }
    Navigator.pop(context, draft);
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(widget.title),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                key: Key('application-asset-name-field'),
                controller: _name,
                autofocus: true,
                maxLength: 100,
                decoration: InputDecoration(
                  labelText: 'Asset name',
                  errorText: _validation,
                ),
              ),
              DropdownButtonFormField<ApplicationAssetKind>(
                key: Key('application-asset-kind-field'),
                initialValue: _kind,
                decoration: InputDecoration(labelText: 'Asset kind'),
                items: [
                  for (final option in ApplicationAssetKind.values)
                    DropdownMenuItem(value: option, child: Text(option.label)),
                ],
                onChanged: (value) {
                  if (value != null) setState(() => _kind = value);
                },
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('Cancel'),
          ),
          FilledButton(
            key: Key('save-application-asset'),
            onPressed: _save,
            child: Text(widget.action),
          ),
        ],
      );
}

final class _ApplicationEmojiEditorDialog extends StatefulWidget {
  const _ApplicationEmojiEditorDialog({
    required this.title,
    required this.action,
    required this.initial,
  });

  final String title;
  final String action;
  final ApplicationEmojiDraft initial;

  @override
  State<_ApplicationEmojiEditorDialog> createState() =>
      _ApplicationEmojiEditorDialogState();
}

final class _ApplicationEmojiEditorDialogState
    extends State<_ApplicationEmojiEditorDialog> {
  late final TextEditingController _name;
  String? _validation;

  @override
  void initState() {
    super.initState();
    _name = TextEditingController(text: widget.initial.name);
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  void _save() {
    final draft = ApplicationEmojiDraft(name: _name.text);
    final message = draft.validationMessage;
    if (message != null) {
      setState(() => _validation = message);
      return;
    }
    Navigator.pop(context, draft);
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(widget.title),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                key: Key('application-emoji-name-field'),
                controller: _name,
                autofocus: true,
                maxLength: 32,
                decoration: InputDecoration(
                  labelText: 'Emoji name',
                  helperText: 'Letters, numbers, and underscores',
                  errorText: _validation,
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('Cancel'),
          ),
          FilledButton(
            key: Key('save-application-emoji'),
            onPressed: _save,
            child: Text(widget.action),
          ),
        ],
      );
}

final class _ApplicationIcon extends StatelessWidget {
  const _ApplicationIcon({required this.application});

  final DeveloperApplication application;

  @override
  Widget build(BuildContext context) {
    final uri = publicAssetUri(
      application.ref.domain,
      application.iconHash,
      variant: 'thumbnail_128',
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(9),
      child: SizedBox.square(
        dimension: 38,
        child: uri == null
            ? ColoredBox(
                color: context.kaede.raised,
                child: Icon(Icons.smart_toy_outlined, size: 21),
              )
            : CachedNetworkImage(
                imageUrl: '$uri',
                fit: BoxFit.cover,
                errorWidget: (_, __, ___) => ColoredBox(
                  color: context.kaede.raised,
                  child: Icon(Icons.smart_toy_outlined, size: 21),
                ),
              ),
      ),
    );
  }
}

final class _ApplicationMediaTile extends StatelessWidget {
  const _ApplicationMediaTile({
    required this.image,
    required this.title,
    required this.subtitle,
    required this.busy,
    required this.onEdit,
    required this.onDelete,
    this.availability,
    super.key,
  });

  final Widget image;
  final String title;
  final String subtitle;
  final bool busy;
  final VoidCallback onEdit;
  final VoidCallback onDelete;
  final Widget? availability;

  @override
  Widget build(BuildContext context) => Container(
        margin: EdgeInsets.only(top: 8),
        padding: EdgeInsets.all(10),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(9),
              child: SizedBox.square(dimension: 58, child: image),
            ),
            SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  SizedBox(height: 3),
                  Text(
                    subtitle,
                    style: TextStyle(
                      color: context.kaede.muted,
                      fontSize: 12,
                    ),
                  ),
                ],
              ),
            ),
            if (busy)
              Padding(
                padding: EdgeInsets.all(10),
                child: SizedBox.square(
                  dimension: 20,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              )
            else ...[
              if (availability case final control?) control,
              IconButton(
                tooltip: 'Edit',
                onPressed: onEdit,
                icon: Icon(Icons.edit_outlined),
              ),
              IconButton(
                tooltip: 'Delete',
                color: context.kaede.danger,
                onPressed: onDelete,
                icon: Icon(Icons.delete_outline_rounded),
              ),
            ],
          ],
        ),
      );
}

final class _InlineNotice extends StatelessWidget {
  const _InlineNotice({required this.message, this.error = false});

  final String message;
  final bool error;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        margin: EdgeInsets.fromLTRB(16, 10, 16, 0),
        padding: EdgeInsets.all(10),
        decoration: BoxDecoration(
          color: (error ? context.kaede.danger : context.kaede.mint)
              .withValues(alpha: .12),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Text(
          message,
          style: TextStyle(
            color: error ? context.kaede.danger : context.kaede.mint,
            fontWeight: FontWeight.w600,
          ),
        ),
      );
}

final class _MediaState extends StatelessWidget {
  const _MediaState({
    required this.icon,
    required this.title,
    this.detail,
    this.actionLabel,
    this.onAction,
    this.progress = false,
    this.compact = false,
  });

  final IconData icon;
  final String title;
  final String? detail;
  final String? actionLabel;
  final FutureOr<void> Function()? onAction;
  final bool progress;
  final bool compact;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: EdgeInsets.symmetric(
            horizontal: 24,
            vertical: compact ? 32 : 72,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (progress)
                CircularProgressIndicator()
              else
                Icon(icon, size: 42, color: context.kaede.muted),
              SizedBox(height: 12),
              Text(
                title,
                textAlign: TextAlign.center,
                style: TextStyle(fontWeight: FontWeight.w800),
              ),
              if (detail case final text?) ...[
                SizedBox(height: 6),
                Text(
                  text,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: context.kaede.muted, height: 1.35),
                ),
              ],
              if (actionLabel case final label?) ...[
                SizedBox(height: 14),
                FilledButton(onPressed: onAction, child: Text(label)),
              ],
            ],
          ),
        ),
      );
}

final class _PickedApplicationImage {
  const _PickedApplicationImage({
    required this.file,
    required this.filename,
    required this.contentType,
  });

  final File file;
  final String filename;
  final String contentType;
}

String _basename(String filename) {
  final cleaned = filename.replaceFirst(RegExp(r'\.[^.]+$'), '').trim();
  return cleaned.substring(0, cleaned.length.clamp(0, 100));
}

int _compareAssets(ApplicationAsset left, ApplicationAsset right) {
  final kind = left.kind.index.compareTo(right.kind.index);
  return kind != 0 ? kind : left.name.compareTo(right.name);
}
