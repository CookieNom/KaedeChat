import 'dart:async';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/tracker_media_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:video_player/video_player.dart';

bool trackerSafeUrl(String value) {
  if (value.length > 2048 || RegExp(r'\s').hasMatch(value)) return false;
  final uri = Uri.tryParse(value);
  return uri != null &&
      (uri.scheme == 'https' || uri.scheme == 'http') &&
      uri.host.isNotEmpty &&
      uri.userInfo.isEmpty;
}

/// Native controls shared by the task editor and its read-only details sheet.
class TrackerCustomFields extends StatefulWidget {
  const TrackerCustomFields(
      {super.key,
      required this.fields,
      required this.values,
      required this.channel,
      this.guild,
      this.repository,
      this.members = const [],
      this.channels = const [],
      this.actor,
      this.canAssignOthers = false,
      this.onChanged,
      this.onBusyChanged});
  final List<TrackerField> fields;
  final Json values;
  final EntityRef channel;
  final EntityRef? guild;
  final KaedeRepository? repository;
  final List<GuildMember> members;
  final List<KaedeChannel> channels;
  final KaedeUser? actor;
  final bool canAssignOthers;
  final ValueChanged<Json>? onChanged;
  final ValueChanged<bool>? onBusyChanged;

  @override
  State<TrackerCustomFields> createState() => _TrackerCustomFieldsState();
}

class _TrackerCustomFieldsState extends State<TrackerCustomFields> {
  final _resolvedNames = <String, String>{};
  List<TrackerField> get fields => widget.fields;
  late Json _draftValues = {...widget.values};
  Json get values => _draftValues;
  @override
  void didUpdateWidget(covariant TrackerCustomFields oldWidget) {
    super.didUpdateWidget(oldWidget);
    _draftValues = {...widget.values};
  }

  EntityRef get channel => widget.channel;
  EntityRef? get guild => widget.guild;
  KaedeRepository? get repository => widget.repository;
  List<GuildMember> get members => widget.members;
  List<KaedeChannel> get channels => widget.channels;
  KaedeUser? get actor => widget.actor;
  bool get canAssignOthers => widget.canAssignOthers;
  ValueChanged<Json>? get onChanged => widget.onChanged;
  ValueChanged<bool>? get onBusyChanged => widget.onBusyChanged;

