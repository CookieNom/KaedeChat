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
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
          summary: L10n.of(context)
              .ui_could_not_load_your_developer_applications_adbee2a5,
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
          title: Text(L10n.of(context).ui_application_media_4fb1e2c1),
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
                  L10n.of(context).ui_developer_applications_60f5fe14,
                  subheading: L10n.of(context)
                      .ui_choose_an_application_you_own_personally_or_t_639131ab,
                ),
                if (_loading)
                  _MediaState(
                    icon: Icons.hourglass_top_rounded,
                    title: L10n.of(context).ui_loading_applications_d3fed322,
                    progress: true,
                  )
                else if (_error case final message?)
                  _MediaState(
                    icon: Icons.cloud_off_rounded,
                    title: L10n.of(context)
                        .ui_applications_are_unavailable_201549ba,
                    detail: message,
                    actionLabel: 'Try again',
                    onAction: _load,
                  )
                else if (_applications.isEmpty)
                  _MediaState(
                    icon: Icons.developer_board_outlined,
                    title:
                        L10n.of(context).ui_no_developer_applications_d4b57be7,
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
            ? L10n.of(context)
                .ui_you_can_view_this_team_application_but_only_t_0e075835
            : userFacingError(
                error,
                summary: L10n.of(context)
                    .ui_could_not_load_application_media_beaf697f,
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
            ? L10n.of(context).ui_choose_an_image_stored_on_this_device_8ae19cfa
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
      title: L10n.of(context).ui_add_application_asset_8aca62f2,
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
        showActionFeedback(
            context,
            L10n.of(context)
                .ui_value0_is_ready_de500e77((asset.name).toString()));
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary:
            L10n.of(context).ui_could_not_create_the_application_asset_520a0b1c,
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
      title: L10n.of(context).ui_add_application_emoji_9b934d70,
      action: 'Upload emoji',
      initial: ApplicationEmojiDraft(name: _basename(image.filename)),
    );
    if (draft == null || !mounted) return;
    setState(() {
      _busy = 'emoji-create';
      _uploadPercent = 0;
      _error = null;
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
        showActionFeedback(
            context,
            L10n.of(context)
                .ui_value0_is_ready_c2c993a1((emoji.name).toString()));
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary:
            L10n.of(context).ui_could_not_create_the_application_emoji_10779392,
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
      title: L10n.of(context).ui_edit_asset_5db3b1f5,
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
        showActionFeedback(
            context,
            L10n.of(context)
                .ui_value0_was_updated_b041a9a4((updated.name).toString()));
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary:
            L10n.of(context).ui_could_not_update_the_application_asset_74565491,
      ));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _editEmoji(ApplicationEmoji emoji) async {
    if (_busy != null) return;
    final draft = await showApplicationEmojiEditor(
      context,
      title: L10n.of(context).ui_edit_emoji_26aed3f7,
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
        showActionFeedback(
            context,
            L10n.of(context)
                .ui_value0_was_updated_5b2bb66a((updated.name).toString()));
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary:
            L10n.of(context).ui_could_not_update_the_application_emoji_6679e91b,
      ));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _deleteAsset(ApplicationAsset asset) async {
    if (_busy != null ||
        !await _confirmDelete(
          title: L10n.of(context)
              .ui_delete_value0_5861e774((asset.name).toString()),
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
        showActionFeedback(
            context,
            L10n.of(context)
                .ui_value0_was_deleted_328445d6((asset.name).toString()));
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary:
            L10n.current.ui_could_not_delete_the_application_asset_118f826b,
      ));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _deleteEmoji(ApplicationEmoji emoji) async {
    if (_busy != null ||
        !await _confirmDelete(
          title: L10n.of(context)
              .ui_delete_value0_4119b3f2((emoji.name).toString()),
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
        showActionFeedback(
            context,
            L10n.of(context)
                .ui_value0_was_deleted_1521827c((emoji.name).toString()));
      });
    } on Object catch (error) {
      _showError(userFacingError(
        error,
        summary:
            L10n.current.ui_could_not_delete_the_application_emoji_92b112ad,
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
            ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => Navigator.pop(dialogContext, false),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            ActionButton(
              key: Key('confirm-application-media-delete'),
              style:
                  FilledButton.styleFrom(backgroundColor: context.kaede.danger),
              onPressed: () => Navigator.pop(dialogContext, true),
              child: Text(L10n.of(context).ui_delete_5797ea6a),
            ),
          ],
        ),
      ) ??
      false;

  void _showError(String message) {
    if (!mounted) return;
    showActionFeedback(context, message, error: true);
    setState(() {
      _error = message;
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
      return _MediaState(
        icon: Icons.hourglass_top_rounded,
        title: L10n.of(context).ui_loading_application_media_28210b97,
        progress: true,
      );
    }
    if (_permissionDenied) {
      return _MediaState(
        icon: Icons.lock_outline_rounded,
        title: L10n.of(context).ui_media_management_is_restricted_29120c37,
        detail: _error,
      );
    }
    if (_error != null && _assets.isEmpty && _emojis.isEmpty) {
      return _MediaState(
        icon: Icons.cloud_off_rounded,
        title: L10n.of(context).ui_application_media_is_unavailable_0c03d789,
        detail: _error,
        actionLabel: 'Try again',
        onAction: _load,
      );
    }
    return Column(
      children: [
        if (_error case final message?)
          _InlineNotice(message: message, error: true),
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
            title: L10n.of(context).ui_add_asset_5f71a334,
            subtitle: L10n.of(context)
                .ui_icons_covers_store_art_achievements_or_activi_c983e278,
            leading: Icon(Icons.add_photo_alternate_outlined),
            onTap: _busy == null ? _createAsset : null,
          ),
          if (_assets.isEmpty)
            _MediaState(
              icon: Icons.image_not_supported_outlined,
              title: L10n.of(context).ui_no_application_assets_yet_998c9d23,
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
                subtitle: L10n.of(context).ui_value0_value1_v_value2_fb7a7d2d(
                    (asset.kind.label).toString(),
                    (asset.dimensions).toString(),
                    (asset.version).toString()),
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
            title: L10n.of(context).ui_add_emoji_a3203c4a,
            subtitle: L10n.of(context)
                .ui_png_jpeg_gif_or_webp_names_follow_emoji_name_c44cdf60,
            leading: Icon(Icons.add_reaction_outlined),
            onTap: _busy == null ? _createEmoji : null,
          ),
          if (_emojis.isEmpty)
            _MediaState(
              icon: Icons.emoji_emotions_outlined,
              title: L10n.of(context).ui_no_application_emoji_yet_c4a722cc,
              detail: 'Add an emoji for messages and interaction responses.',
              compact: true,
            )
          else
            for (final emoji in _emojis)
              _ApplicationMediaTile(
                key: Key('application-emoji-${emoji.id.value}'),
                image:
                    _mediaImage(emoji.mediaHash, emoji.applicationRef.domain),
                title: L10n.of(context)
                    .ui_value0_2397634a((emoji.name).toString()),
                subtitle: L10n.of(context).ui_value0_v_value1_7b65e035(
                    (emoji.animated ? 'Animated' : 'Static').toString(),
                    (emoji.version).toString()),
                busy: _busy == 'emoji-${emoji.id.value}',
                availability: Text(
                  key: Key('application-emoji-availability-${emoji.id.value}'),
                  emoji.available
                      ? L10n.of(context).ui_available_72402638
                      : L10n.of(context).ui_unavailable_da4baaa9,
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
                  labelText: L10n.of(context).ui_asset_name_007d64fa,
                  errorText: _validation,
                ),
              ),
              DropdownButtonFormField<ApplicationAssetKind>(
                key: Key('application-asset-kind-field'),
                initialValue: _kind,
                decoration: InputDecoration(
                    labelText: L10n.of(context).ui_asset_kind_64cad39f),
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
          ActionButton(
            kind: ActionButtonKind.text,
            onPressed: () => Navigator.pop(context),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          ActionButton(
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
                  labelText: L10n.of(context).ui_emoji_name_c9418744,
                  helperText: L10n.of(context)
                      .ui_letters_numbers_and_underscores_205abdd4,
                  errorText: _validation,
                ),
              ),
            ],
          ),
        ),
        actions: [
          ActionButton(
            kind: ActionButtonKind.text,
            onPressed: () => Navigator.pop(context),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          ActionButton(
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
              ActionButton(
                kind: ActionButtonKind.icon,
                tooltip: L10n.of(context).ui_edit_c2c76cb1,
                onPressed: onEdit,
                icon: Icon(Icons.edit_outlined),
              ),
              ActionButton(
                kind: ActionButtonKind.icon,
                tooltip: L10n.of(context).ui_delete_5797ea6a,
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
                ActionButton(onPressed: onAction, child: Text(label)),
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
