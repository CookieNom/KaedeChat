import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';

class ChildSafetyQueue extends StatefulWidget {
  const ChildSafetyQueue(
      {super.key,
      required this.repository,
      required this.canManage,
      required this.canViewEvidence});
  final KaedeRepository repository;
  final bool canManage;
  final bool canViewEvidence;

  @override
  State<ChildSafetyQueue> createState() => _ChildSafetyQueueState();
}

class _ChildSafetyQueueState extends State<ChildSafetyQueue> {
  late Future<Map<String, Object?>> _load;
  @override
  void initState() {
    super.initState();
    _refresh();
  }

  void _refresh() {
    _load =
        widget.repository.api.getJson('/api/v1/administration/child-safety');
  }

  @override
  Widget build(BuildContext context) =>
      Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const SettingsSectionHeader('Child safety',
            subheading:
                'Restricted reports and preserved evidence. Media is hidden until explicitly opened.'),
        ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: () => setState(_refresh),
            child: const Text('Refresh cases')),
        FutureBuilder<Map<String, Object?>>(
          future: _load,
          builder: (context, snapshot) {
            if (snapshot.hasError) {
              return Text(userFacingError(snapshot.error!));
            }
            if (!snapshot.hasData) {
              return const Center(child: CircularProgressIndicator());
            }
            final cases = (snapshot.data!['cases'] as List? ?? const [])
                .whereType<Map<Object?, Object?>>();
            if (cases.isEmpty) {
              return const Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('No child safety cases.'));
            }
            return Column(children: [
              for (final item in cases)
                Card(
                    child: ListTile(
                  leading: const Icon(Icons.shield_outlined),
                  title: Text('Case ${item['id']} · ${item['status']}'),
                  subtitle: Text('${item['reason'] ?? item['source'] ?? ''}',
                      maxLines: 2, overflow: TextOverflow.ellipsis),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: () async {
                    await Navigator.of(context).push(MaterialPageRoute<void>(
                        builder: (_) => _ChildSafetyCase(
                            repository: widget.repository,
                            id: '${item['id']}',
                            canManage: widget.canManage,
                            canViewEvidence: widget.canViewEvidence)));
                    if (mounted) setState(_refresh);
                  },
                )),
            ]);
          },
        ),
      ]);
}

class _ChildSafetyCase extends StatefulWidget {
  const _ChildSafetyCase(
      {required this.repository,
      required this.id,
      required this.canManage,
      required this.canViewEvidence});
  final KaedeRepository repository;
  final String id;
  final bool canManage;
  final bool canViewEvidence;
  @override
  State<_ChildSafetyCase> createState() => _ChildSafetyCaseState();
}