  void _set(TrackerField field, Object? value) {
    final next = Map<String, Object?>.from(values);
    if (value == null || value == '' || (value is List && value.isEmpty)) {
      next.remove(field.id);
    } else {
      next[field.id] = value;
    }
    _draftValues = next;
    onChanged?.call(next);
  }

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final field in fields)
            Padding(
              key: ValueKey('custom-field-${field.id}'),
              padding: const EdgeInsets.only(top: 16),
              child: _field(context, field),
            )
        ],
      );

  Widget _field(BuildContext context, TrackerField field) {
    final value = values[field.id];
    final editable = onChanged != null;
    if (field.type == 'attachments') {
      return TrackerAttachments(
          field: field,
          values: value is List ? value : const [],
          channel: channel,
          repository: repository,
          onChanged: editable ? (v) => _set(field, v) : null,
          onBusyChanged: onBusyChanged);
    }
    if (field.type == 'users' ||
        field.type == 'channels' ||
        field.type == 'multiselect') {
      final selected =
          value is List ? value.whereType<String>().toList() : <String>[];
      final labels = <String, String>{
        if (field.type == 'users') ...{
          ..._resolvedNames,
          for (final m in members) m.user.ref.wire: m.user.name,
          if (actor != null) actor!.ref.wire: actor!.name
        },
        if (field.type == 'channels') ...{
          for (final c in channels.where((c) =>
              c.type != ChannelType.category &&
              (guild == null || c.guildRef == guild)))
            c.ref.wire: '#${c.name}'
        },
        if (field.type == 'multiselect') ...{
          for (final o in field.options) o: o
        },
      };
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(field.name, style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (selected.isEmpty) const Text('None'),
        Wrap(spacing: 6, runSpacing: 4, children: [
          for (final s in selected)
            Chip(
                label: Text(
                    labels[s] ?? (field.type == 'users' ? 'Member ($s)' : s)),
                onDeleted: editable &&
                        (field.type != 'users' ||
                            canAssignOthers ||
                            s == actor?.ref.wire)
                    ? () =>
                        _set(field, selected.where((id) => id != s).toList())
                    : null)
        ]),
        if (editable)
          Align(
              alignment: Alignment.centerLeft,
              child: ActionButton(
                  kind: ActionButtonKind.outlined,
                  icon: const Icon(Icons.edit_outlined),
                  label: Text('Choose ${field.name}'),
                  onPressed: () async {
                    final result = await showModalBottomSheet<List<String>>(
                        context: context,
                        isScrollControlled: true,
                        useSafeArea: true,
                        showDragHandle: true,
                        builder: (_) => TrackerReferencePicker(
                            title: field.name,
                            onMembersLoaded: (members) {
                              if (mounted) {
                                setState(() {
                                  for (final m in members) {
                                    _resolvedNames[m.user.ref.wire] =
                                        m.user.name;
                                  }
                                });
                              }
                            },
                            labels: labels,
                            selected: selected,
                            repository: field.type == 'users' && canAssignOthers
                                ? repository
                                : null,
                            guild: guild,
                            canChange: (id) =>
                                field.type != 'users' ||
                                canAssignOthers ||
                                id == actor?.ref.wire));
                    if (result != null) _set(field, result);
                  })),
      ]);
    }
    if (!editable) {
      final text = value == null
          ? 'Not set'
          : field.type == 'checkbox'
              ? (value == true ? 'Yes' : 'No')
              : '$value';
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(field.name, style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 6),
        if (field.type == 'url' && value is String && trackerSafeUrl(value))
          ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => openTrackerLink(context, value),
              icon: const Icon(Icons.open_in_new),
              label: Text(value, softWrap: true))
        else
          SelectableText(text),
      ]);
    }
    switch (field.type) {
      case 'checkbox':
        return CheckboxListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(field.name),
            value: value == true,
            onChanged: (v) => _set(field, v));
      case 'select':
        return DropdownButtonFormField<String>(
            key: ValueKey('${field.id}-$value'),
            isExpanded: true,
            initialValue:
                value is String && field.options.contains(value) ? value : '',
            decoration: InputDecoration(labelText: field.name),
            items: [
              const DropdownMenuItem(value: '', child: Text('None')),
              for (final option in field.options)
                DropdownMenuItem(
                    value: option,
                    child: Text(option, overflow: TextOverflow.ellipsis))
            ],
            onChanged: (v) => _set(field, v));
      case 'date':
        return ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(field.name),
            subtitle: Text(value?.toString() ?? 'No date'),
            trailing: Wrap(children: [
              if (value != null)
                ActionButton(
                    kind: ActionButtonKind.icon,
                    tooltip: 'Clear date',
                    icon: const Icon(Icons.clear),
                    onPressed: () => _set(field, null)),
              ActionButton(
                  kind: ActionButtonKind.icon,
                  tooltip: 'Choose date',
                  icon: const Icon(Icons.calendar_month),
                  onPressed: () async {
                    final date = await showDatePicker(
                        context: context,
                        initialDate:
                            DateTime.tryParse('$value') ?? DateTime.now(),
                        firstDate: DateTime(1),
                        lastDate: DateTime(9999, 12, 31));
                    if (date != null) {
                      _set(field,
                          '${date.year.toString().padLeft(4, '0')}-${date.month.toString().padLeft(2, '0')}-${date.day.toString().padLeft(2, '0')}');
                    }
                  })
            ]));
      case 'text':
      case 'textarea':
      case 'url':
      case 'number':
        return TextFormField(
            initialValue: value?.toString() ?? '',
            decoration: InputDecoration(
                labelText: field.name, alignLabelWithHint: true),
            minLines: field.type == 'textarea' ? 3 : 1,
            maxLines: field.type == 'textarea' ? 8 : 1,
            maxLength: field.type == 'textarea'
                ? 10000
                : field.type == 'text'
                    ? 500
                    : field.type == 'url'
                        ? 2048
                        : null,
            keyboardType: field.type == 'number'
                ? const TextInputType.numberWithOptions(
                    decimal: true, signed: true)
                : field.type == 'url'
                    ? TextInputType.url
                    : field.type == 'textarea'
                        ? TextInputType.multiline
                        : TextInputType.text,
            autocorrect: field.type != 'url',
            validator: (v) {
              if (v == null || v.trim().isEmpty) return null;
              if (field.type == 'url' && !trackerSafeUrl(v.trim())) {
                return 'Enter a full http:// or https:// link';
              }
              if (field.type == 'number') {
                final n = num.tryParse(v);
                if (n == null || !n.isFinite || n.abs() > 1e15) {
                  return 'Enter a number between −1,000,000,000,000,000 and 1,000,000,000,000,000';
                }
              }
              return null;
            },
            onChanged: (v) => _set(
                field,
                field.type == 'number'
                    ? (num.tryParse(v)?.isFinite == true ? num.parse(v) : v)
                    : field.type == 'url'
                        ? v.trim()
                        : v));
      default:
        return ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(field.name),
            subtitle: const Text('Update the app to edit this field.'));
    }
  }
}

