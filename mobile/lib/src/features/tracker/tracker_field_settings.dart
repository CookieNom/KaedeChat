import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:uuid/uuid.dart';

class TrackerFieldSettingsSheet extends StatefulWidget {
  const TrackerFieldSettingsSheet(
      {super.key,
      required this.board,
      required this.channel,
      required this.repository});
  final TrackerBoard board;
  final EntityRef channel;
  final KaedeRepository repository;
  @override
  State<TrackerFieldSettingsSheet> createState() =>
      _TrackerFieldSettingsSheetState();
}

class _TrackerFieldSettingsSheetState extends State<TrackerFieldSettingsSheet> {
  late TrackerBoard _board = widget.board;
  late final _prefix = TextEditingController(text: widget.board.keyPrefix);
  late List<TrackerField> _fields = [...widget.board.customFields];
  final _form = GlobalKey<FormState>();
  bool _saving = false, _conflict = false, _dirty = false;
  String? _error;
  @override
  void dispose() {
    _prefix.dispose();
    super.dispose();
  }

  Future<bool> _discard() async =>
      !_dirty ||
      await showDialog<bool>(
              context: context,
              builder: (context) => AlertDialog(
                      title: const Text('Discard changes?'),
                      content: const Text(
                          'Your changes to tracker settings have not been saved.'),
                      actions: [
                        TextButton(
                            onPressed: () => Navigator.pop(context, false),
                            child: const Text('Keep editing')),
                        FilledButton(
                            onPressed: () => Navigator.pop(context, true),
                            child: const Text('Discard'))
                      ])) ==
          true;
  Future<void> _save() async {
    if (_form.currentState?.validate() != true) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await widget.repository.updateTrackerBoard(widget.channel, _board.version,
          keyPrefix: _prefix.text.trim().toUpperCase(), customFields: _fields);
      if (mounted) Navigator.pop(context, true);
    } on Object catch (e) {
      if (mounted) {
        setState(() {
          _error =
              userFacingError(e, summary: 'Could not save tracker settings');
          _conflict =
              e is KaedeException && (e.status == 412 || e.status == 428);
        });
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _reload() async {
    if (!await _discard() || !mounted) return;
    setState(() => _saving = true);
    try {
      final board = await widget.repository.trackerBoard(widget.channel);
      if (mounted) {
        setState(() {
          _board = board;
          _fields = [...board.customFields];
          _prefix.text = board.keyPrefix;
          _error = null;
          _conflict = false;
          _dirty = false;
        });
      }
    } on Object catch (e) {
      if (mounted) {
        setState(() => _error =
            userFacingError(e, summary: 'Could not reload tracker settings'));
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _edit([TrackerField? field]) async {
    final result = await showModalBottomSheet<TrackerField>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        showDragHandle: true,
        builder: (_) => TrackerFieldEditorSheet(field: field, fields: _fields));
    if (!mounted || result == null) return;
    setState(() {
      _dirty = true;
      if (field == null) {
        _fields.add(result);
      } else {
        _fields[_fields.indexOf(field)] = result;
      }
    });
  }

  Future<void> _delete(TrackerField field) async {
    final confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
                title: Text('Remove ${field.name}?'),
                content: const Text(
                    'Saving this change will remove this field and its values from every task in this channel.'),
                actions: [
                  TextButton(
                      onPressed: () => Navigator.pop(context, false),
                      child: const Text('Cancel')),
                  FilledButton(
                      onPressed: () => Navigator.pop(context, true),
                      child: const Text('Remove field'))
                ]));
    if (mounted && confirmed == true) {
      setState(() {
        _fields.remove(field);
        _dirty = true;
      });
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
      canPop: !_saving && !_dirty,
      onPopInvokedWithResult: (didPop, result) async {
        if (!didPop && !_saving && await _discard() && context.mounted) {
          setState(() => _dirty = false);
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (context.mounted) Navigator.pop(context);
          });
        }
      },
      child: Padding(
          padding:
              EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
          child: SizedBox(
            height: MediaQuery.sizeOf(context).height * .9,
            child: Form(
                key: _form,
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      ListTile(
                          title: const Text('Tracker settings'),
                          trailing: IconButton(
                              tooltip: 'Close settings',
                              onPressed: _saving
                                  ? null
                                  : () => Navigator.maybePop(context),
                              icon: const Icon(Icons.close))),
                      Expanded(
                          child: AbsorbPointer(
                              absorbing: _saving,
                              child: ListView(
                                  padding:
                                      const EdgeInsets.fromLTRB(20, 12, 20, 12),
                                  children: [
                                    TextFormField(
                                        key: const ValueKey(
                                            'tracker-prefix-field'),
                                        controller: _prefix,
                                        maxLength: 10,
                                        textCapitalization:
                                            TextCapitalization.characters,
                                        onChanged: (_) =>
                                            setState(() => _dirty = true),
                                        decoration: const InputDecoration(
                                            labelText: 'Task key prefix',
                                            helperText:
                                                '2–10 letters or digits; starts with a letter'),
                                        validator: (v) =>
                                            RegExp(r'^[A-Za-z][A-Za-z0-9]{1,9}$')
                                                    .hasMatch(v?.trim() ?? '')
                                                ? null
                                                : 'Enter a valid key prefix'),
                                    const SizedBox(height: 20),
                                    Text('Task fields',
                                        style: Theme.of(context)
                                            .textTheme
                                            .titleLarge),
                                    const SizedBox(height: 8),
                                    const Text(
                                        'Add fields to every task in this channel. People fields can hold reviewers or other extra assignments.'),
                                    const SizedBox(height: 12),
                                    OutlinedButton.icon(
                                        onPressed:
                                            _fields.length >= 50 ? null : _edit,
                                        icon: const Icon(Icons.add),
                                        label: Text(
                                            'Add field (${_fields.length}/50)')),
                                    if (_fields.isEmpty)
                                      const Padding(
                                          padding: EdgeInsets.symmetric(
                                              vertical: 16),
                                          child: Text('No custom fields yet.')),
                                    for (var i = 0; i < _fields.length; i++)
                                      Card(
                                          margin: const EdgeInsets.symmetric(
                                              vertical: 6),
                                          child: Column(children: [
                                            ListTile(
                                                title: Text(_fields[i].name),
                                                subtitle: Text(
                                                    trackerFieldTypes[
                                                            _fields[i].type] ??
                                                        _fields[i].type),
                                                onTap: () => _edit(_fields[i]),
                                                trailing: IconButton(
                                                    tooltip: 'Edit field',
                                                    onPressed: () =>
                                                        _edit(_fields[i]),
                                                    icon: const Icon(
                                                        Icons.edit_outlined))),
                                            Padding(
                                                padding:
                                                    const EdgeInsets.fromLTRB(
                                                        8, 0, 8, 8),
                                                child: Row(children: [
                                                  IconButton(
                                                      tooltip: 'Move field up',
                                                      onPressed: i == 0
                                                          ? null
                                                          : () => setState(() {
                                                                final f = _fields
                                                                    .removeAt(
                                                                        i);
                                                                _fields.insert(
                                                                    i - 1, f);
                                                                _dirty = true;
                                                              }),
                                                      icon: const Icon(
                                                          Icons.arrow_upward)),
                                                  IconButton(
                                                      tooltip:
                                                          'Move field down',
                                                      onPressed: i ==
                                                              _fields.length - 1
                                                          ? null
                                                          : () => setState(() {
                                                                final f = _fields
                                                                    .removeAt(
                                                                        i);
                                                                _fields.insert(
                                                                    i + 1, f);
                                                                _dirty = true;
                                                              }),
                                                      icon: const Icon(Icons
                                                          .arrow_downward)),
                                                  const Spacer(),
                                                  TextButton.icon(
                                                      onPressed: () =>
                                                          _delete(_fields[i]),
                                                      icon: const Icon(
                                                          Icons.delete_outline),
                                                      label:
                                                          const Text('Remove')),
                                                ])),
                                          ])),
                                  ]))),
                      if (_error != null)
                        ConstrainedBox(
                            constraints: const BoxConstraints(maxHeight: 120),
                            child: SingleChildScrollView(
                                padding:
                                    const EdgeInsets.fromLTRB(20, 12, 20, 12),
                                child: Column(children: [
                                  if (_error != null)
                                    Padding(
                                        padding: const EdgeInsets.symmetric(
                                            vertical: 12),
                                        child: Text(_error!,
                                            style: TextStyle(
                                                color: Theme.of(context)
                                                    .colorScheme
                                                    .error))),
                                  if (_conflict)
                                    TextButton(
                                        onPressed: _reload,
                                        child: const Text(
                                            'Reload latest settings')),
                                ]))),
                      if (_saving) const LinearProgressIndicator(),
                      SafeArea(
                          top: false,
                          child: Padding(
                              padding: const EdgeInsets.all(16),
                              child: FilledButton.icon(
                                  onPressed:
                                      _saving || _conflict ? null : _save,
                                  icon: const Icon(Icons.save_outlined),
                                  label: Text(
                                      _saving ? 'Saving…' : 'Save settings')))),
                    ])),
          )));
}