class _ChildSafetyCaseState extends State<_ChildSafetyCase> {
  Map<String, Object?>? _case;
  String? _error;
  bool _busy = false;
  String get _path =>
      '/api/v1/administration/child-safety/${Uri.encodeComponent(widget.id)}';

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    try {
      final result = await widget.repository.api.getJson(_path);
      if (mounted) {
        setState(() {
          _case = result;
          _error = null;
        });
      }
    } on Object catch (error) {
      if (mounted) setState(() => _error = userFacingError(error));
    }
  }

  Future<Map<String, Object?>?> _details(String title,
      {bool reference = false,
      bool date = false,
      bool futureDate = false,
      bool suspend = false,
      bool sourceRequired = false}) async {
    final reason = TextEditingController();
    final referenceText = TextEditingController();
    final minimumDate = futureDate
        ? (DateTime.tryParse('${_case?['retain_until']}')?.toLocal() ??
                DateTime.now())
            .add(const Duration(days: 1))
        : DateTime(2020);
    var selectedDate = futureDate ? minimumDate : DateTime.now();
    var suspendAccount = false;
    var sourceVerified = false;
    try {
      return await showDialog<Map<String, Object?>>(
          context: context,
          builder: (dialogContext) => StatefulBuilder(
                builder: (context, update) => AlertDialog(
                  title: Text(title),
                  content: SingleChildScrollView(
                      child: Column(mainAxisSize: MainAxisSize.min, children: [
                    if (reference)
                      TextField(
                          controller: referenceText,
                          maxLength: 200,
                          decoration: const InputDecoration(
                              labelText: 'External reference'),
                          onChanged: (_) => update(() {})),
                    TextField(
                        controller: reason,
                        minLines: 2,
                        maxLines: 4,
                        maxLength: 2000,
                        decoration:
                            const InputDecoration(labelText: 'Reason / notes'),
                        onChanged: (_) => update(() {})),
                    if (date)
                      ListTile(
                          title: const Text('Date'),
                          subtitle: Text(MaterialLocalizations.of(context)
                              .formatMediumDate(selectedDate)),
                          trailing: const Icon(Icons.calendar_today),
                          onTap: () async {
                            final value = await showDatePicker(
                                context: context,
                                initialDate: selectedDate,
                                firstDate: minimumDate,
                                lastDate: futureDate
                                    ? selectedDate
                                        .add(const Duration(days: 3650))
                                    : DateTime.now());
                            if (value != null) {
                              update(() => selectedDate = value);
                            }
                          }),
                    if (sourceRequired)
                      CheckboxListTile(
                          value: sourceVerified,
                          onChanged: (value) =>
                              update(() => sourceVerified = value == true),
                          title: const Text(
                              'I verified that the evidence belongs to the reported message.')),
                    if (suspend)
                      CheckboxListTile(
                          value: suspendAccount,
                          onChanged: (value) =>
                              update(() => suspendAccount = value == true),
                          title: const Text('Suspend the linked account')),
                  ])),
                  actions: [
                    ActionButton(
                        kind: ActionButtonKind.text,
                        onPressed: () => Navigator.pop(dialogContext),
                        child: const Text('Cancel')),
                    ActionButton(
                        onPressed: (sourceRequired && !sourceVerified) ||
                                reason.text.trim().length < 3 ||
                                (reference && referenceText.text.trim().isEmpty)
                            ? null
                            : () =>
                                Navigator.pop(dialogContext, <String, Object?>{
                                  'reason': reason.text.trim(),
                                  if (sourceRequired)
                                    'source_verified': sourceVerified,
                                  if (reference)
                                    'reference': referenceText.text.trim(),
                                  if (date)
                                    'date':
                                        selectedDate.toUtc().toIso8601String(),
                                  if (suspend)
                                    'suspend_account': suspendAccount,
                                }),
                        child: Text(title)),
                  ],
                ),
              ));
    } finally {
      reason.dispose();
      referenceText.dispose();
    }
  }

  Future<void> _action(String action, String title,
      {String? decision, String? holdId}) async {
    if (_busy || !widget.canManage) return;
    final details = await _details(title,
        reference: action == 'submissions' || action == 'holds',
        date: action == 'submissions' || action == 'retention',
        futureDate: action == 'retention',
        sourceRequired: decision == 'confirmed' &&
            _case?['provenance_verified'] != true &&
            _case?['message_ref'] != null,
        suspend: decision == 'confirmed' && _case?['can_suspend'] == true);
    if (details == null || !mounted) return;
    setState(() => _busy = true);
    try {
      final payload = <String, Object?>{...details};
      if (decision != null) payload['decision'] = decision;
      if (action == 'submissions') {
        payload['notes'] = payload.remove('reason');
        payload['submitted_at'] = payload.remove('date');
      }
      if (action == 'retention') {
        payload['retain_until'] = payload.remove('date');
      }
      final suffix = holdId == null
          ? action
          : 'holds/${Uri.encodeComponent(holdId)}/release';
      await widget.repository.api.sendJson(
          action == 'evidence' ? 'DELETE' : 'POST', '$_path/$suffix',
          data: payload);
      await _reload();
    } on Object catch (error) {
      if (mounted) {
        showActionFeedback(context, userFacingError(error), error: true);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _viewEvidence(Map<Object?, Object?> evidence) async {
    if (_busy || !widget.canViewEvidence) return;
    setState(() => _busy = true);
    Uint8List? bytes;
    MemoryImage? provider;
    try {
      bytes = Uint8List.fromList(await widget.repository.api.getBytes(
          '$_path/evidence/${Uri.encodeComponent('${evidence['id']}')}'));
      provider = MemoryImage(bytes);
      if (!mounted) return;
      await showDialog<void>(
          context: context,
          builder: (context) => Dialog.fullscreen(
                  child: Scaffold(
                appBar: AppBar(
                    title: const Text('Restricted evidence'),
                    leading: IconButton(
                        icon: const Icon(Icons.close),
                        tooltip: 'Close evidence',
                        onPressed: () => Navigator.pop(context))),
                body: Center(
                    child: InteractiveViewer(child: Image(image: provider!))),
              )));
    } on Object catch (error) {
      if (mounted) {
        showActionFeedback(context, userFacingError(error), error: true);
      }
    } finally {
      await provider?.evict();
      bytes?.fillRange(0, bytes.length, 0);
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final item = _case;
    final manage = widget.canManage && !_busy && item?['disposed_at'] == null;
    return Scaffold(
        appBar: AppBar(title: Text('Child safety case ${widget.id}')),
        body: SafeArea(
            child: ListView(padding: const EdgeInsets.all(16), children: [
          if (_error != null)
            SettingsStatusPanel.error(message: _error!, onRetry: _reload),
          if (item == null)
            const Center(child: CircularProgressIndicator())
          else ...[
            Text('Status: ${item['status']}',
                style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 12),
            if (item['status'] == 'confirmed') ...[
              Text(item['removal_completed_at'] != null
                  ? 'Storage removal verified for this instance. Restricted evidence is preserved separately.'
                  : 'Removal pending. New download links are blocked. Storage deletion is retried automatically; existing links may still work until deletion succeeds or they expire.'),
              const SizedBox(height: 8),
              const Text(
                  'Client cleanup requires a supported client to reconnect. Copies saved outside the app cannot be erased remotely.'),
              const SizedBox(height: 12),
            ],
            Text('${item['reason'] ?? ''}'),
            Text(item['provenance_verified'] == true
                ? 'Evidence is linked to the original content.'
                : 'Reporter-supplied evidence is not verified as the original content.'),
            const SettingsSectionHeader('Preserved evidence'),
            for (final evidence in (item['evidence'] as List? ?? const [])
                .whereType<Map<Object?, Object?>>())
              ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(Icons.lock_outline),
                  title: Text('${evidence['filename'] ?? 'Evidence'}'),
                  subtitle: Text(
                      '${evidence['content_type']} · ${evidence['size']} bytes'),
                  trailing: widget.canViewEvidence &&
                          '${evidence['content_type']}'.startsWith('image/')
                      ? ActionButton(
                          kind: ActionButtonKind.outlined,
                          onPressed: _busy || evidence['preserved_at'] == null
                              ? null
                              : () => _viewEvidence(evidence),
                          child: Text(evidence['preserved_at'] == null
                              ? 'Preservation pending'
                              : 'View evidence'))
                      : null),
            if (widget.canManage) ...[
              const SettingsSectionHeader('Review'),
              Wrap(spacing: 8, runSpacing: 8, children: [
                ActionButton(
                    onPressed: manage && item['status'] == 'pending'
                        ? () => _action('review', 'Confirm finding',
                            decision: 'confirmed')
                        : null,
                    child: const Text('Confirm finding')),
                ActionButton(
                    kind: ActionButtonKind.outlined,
                    onPressed: manage && item['status'] == 'pending'
                        ? () => _action('review', 'Dismiss finding',
                            decision: 'dismissed')
                        : null,
                    child: const Text('Dismiss finding')),
              ]),
              const SettingsSectionHeader('Reporting and retention'),
              Text('Retain until: ${item['retain_until'] ?? 'Not set'}'),
              if (item['closed_at'] != null)
                Text('Closed: ${item['closed_at']}'),
              Wrap(spacing: 8, runSpacing: 8, children: [
                ActionButton(
                    onPressed: manage && item['status'] == 'confirmed'
                        ? () => _action('submissions', 'Record submission')
                        : null,
                    child: const Text('Record submission')),
                ActionButton(
                    kind: ActionButtonKind.outlined,
                    onPressed: manage
                        ? () => _action('retention', 'Set retention date')
                        : null,
                    child: const Text('Set retention date')),
                ActionButton(
                    kind: ActionButtonKind.outlined,
                    onPressed: manage
                        ? () => _action('holds', 'Add legal hold')
                        : null,
                    child: const Text('Add legal hold')),
                ActionButton(
                    kind: ActionButtonKind.outlined,
                    onPressed: manage &&
                            item['closed_at'] == null &&
                            (item['status'] == 'dismissed' ||
                                (item['status'] == 'confirmed' &&
                                    (item['submissions'] as List? ?? const [])
                                        .isNotEmpty))
                        ? () => _action('close', 'Close case')
                        : null,
                    child: const Text('Close case')),
                ActionButton(
                    kind: ActionButtonKind.outlined,
                    onPressed: manage &&
                            widget.canViewEvidence &&
                            item['can_delete_evidence'] == true
                        ? () => _action('evidence', 'Delete eligible evidence')
                        : null,
                    child: const Text('Delete eligible evidence')),
              ]),
              if (item['can_delete_evidence'] != true)
                const SettingsInfo(
                    'Evidence cannot be deleted until storage removal, case closure, retention, and legal holds permit it.'),
            ],
            for (final hold in (item['holds'] as List? ?? const [])
                .whereType<Map<Object?, Object?>>())
              ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text('Legal hold: ${hold['reason']}'),
                  subtitle: Text('${hold['reference'] ?? ''}'),
                  trailing: widget.canManage
                      ? ActionButton(
                          kind: ActionButtonKind.outlined,
                          onPressed: manage && hold['released_at'] == null
                              ? () => _action('release', 'Release hold',
                                  holdId: '${hold['id']}')
                              : null,
                          child: Text(hold['released_at'] == null
                              ? 'Release hold'
                              : 'Released'))
                      : null),
            for (final submission in (item['submissions'] as List? ?? const [])
                .whereType<Map<Object?, Object?>>())
              ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text('Submission: ${submission['reference']}'),
                  subtitle: Text(
                      '${submission['submitted_at']}\n${submission['notes'] ?? ''}')),
            if (_busy)
              const Padding(
                  padding: EdgeInsets.all(16),
                  child: LinearProgressIndicator()),
          ],
        ])));
  }
}