class TrackerReferencePicker extends StatefulWidget {
  const TrackerReferencePicker(
      {super.key,
      required this.title,
      required this.labels,
      required this.selected,
      required this.canChange,
      this.repository,
      this.guild,
      this.single = false,
      this.onMembersLoaded});
  final String title;
  final Map<String, String> labels;
  final List<String> selected;
  final bool Function(String) canChange;
  final KaedeRepository? repository;
  final EntityRef? guild;
  final bool single;
  final ValueChanged<List<GuildMember>>? onMembersLoaded;
  @override
  State<TrackerReferencePicker> createState() => _TrackerReferencePickerState();
}

class _TrackerReferencePickerState extends State<TrackerReferencePicker> {
  late final Set<String> _selected = widget.selected.toSet();
  late final Map<String, String> _labels = {...widget.labels};
  List<String>? _results;
  Timer? _debounce;
  String _query = '';
  String? _error;
  bool _loading = false, _more = false;
  EntityRef? _after;
  int _generation = 0;
  @override
  void initState() {
    super.initState();
    if (widget.repository != null && widget.guild != null) _search();
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _generation++;
    super.dispose();
  }

  Future<void> _search({bool more = false}) async {
    final repo = widget.repository, guild = widget.guild;
    if (repo == null || guild == null) return;
    final generation = ++_generation;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final members = await repo.members(guild,
          query: _query.isEmpty ? null : _query, after: more ? _after : null);
      if (!mounted || generation != _generation) return;
      widget.onMembersLoaded?.call(members);
      setState(() {
        for (final m in members) {
          _labels[m.user.ref.wire] = m.user.name;
        }
        _results = <String>{
          if (more) ...?_results,
          ...members.map((m) => m.user.ref.wire)
        }.toList();
        _after = members.lastOrNull?.user.ref;
        _more = members.length == 100;
      });
    } on Object catch (e) {
      if (mounted && generation == _generation) {
        setState(() =>
            _error = userFacingError(e, summary: 'Could not load members'));
      }
    } finally {
      if (mounted && generation == _generation) {
        setState(() => _loading = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final ids = <String>{
      ..._selected,
      ...?_results,
      if (_results == null) ..._labels.keys
    }
        .where((id) =>
            (_labels[id] ?? id).toLowerCase().contains(_query.toLowerCase()) ||
            (_results?.contains(id) ?? false))
        .toList();
    return Padding(
        padding:
            EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
        child: SizedBox(
            height: MediaQuery.sizeOf(context).height * .75,
            child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 20),
                      child: Text(widget.title,
                          style: Theme.of(context).textTheme.titleLarge)),
                  Padding(
                      padding: const EdgeInsets.all(16),
                      child: TextField(
                          autofocus: false,
                          maxLength: 100,
                          decoration: const InputDecoration(
                              labelText: 'Search',
                              prefixIcon: Icon(Icons.search)),
                          onChanged: (v) {
                            _debounce?.cancel();
                            _generation++;
                            setState(() {
                              _query = v.trim();
                              _results = null;
                              _more = false;
                              _after = null;
                              _loading = false;
                            });
                            _debounce = Timer(
                                const Duration(milliseconds: 300), _search);
                          })),
                  if (_loading) const LinearProgressIndicator(),
                  if (_error != null)
                    Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 16),
                        child: Column(children: [
                          Text(_error!),
                          ActionButton(
                              kind: ActionButtonKind.text,
                              onPressed: _search,
                              child: const Text('Retry'))
                        ])),
                  Expanded(
                      child: ids.isEmpty && !_loading
                          ? const Center(child: Text('No matches'))
                          : ListView(children: [
                              for (final id in ids)
                                CheckboxListTile(
                                    title: Text(_labels[id] ?? id),
                                    value: _selected.contains(id),
                                    onChanged: !widget.canChange(id)
                                        ? null
                                        : (checked) => setState(() {
                                              if (checked == true) {
                                                if (widget.single) {
                                                  _selected.clear();
                                                }
                                                if (_selected.length < 100) {
                                                  _selected.add(id);
                                                }
                                              } else {
                                                _selected.remove(id);
                                              }
                                            })),
                              if (_selected.length >= 100)
                                const Padding(
                                    padding: EdgeInsets.all(16),
                                    child:
                                        Text('Up to 100 selections allowed.')),
                              if (_more)
                                ActionButton(
                                    kind: ActionButtonKind.text,
                                    onPressed: _loading
                                        ? null
                                        : () => _search(more: true),
                                    child: const Text('Load more')),
                            ])),
                  SafeArea(
                      top: false,
                      child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: ActionButton(
                              onPressed: () =>
                                  Navigator.pop(context, _selected.toList()),
                              child: const Text('Done')))),
                ])));
  }
}