class TrackerFieldEditorSheet extends StatefulWidget {
  const TrackerFieldEditorSheet({super.key, this.field, required this.fields});
  final TrackerField? field;
  final List<TrackerField> fields;
  @override
  State<TrackerFieldEditorSheet> createState() =>
      _TrackerFieldEditorSheetState();
}

class _TrackerFieldEditorSheetState extends State<TrackerFieldEditorSheet> {
  final _form = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.field?.name ?? '');
  late final _options =
      TextEditingController(text: widget.field?.options.join('\n') ?? '');
  late String _type = widget.field?.type ?? 'text';
  @override
  void dispose() {
    _name.dispose();
    _options.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
      child: ConstrainedBox(
        constraints:
            BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .85),
        child: Form(
            key: _form,
            child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  ListTile(
                      title: Text(widget.field == null
                          ? 'Add task field'
                          : 'Edit task field'),
                      trailing: IconButton(
                          tooltip: 'Close field editor',
                          onPressed: () => Navigator.pop(context),
                          icon: const Icon(Icons.close))),
                  Flexible(
                      child: SingleChildScrollView(
                          padding: const EdgeInsets.fromLTRB(20, 12, 20, 12),
                          child: Column(children: [
                            TextFormField(
                                key: const ValueKey('tracker-field-name'),
                                controller: _name,
                                maxLength: 100,
                                textCapitalization:
                                    TextCapitalization.sentences,
                                decoration: const InputDecoration(
                                    labelText: 'Field name'),
                                validator: (v) {
                                  final name = v?.trim() ?? '';
                                  if (name.isEmpty) return 'Enter a field name';
                                  if (widget.fields.any((f) =>
                                      f.id != widget.field?.id &&
                                      f.name.toLowerCase() ==
                                          name.toLowerCase())) {
                                    return 'A field already has this name';
                                  }
                                  return null;
                                }),
                            const SizedBox(height: 12),
                            DropdownButtonFormField<String>(
                                isExpanded: true,
                                initialValue: _type,
                                decoration: InputDecoration(
                                    labelText: 'Field type',
                                    helperText: widget.field == null
                                        ? null
                                        : 'To use another type, add a new field.'),
                                items: [
                                  for (final e in trackerFieldTypes.entries)
                                    DropdownMenuItem(
                                        value: e.key, child: Text(e.value)),
                                  if (!trackerFieldTypes.containsKey(_type))
                                    DropdownMenuItem(
                                        value: _type, child: Text(_type))
                                ],
                                onChanged: widget.field == null
                                    ? (v) => setState(() => _type = v!)
                                    : null),
                            if (_type == 'select' ||
                                _type == 'multiselect') ...[
                              const SizedBox(height: 16),
                              TextFormField(
                                  controller: _options,
                                  minLines: 4,
                                  maxLines: 8,
                                  maxLength: 10100,
                                  decoration: const InputDecoration(
                                      labelText: 'Choices',
                                      helperText:
                                          'One choice per line. Up to 100 unique choices.',
                                      alignLabelWithHint: true),
                                  validator: (v) {
                                    final options = (v ?? '')
                                        .split('\n')
                                        .map((o) => o.trim())
                                        .where((o) => o.isNotEmpty)
                                        .toList();
                                    if (options.isEmpty ||
                                        options.length > 100 ||
                                        options.any((o) => o.length > 100)) {
                                      return 'Add 1–100 choices, each up to 100 characters';
                                    }
                                    if (options.toSet().length !=
                                        options.length) {
                                      return 'Each choice must be unique';
                                    }
                                    return null;
                                  }),
                              if (widget.field != null)
                                const Text(
                                    'Choices used by existing tasks cannot be removed. Change those tasks first.'),
                            ],
                          ]))),
                  SafeArea(
                      top: false,
                      child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: FilledButton(
                              onPressed: () {
                                if (_form.currentState?.validate() != true) {
                                  return;
                                }
                                Navigator.pop(
                                    context,
                                    TrackerField(
                                        id: widget.field?.id ??
                                            const Uuid().v4(),
                                        name: _name.text.trim(),
                                        type: _type,
                                        options: _type == 'select' ||
                                                _type == 'multiselect'
                                            ? _options.text
                                                .split('\n')
                                                .map((o) => o.trim())
                                                .where((o) => o.isNotEmpty)
                                                .toList()
                                            : const []));
                              },
                              child: Text(widget.field == null
                                  ? 'Add field'
                                  : 'Done')))),
                ])),
      ));
}