Future<void> openTrackerLink(BuildContext context, String url) async {
  try {
    if (!trackerSafeUrl(url) ||
        !await launchUrl(Uri.parse(url),
            mode: LaunchMode.externalApplication)) {
      throw const UserInputException('This link could not be opened.');
    }
  } on Object catch (e) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content:
              Text(userFacingError(e, summary: 'Could not open attachment'))));
    }
  }
}

class TrackerAttachments extends StatefulWidget {
  const TrackerAttachments(
      {super.key,
      required this.field,
      required this.values,
      required this.channel,
      this.repository,
      this.onChanged,
      this.onBusyChanged});
  final TrackerField field;
  final List<Object?> values;
  final EntityRef channel;
  final KaedeRepository? repository;
  final ValueChanged<List<Object?>>? onChanged;
  final ValueChanged<bool>? onBusyChanged;
  @override
  State<TrackerAttachments> createState() => _TrackerAttachmentsState();
}

class _TrackerAttachmentsState extends State<TrackerAttachments> {
  bool _busy = false, _cancelled = false;
  double? _progress;
  String? _status, _error;
  Future<void> _upload() async {
    if (_busy || widget.repository == null) return;
    setState(() {
      _busy = true;
      _cancelled = false;
      _error = null;
      _status = 'Choosing files…';
    });
    widget.onBusyChanged?.call(true);
    try {
      final picked = await FilePicker.platform
          .pickFiles(allowMultiple: true, withData: false);
      if (!mounted || _cancelled || picked == null) return;
      if (widget.values.length + picked.files.length > 10) {
        throw const UserInputException(
            'Each field can hold up to 10 attachments.');
      }
      var values = [...widget.values];
      for (final file in picked.files) {
        if (!mounted || _cancelled) return;
        if (file.path == null) {
          throw const UserInputException(
              'This file could not be opened. Please choose it again.');
        }
        setState(() {
          _status = 'Uploading ${file.name}';
          _progress = 0;
        });
        final result = await widget.repository!
            .uploadTrackerFile(widget.channel, File(file.path!), file.name,
                contentType: _mime(file.extension),
                isActive: () => mounted && !_cancelled,
                onProgress: (sent, total) {
                  if (mounted) {
                    setState(() => _progress = total > 0 ? sent / total : null);
                  }
                },
                onProcessing: () {
                  if (mounted) {
                    setState(() {
                      _status = 'Checking ${file.name}…';
                      _progress = null;
                    });
                  }
                });
        if (!mounted || _cancelled) return;
        values = [...values, result];
        widget.onChanged?.call(values);
      }
    } on Object catch (e) {
      if (mounted && !_cancelled) {
        setState(() =>
            _error = userFacingError(e, summary: 'Could not add attachment'));
      }
    } finally {
      if (mounted) {
        setState(() => _busy = false);
        widget.onBusyChanged?.call(false);
      }
    }
  }

  Future<void> _link() async {
    final result = await showDialog<Json>(
        context: context, builder: (_) => const _TrackerAttachmentLinkDialog());
    if (mounted && result != null) {
      widget.onChanged?.call([...widget.values, result]);
    }
  }

  @override
  Widget build(BuildContext context) =>
      Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(widget.field.name, style: Theme.of(context).textTheme.titleSmall),
        if (widget.values.isEmpty)
          const Padding(
              padding: EdgeInsets.symmetric(vertical: 8),
              child: Text('No attachments')),
        for (var i = 0; i < widget.values.length; i++)
          if (widget.values[i] is Map)
            TrackerAttachmentTile(
                key: ValueKey(
                    '${widget.field.id}-${(widget.values[i] as Map)['id'] ?? (widget.values[i] as Map)['url']}'),
                value: Map<String, Object?>.from(widget.values[i] as Map),
                repository: widget.repository,
                channel: widget.channel,
                onRemove: widget.onChanged == null || _busy
                    ? null
                    : () => widget.onChanged!([...widget.values]..removeAt(i))),
        if (_busy) ...[
          const SizedBox(height: 8),
          LinearProgressIndicator(value: _progress),
          const SizedBox(height: 8),
          Text(_cancelled ? 'Cancelling upload…' : _status ?? 'Uploading…'),
          Align(
              alignment: Alignment.centerLeft,
              child: ActionButton(
                  kind: ActionButtonKind.text,
                  onPressed: _cancelled
                      ? null
                      : () {
                          setState(() => _cancelled = true);
                          widget.onBusyChanged?.call(false);
                        },
                  child: const Text('Cancel upload')))
        ],
        if (_error != null)
          Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Text(_error!,
                  style:
                      TextStyle(color: Theme.of(context).colorScheme.error))),
        if (widget.onChanged != null)
          Wrap(spacing: 8, children: [
            ActionButton(
                kind: ActionButtonKind.outlined,
                onPressed: _busy ||
                        widget.values.length >= 10 ||
                        widget.repository == null
                    ? null
                    : _upload,
                icon: const Icon(Icons.upload_file),
                label: const Text('Upload files')),
            ActionButton(
                kind: ActionButtonKind.text,
                onPressed: _busy || widget.values.length >= 10 ? null : _link,
                icon: const Icon(Icons.link),
                label: const Text('Add link')),
          ]),
      ]);
}

String _mime(String? extension) => switch (extension?.toLowerCase()) {
      'png' => 'image/png',
      'jpg' || 'jpeg' => 'image/jpeg',
      'gif' => 'image/gif',
      'webp' => 'image/webp',
      'heic' => 'image/heic',
      'avif' => 'image/avif',
      'mp4' || 'm4v' => 'video/mp4',
      'mov' => 'video/quicktime',
      'webm' => 'video/webm',
      'pdf' => 'application/pdf',
      'txt' => 'text/plain',
      'csv' => 'text/csv',
      'mp3' => 'audio/mpeg',
      'ogg' => 'audio/ogg',
      _ => 'application/octet-stream',
    };

class TrackerAttachmentTile extends StatefulWidget {
  const TrackerAttachmentTile(
      {super.key,
      required this.value,
      required this.channel,
      this.repository,
      this.onRemove});
  final Json value;
  final EntityRef channel;
  final KaedeRepository? repository;
  final VoidCallback? onRemove;
  @override
  State<TrackerAttachmentTile> createState() => _TrackerAttachmentTileState();
}

class _TrackerAttachmentTileState extends State<TrackerAttachmentTile> {
  bool _loading = false;
  String? _error;
  Future<void> _open() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final url = widget.value['id'] != null
          ? (await widget.repository!.trackerMedia(widget.channel, 'read',
              {'attachment_id': widget.value['id']}))['url'] as String
          : widget.value['url'] as String;
      if (!trackerSafeUrl(url)) {
        throw const UserInputException('This attachment link is invalid.');
      }
      if (!mounted) return;
      if (widget.value['type'] == 'image' || widget.value['type'] == 'video') {
        await showDialog<void>(
            context: context,
            useSafeArea: true,
            builder: (_) => _TrackerMediaPreview(
                url: url,
                name: '${widget.value['name']}',
                video: widget.value['type'] == 'video'));
      } else {
        await openTrackerLink(context, url);
      }
    } on Object catch (e) {
      if (mounted) {
        setState(() =>
            _error = userFacingError(e, summary: 'Could not open attachment'));
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) =>
      Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Card(
            margin: const EdgeInsets.symmetric(vertical: 4),
            child: ListTile(
              leading: _loading
                  ? const SizedBox(
                      width: 24,
                      height: 24,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : Icon(widget.value['type'] == 'image'
                      ? Icons.image_outlined
                      : widget.value['type'] == 'video'
                          ? Icons.movie_outlined
                          : Icons.attach_file),
              title: Text('${widget.value['name']}',
                  maxLines: 2, overflow: TextOverflow.ellipsis),
              subtitle: Text(widget.value['type'] == 'file'
                  ? 'Open file'
                  : 'Tap to preview'),
              onTap: _loading ||
                      (widget.value['id'] != null && widget.repository == null)
                  ? null
                  : _open,
              trailing: widget.onRemove == null
                  ? null
                  : ActionButton(
                      kind: ActionButtonKind.icon,
                      tooltip: 'Remove attachment',
                      onPressed: widget.onRemove,
                      icon: const Icon(Icons.close)),
            )),
        if (_error != null)
          Text(_error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error)),
      ]);
}

class _TrackerMediaPreview extends StatefulWidget {
  const _TrackerMediaPreview(
      {required this.url, required this.name, required this.video});
  final String url, name;
  final bool video;
  @override
  State<_TrackerMediaPreview> createState() => _TrackerMediaPreviewState();
}

class _TrackerMediaPreviewState extends State<_TrackerMediaPreview>
    with WidgetsBindingObserver {
  VideoPlayerController? _video;
  String? _error;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    if (widget.video) _initialize();
  }

  Future<void> _initialize() async {
    final player = VideoPlayerController.networkUrl(Uri.parse(widget.url));
    _video = player;
    try {
      await player.initialize();
      if (mounted) setState(() {});
    } on Object {
      if (mounted) {
        setState(() => _error =
            'This video could not be previewed. You can open it externally.');
      }
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) _video?.pause();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _video?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Dialog(
      insetPadding: const EdgeInsets.all(12),
      child: SizedBox(
        height: MediaQuery.sizeOf(context).height * .8,
        child: Column(children: [
          ListTile(
              title: Text(widget.name,
                  maxLines: 2, overflow: TextOverflow.ellipsis),
              trailing: ActionButton(
                  kind: ActionButtonKind.icon,
                  tooltip: 'Close preview',
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.close))),
          Expanded(
              child: _error != null
                  ? Center(
                      child: Padding(
                          padding: const EdgeInsets.all(20),
                          child: Text(_error!)))
                  : widget.video
                      ? _video?.value.isInitialized == true
                          ? Center(
                              child: AspectRatio(
                                  aspectRatio: _video!.value.aspectRatio,
                                  child: VideoPlayer(_video!)))
                          : const Center(child: CircularProgressIndicator())
                      : InteractiveViewer(
                          minScale: .5,
                          maxScale: 5,
                          child: Center(
                              child: Image.network(widget.url,
                                  cacheWidth: 2048,
                                  fit: BoxFit.contain,
                                  loadingBuilder: (_, child, progress) =>
                                      progress == null
                                          ? child
                                          : const Center(
                                              child:
                                                  CircularProgressIndicator()),
                                  errorBuilder: (_, error, stack) => const Padding(
                                      padding: EdgeInsets.all(20),
                                      child: Text(
                                          'This image could not be previewed. You can open it externally.')))))),
          if (_video?.value.isInitialized == true)
            ValueListenableBuilder<VideoPlayerValue>(
                valueListenable: _video!,
                builder: (context, value, _) => Column(children: [
                      VideoProgressIndicator(_video!,
                          allowScrubbing: true,
                          padding: const EdgeInsets.all(16)),
                      ActionButton(
                          kind: ActionButtonKind.icon,
                          tooltip: value.isPlaying ? 'Pause' : 'Play',
                          onPressed: () => value.isPlaying
                              ? _video!.pause()
                              : _video!.play(),
                          icon: Icon(value.isPlaying
                              ? Icons.pause
                              : Icons.play_arrow)),
                    ])),
          ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => openTrackerLink(context, widget.url),
              icon: const Icon(Icons.open_in_new),
              label: const Text('Open externally')),
          const SizedBox(height: 8),
        ]),
      ));
}

class _TrackerAttachmentLinkDialog extends StatefulWidget {
  const _TrackerAttachmentLinkDialog();
  @override
  State<_TrackerAttachmentLinkDialog> createState() =>
      _TrackerAttachmentLinkDialogState();
}

class _TrackerAttachmentLinkDialogState
    extends State<_TrackerAttachmentLinkDialog> {
  final form = GlobalKey<FormState>();
  final name = TextEditingController(), url = TextEditingController();
  String type = 'file';
  @override
  void dispose() {
    name.dispose();
    url.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Add attachment link'),
        content: SingleChildScrollView(
            child: Form(
                key: form,
                child: Column(mainAxisSize: MainAxisSize.min, children: [
                  TextFormField(
                      controller: name,
                      maxLength: 255,
                      decoration: const InputDecoration(labelText: 'Name'),
                      validator: (v) =>
                          v?.trim().isEmpty != false ? 'Enter a name' : null),
                  TextFormField(
                      controller: url,
                      maxLength: 2048,
                      keyboardType: TextInputType.url,
                      autocorrect: false,
                      decoration: const InputDecoration(labelText: 'Link'),
                      validator: (v) => trackerSafeUrl(v?.trim() ?? '')
                          ? null
                          : 'Enter a full http:// or https:// link'),
                  DropdownButtonFormField<String>(
                      initialValue: type,
                      decoration: const InputDecoration(labelText: 'Preview'),
                      items: const [
                        DropdownMenuItem(value: 'file', child: Text('File')),
                        DropdownMenuItem(value: 'image', child: Text('Image')),
                        DropdownMenuItem(value: 'video', child: Text('Video'))
                      ],
                      onChanged: (v) => setState(() => type = v!)),
                ]))),
        actions: [
          ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel')),
          ActionButton(
              onPressed: () {
                if (form.currentState?.validate() == true) {
                  Navigator.pop(context, <String, Object?>{
                    'name': name.text.trim(),
                    'url': url.text.trim(),
                    'type': type
                  });
                }
              },
              child: const Text('Add link'))
        ],
      );
}
