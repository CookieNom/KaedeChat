import 'dart:async';
import 'dart:io';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/guild_admin_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_admin.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

List<String> _lines(String value, {bool commaSeparated = false}) {
  final source = commaSeparated ? value.replaceAll(',', '\n') : value;
  final result = <String>[];
  for (final line in source.split('\n')) {
    final cleaned = line.trim();
    if (cleaned.isNotEmpty && !result.contains(cleaned)) result.add(cleaned);
  }
  return result;
}

List<EntityRef> parseCanonicalUserRefs(String value, Domain localDomain) {
  final refs = <EntityRef>[];
  for (final raw in _lines(value, commaSeparated: true)) {
    final ref = EntityRef.parse(raw, localDomain: localDomain);
    if (!refs.contains(ref)) refs.add(ref);
  }
  if (refs.isEmpty) {
    throw FormatException('Enter at least one user reference.');
  }
  if (refs.length > 200) {
    throw FormatException('A bulk ban can include at most 200 users.');
  }
  return refs;
}

String? soundboardContentType(String filename) {
  final lower = filename.toLowerCase();
  if (lower.endsWith('.mp3')) return 'audio/mpeg';
  if (lower.endsWith('.ogg') || lower.endsWith('.oga')) return 'audio/ogg';
  return null;
}

String? autoModDraftValidationMessage(AutoModRuleDraft draft) {
  if (draft.name.trim().isEmpty || draft.name.trim().length > 100) {
    return 'Rule names must contain 1–100 characters.';
  }
  if (draft.actions.isEmpty) return 'Choose at least one action.';
  if (draft.actions.map((item) => item.type).toSet().length !=
      draft.actions.length) {
    return 'Each action can be added only once.';
  }
  final metadata = draft.triggerMetadata;
  switch (draft.triggerType) {
    case 'keyword':
    case 'member_profile':
      if (metadata.keywordFilter.isEmpty && metadata.regexPatterns.isEmpty) {
        return 'Add at least one keyword or regular expression.';
      }
      break;
    case 'keyword_preset':
      if (metadata.presets.isEmpty) {
        return 'Choose at least one keyword preset.';
      }
      break;
    case 'mention_spam':
      if (metadata.mentionTotalLimit == null) {
        return 'Choose a mention limit.';
      }
      break;
  }
  if (draft.triggerType == 'member_profile' &&
      draft.eventType != 'member_update') {
    return 'Member-profile rules must run when a member is updated.';
  }
  if (draft.triggerType != 'member_profile' &&
      draft.eventType != 'message_send') {
    return 'Message rules must run when a message is sent.';
  }
  if (draft.actions.any((item) => item.type == 'timeout') &&
      !{'keyword', 'mention_spam'}.contains(draft.triggerType)) {
    return 'Timeouts can be used only with keyword or mention-spam rules.';
  }
  if (draft.actions.any(
      (item) => item.type == 'send_alert_message' && item.channelRef == null)) {
    return 'Choose a plaintext text channel for AutoMod alerts.';
  }
  return null;
}

void _showError(BuildContext context, String summary, Object error) {
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      backgroundColor: context.kaede.danger,
      content: Text(userFacingError(error, summary: summary)),
    ),
  );
}

Future<bool> _confirm(
  BuildContext context, {
  required String title,
  required String body,
  required String action,
}) async =>
    await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(title),
        content: Text(body),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text('Cancel'),
          ),
          FilledButton(
            style:
                FilledButton.styleFrom(backgroundColor: context.kaede.danger),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(action),
          ),
        ],
      ),
    ) ??
    false;

final class _AdminHint extends StatelessWidget {
  const _AdminHint(this.text);

  final String text;

  @override
  Widget build(BuildContext context) => Container(
        margin: EdgeInsets.only(bottom: 12),
        padding: EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          border: Border.all(color: context.kaede.border),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.info_outline_rounded,
                size: 18, color: context.kaede.muted),
            SizedBox(width: 10),
            Expanded(
              child: Text(text, style: TextStyle(color: context.kaede.muted)),
            ),
          ],
        ),
      );
}

final class GuildAutoModTab extends StatefulWidget {
  const GuildAutoModTab({
    required this.guild,
    required this.repository,
    super.key,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;

  @override
  State<GuildAutoModTab> createState() => _GuildAutoModTabState();
}

final class _GuildAutoModTabState extends State<GuildAutoModTab> {
  List<AutoModRule> _rules = const [];
  var _loading = true;
  var _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant GuildAutoModTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final rules = await widget.repository.autoModRules(widget.guild.ref);
      if (!mounted) return;
      setState(() {
        _rules = rules;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      _showError(context, 'Could not load AutoMod rules.', error);
      setState(() => _loading = false);
    }
  }

  Future<void> _edit([AutoModRule? existing]) async {
    final draft = await showDialog<AutoModRuleDraft>(
      context: context,
      barrierDismissible: false,
      builder: (_) => _AutoModRuleDialog(
        guild: widget.guild,
        existing: existing,
      ),
    );
    if (draft == null || !mounted) return;
    setState(() => _busy = true);
    try {
      if (existing == null) {
        await widget.repository.createAutoModRule(widget.guild.ref, draft);
      } else {
        await widget.repository
            .updateAutoModRule(widget.guild.ref, existing.ref, draft);
      }
      await _load();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(existing == null
                ? 'AutoMod rule created.'
                : 'AutoMod rule saved.'),
          ),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        _showError(context, 'Could not save the AutoMod rule.', error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _toggle(AutoModRule rule, bool enabled) async {
    setState(() => _busy = true);
    try {
      final draft = AutoModRuleDraft.fromRule(rule);
      await widget.repository.updateAutoModRule(
        widget.guild.ref,
        rule.ref,
        AutoModRuleDraft(
          name: draft.name,
          eventType: draft.eventType,
          triggerType: draft.triggerType,
          triggerMetadata: draft.triggerMetadata,
          actions: draft.actions,
          enabled: enabled,
          exemptRoles: draft.exemptRoles,
          exemptChannels: draft.exemptChannels,
        ),
      );
      await _load();
    } on Object catch (error) {
      if (mounted) {
        _showError(context, 'Could not update the AutoMod rule.', error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _delete(AutoModRule rule) async {
    if (!await _confirm(
      context,
      title: 'Delete “${rule.name}”?',
      body: 'AutoMod will immediately stop enforcing this rule.',
      action: 'Delete rule',
    )) {
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.repository.deleteAutoModRule(widget.guild.ref, rule.ref);
      await _load();
    } on Object catch (error) {
      if (mounted) {
        _showError(context, 'Could not delete the AutoMod rule.', error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: settingsSurface(context),
        body: _loading
            ? Center(child: CircularProgressIndicator())
            : ListView(
                padding: EdgeInsets.fromLTRB(14, 12, 14, 90),
                children: [
                  const _AdminHint(
                    'AutoMod evaluates messages and member profiles on the '
                    'server. Exemptions, alert destinations, timeouts, and '
                    'role hierarchy are enforced even when no client is open.',
                  ),
                  if (_rules.isEmpty)
                    ListTile(
                      leading: Icon(Icons.shield_outlined),
                      title: Text('No AutoMod rules'),
                      subtitle:
                          Text('Create a rule to start filtering activity.'),
                    ),
                  for (final rule in _rules)
                    Card(
                      child: ListTile(
                        onTap: _busy ? null : () => _edit(rule),
                        leading: Icon(
                          rule.enabled
                              ? Icons.shield_rounded
                              : Icons.shield_outlined,
                          color: rule.enabled
                              ? context.kaede.mint
                              : context.kaede.muted,
                        ),
                        title: Text(rule.name),
                        subtitle: Text(
                          '${rule.triggerType.replaceAll('_', ' ')} · '
                          '${rule.actions.map((item) => item.type.replaceAll('_', ' ')).join(', ')}',
                        ),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Switch(
                              value: rule.enabled,
                              onChanged: _busy
                                  ? null
                                  : (value) => _toggle(rule, value),
                            ),
                            PopupMenuButton<String>(
                              enabled: !_busy,
                              onSelected: (value) =>
                                  value == 'edit' ? _edit(rule) : _delete(rule),
                              itemBuilder: (_) => [
                                PopupMenuItem(
                                  value: 'edit',
                                  child: Text('Edit rule'),
                                ),
                                PopupMenuItem(
                                  value: 'delete',
                                  child: Text(
                                    'Delete rule',
                                    style:
                                        TextStyle(color: context.kaede.danger),
                                  ),
                                ),
                              ],
                            ),
                          ],
                        ),
                      ),
                    ),
                ],
              ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: _busy ? null : _edit,
          icon: Icon(Icons.add_rounded),
          label: Text('AutoMod rule'),
        ),
      );
}

final class _AutoModRuleDialog extends StatefulWidget {
  const _AutoModRuleDialog({required this.guild, this.existing});

  final KaedeGuild guild;
  final AutoModRule? existing;

  @override
  State<_AutoModRuleDialog> createState() => _AutoModRuleDialogState();
}

final class _AutoModRuleDialogState extends State<_AutoModRuleDialog> {
  late final _name = TextEditingController(text: widget.existing?.name ?? '');
  late final _keywords = TextEditingController(
    text: widget.existing?.triggerMetadata.keywordFilter.join('\n') ?? '',
  );
  late final _regex = TextEditingController(
    text: widget.existing?.triggerMetadata.regexPatterns.join('\n') ?? '',
  );
  late final _allow = TextEditingController(
    text: widget.existing?.triggerMetadata.allowList.join('\n') ?? '',
  );
  late final _blockMessage = TextEditingController(
    text: widget.existing?.actions
            .where((item) => item.type == 'block_message')
            .firstOrNull
            ?.customMessage ??
        '',
  );
  late var _trigger = widget.existing?.triggerType ?? 'keyword';
  late var _enabled = widget.existing?.enabled ?? true;
  late var _mentionLimit =
      widget.existing?.triggerMetadata.mentionTotalLimit ?? 5;
  late var _mentionRaid =
      widget.existing?.triggerMetadata.mentionRaidProtectionEnabled ?? false;
  late final _presets = <String>{
    ...?widget.existing?.triggerMetadata.presets,
  };
  late var _block = widget.existing == null ||
      widget.existing!.actions.any((item) => item.type == 'block_message');
  late var _alert = widget.existing?.actions
          .any((item) => item.type == 'send_alert_message') ??
      false;
  late var _timeout =
      widget.existing?.actions.any((item) => item.type == 'timeout') ?? false;
  late var _blockInteraction = widget.existing?.actions
          .any((item) => item.type == 'block_member_interaction') ??
      false;
  late var _alertChannel = widget.existing?.actions
      .where((item) => item.type == 'send_alert_message')
      .firstOrNull
      ?.channelRef
      ?.wire;
  late var _timeoutSeconds = widget.existing?.actions
          .where((item) => item.type == 'timeout')
          .firstOrNull
          ?.durationSeconds ??
      600;
  late final _exemptRoles = <EntityRef>{...?widget.existing?.exemptRoles};
  late final _exemptChannels = <EntityRef>{
    ...?widget.existing?.exemptChannels,
  };
  String? _error;

  Iterable<KaedeChannel> get _alertChannels => widget.guild.channels.where(
        (channel) =>
            {ChannelType.text, ChannelType.announcement}
                .contains(channel.type) &&
            channel.encryptionMode != 'e2ee' &&
            !channel.e2eeRequired,
      );

  @override
  void dispose() {
    _name.dispose();
    _keywords.dispose();
    _regex.dispose();
    _allow.dispose();
    _blockMessage.dispose();
    super.dispose();
  }

  void _selectTrigger(String value) {
    setState(() {
      _trigger = value;
      if (value == 'member_profile') {
        _block = false;
        _timeout = false;
        _blockInteraction = true;
      } else {
        _blockInteraction = false;
        if (!_block && !_alert && !_timeout) _block = true;
      }
      if (!{'keyword', 'mention_spam'}.contains(value)) _timeout = false;
    });
  }

  AutoModRuleDraft _draft() {
    final actions = <AutoModAction>[
      if (_block)
        AutoModAction(
          type: 'block_message',
          customMessage: _blockMessage.text.trim().isEmpty
              ? null
              : _blockMessage.text.trim(),
        ),
      if (_alert)
        AutoModAction(
          type: 'send_alert_message',
          channelRef: _alertChannel == null
              ? null
              : EntityRef.parse(
                  _alertChannel!,
                  localDomain: widget.guild.ref.domain,
                ),
        ),
      if (_timeout)
        AutoModAction(type: 'timeout', durationSeconds: _timeoutSeconds),
      if (_blockInteraction) AutoModAction(type: 'block_member_interaction'),
    ];
    final metadata = switch (_trigger) {
      'keyword' || 'member_profile' => AutoModTriggerMetadata(
          keywordFilter: _lines(_keywords.text),
          regexPatterns: _lines(_regex.text),
          allowList: _lines(_allow.text),
        ),
      'keyword_preset' => AutoModTriggerMetadata(
          presets: _presets.toList(growable: false),
          allowList: _lines(_allow.text),
        ),
      'mention_spam' => AutoModTriggerMetadata(
          mentionTotalLimit: _mentionLimit,
          mentionRaidProtectionEnabled: _mentionRaid,
        ),
      _ => AutoModTriggerMetadata(),
    };
    return AutoModRuleDraft(
      name: _name.text.trim(),
      eventType:
          _trigger == 'member_profile' ? 'member_update' : 'message_send',
      triggerType: _trigger,
      triggerMetadata: metadata,
      actions: actions,
      enabled: _enabled,
      exemptRoles: _exemptRoles.toList(growable: false),
      exemptChannels: _exemptChannels.toList(growable: false),
    );
  }

  void _save() {
    final draft = _draft();
    final error = autoModDraftValidationMessage(draft);
    if (error != null) {
      setState(() => _error = error);
      return;
    }
    Navigator.pop(context, draft);
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(widget.existing == null
            ? 'Create AutoMod rule'
            : 'Edit AutoMod rule'),
        content: SizedBox(
          width: 680,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                TextField(
                  controller: _name,
                  maxLength: 100,
                  decoration: InputDecoration(labelText: 'Rule name'),
                ),
                DropdownButtonFormField<String>(
                  initialValue: _trigger,
                  decoration: InputDecoration(labelText: 'Trigger'),
                  items: const [
                    DropdownMenuItem(
                        value: 'keyword', child: Text('Keyword or regex')),
                    DropdownMenuItem(value: 'spam', child: Text('Spam')),
                    DropdownMenuItem(
                        value: 'keyword_preset', child: Text('Keyword preset')),
                    DropdownMenuItem(
                        value: 'mention_spam', child: Text('Mention spam')),
                    DropdownMenuItem(
                        value: 'member_profile', child: Text('Member profile')),
                  ],
                  onChanged: (value) {
                    if (value != null) _selectTrigger(value);
                  },
                ),
                if (_trigger == 'keyword' || _trigger == 'member_profile') ...[
                  SizedBox(height: 12),
                  TextField(
                    controller: _keywords,
                    minLines: 3,
                    maxLines: 6,
                    decoration: InputDecoration(
                      labelText: 'Keywords',
                      helperText: 'One per line. Use * as a wildcard.',
                      alignLabelWithHint: true,
                    ),
                  ),
                  SizedBox(height: 12),
                  TextField(
                    controller: _regex,
                    minLines: 2,
                    maxLines: 5,
                    decoration: InputDecoration(
                      labelText: 'Safe regular expressions (optional)',
                      helperText: 'One expression per line; up to 10.',
                      alignLabelWithHint: true,
                    ),
                  ),
                ],
                if (_trigger == 'keyword_preset') ...[
                  SizedBox(height: 12),
                  Text('Keyword presets',
                      style: TextStyle(fontWeight: FontWeight.w800)),
                  for (final preset in const [
                    'profanity',
                    'sexual_content',
                    'slurs'
                  ])
                    CheckboxListTile(
                      contentPadding: EdgeInsets.zero,
                      value: _presets.contains(preset),
                      title: Text(preset.replaceAll('_', ' ')),
                      onChanged: (value) => setState(() {
                        if (value == true) {
                          _presets.add(preset);
                        } else {
                          _presets.remove(preset);
                        }
                      }),
                    ),
                ],
                if (_trigger == 'keyword' ||
                    _trigger == 'member_profile' ||
                    _trigger == 'keyword_preset') ...[
                  SizedBox(height: 12),
                  TextField(
                    controller: _allow,
                    minLines: 2,
                    maxLines: 5,
                    decoration: InputDecoration(
                      labelText: 'Allowed terms (optional)',
                      helperText: 'One exception per line.',
                      alignLabelWithHint: true,
                    ),
                  ),
                ],
                if (_trigger == 'mention_spam') ...[
                  SizedBox(height: 12),
                  Text('Mention limit: $_mentionLimit'),
                  Slider(
                    value: _mentionLimit.toDouble(),
                    min: 1,
                    max: 50,
                    divisions: 49,
                    label: '$_mentionLimit',
                    onChanged: (value) =>
                        setState(() => _mentionLimit = value.round()),
                  ),
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _mentionRaid,
                    title: Text('Mention raid protection'),
                    subtitle: Text(
                        'Use account-wide burst signals in addition to the per-message limit.'),
                    onChanged: (value) => setState(() => _mentionRaid = value),
                  ),
                ],
                Divider(height: 28),
                Text('Actions', style: TextStyle(fontWeight: FontWeight.w900)),
                if (_trigger != 'member_profile')
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _block,
                    title: Text('Block message'),
                    onChanged: (value) => setState(() => _block = value),
                  ),
                if (_block)
                  TextField(
                    controller: _blockMessage,
                    maxLength: 150,
                    decoration: InputDecoration(
                      labelText: 'Message shown to the author (optional)',
                    ),
                  ),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: _alert,
                  title: Text('Send moderator alert'),
                  onChanged: (value) => setState(() => _alert = value),
                ),
                if (_alert)
                  DropdownButtonFormField<String>(
                    initialValue: _alertChannels
                            .any((item) => item.ref.wire == _alertChannel)
                        ? _alertChannel
                        : null,
                    decoration: InputDecoration(labelText: 'Alert channel'),
                    items: [
                      for (final channel in _alertChannels)
                        DropdownMenuItem(
                          value: channel.ref.wire,
                          child: Text('#${channel.name ?? 'channel'}'),
                        ),
                    ],
                    onChanged: (value) => setState(() => _alertChannel = value),
                  ),
                if ({'keyword', 'mention_spam'}.contains(_trigger)) ...[
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _timeout,
                    title: Text('Timeout member'),
                    onChanged: (value) => setState(() => _timeout = value),
                  ),
                  if (_timeout)
                    DropdownButtonFormField<int>(
                      initialValue: _timeoutSeconds,
                      decoration:
                          InputDecoration(labelText: 'Timeout duration'),
                      items: const [
                        DropdownMenuItem(value: 60, child: Text('1 minute')),
                        DropdownMenuItem(value: 600, child: Text('10 minutes')),
                        DropdownMenuItem(value: 3600, child: Text('1 hour')),
                        DropdownMenuItem(value: 86400, child: Text('1 day')),
                        DropdownMenuItem(value: 604800, child: Text('7 days')),
                        DropdownMenuItem(
                            value: 2419200, child: Text('28 days')),
                      ],
                      onChanged: (value) =>
                          setState(() => _timeoutSeconds = value ?? 600),
                    ),
                ],
                if (_trigger == 'member_profile')
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _blockInteraction,
                    title: Text('Block member interaction'),
                    subtitle: Text(
                        'Prevent the member from messaging or joining voice until their profile is changed.'),
                    onChanged: (value) =>
                        setState(() => _blockInteraction = value),
                  ),
                Divider(height: 28),
                _MultiRefPicker(
                  label: 'Exempt roles',
                  options: [
                    for (final role in widget.guild.roles)
                      (role.ref, role.name),
                  ],
                  selected: _exemptRoles,
                  maximum: 20,
                ),
                SizedBox(height: 12),
                _MultiRefPicker(
                  label: 'Exempt channels',
                  options: [
                    for (final channel in widget.guild.channels)
                      (channel.ref, '#${channel.name ?? 'channel'}'),
                  ],
                  selected: _exemptChannels,
                  maximum: 50,
                ),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: _enabled,
                  title: Text('Enable immediately'),
                  onChanged: (value) => setState(() => _enabled = value),
                ),
                if (_error != null)
                  Text(_error!, style: TextStyle(color: context.kaede.danger)),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('Cancel'),
          ),
          FilledButton(onPressed: _save, child: Text('Save rule')),
        ],
      );
}

final class _MultiRefPicker extends StatefulWidget {
  const _MultiRefPicker({
    required this.label,
    required this.options,
    required this.selected,
    required this.maximum,
    this.onChanged,
  });

  final String label;
  final List<(EntityRef, String)> options;
  final Set<EntityRef> selected;
  final int maximum;
  final VoidCallback? onChanged;

  @override
  State<_MultiRefPicker> createState() => _MultiRefPickerState();
}

final class _MultiRefPickerState extends State<_MultiRefPicker> {
  @override
  Widget build(BuildContext context) => ExpansionTile(
        tilePadding: EdgeInsets.zero,
        title: Text(widget.label),
        subtitle: Text(
          widget.selected.isEmpty
              ? 'None'
              : '${widget.selected.length} selected',
        ),
        children: [
          for (final option in widget.options)
            CheckboxListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              value: widget.selected.contains(option.$1),
              title: Text(option.$2),
              onChanged: !widget.selected.contains(option.$1) &&
                      widget.selected.length >= widget.maximum
                  ? null
                  : (value) {
                      var changed = false;
                      setState(() {
                        if (value == true) {
                          changed = widget.selected.add(option.$1);
                        } else {
                          changed = widget.selected.remove(option.$1);
                        }
                      });
                      if (changed) widget.onChanged?.call();
                    },
            ),
        ],
      );
}

final class GuildBulkModerationTab extends StatefulWidget {
  const GuildBulkModerationTab({
    required this.guild,
    required this.repository,
    required this.canPrune,
    required this.canBulkBan,
    super.key,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canPrune;
  final bool canBulkBan;

  @override
  State<GuildBulkModerationTab> createState() => _GuildBulkModerationTabState();
}

final class _GuildBulkModerationTabState extends State<GuildBulkModerationTab> {
  final _bulkUsers = TextEditingController();
  final _bulkReason = TextEditingController();
  final _pruneReason = TextEditingController();
  final _includeRoles = <EntityRef>{};
  var _days = 7;
  var _deleteMessageSeconds = 0;
  int? _estimate;
  PruneResult? _pruneResult;
  BulkBanResult? _bulkResult;
  var _busy = false;

  @override
  void dispose() {
    _bulkUsers.dispose();
    _bulkReason.dispose();
    _pruneReason.dispose();
    super.dispose();
  }

  Future<void> _estimatePrune() async {
    setState(() => _busy = true);
    try {
      final estimate = await widget.repository.estimatePrune(
        widget.guild.ref,
        days: _days,
        includeRoles: _includeRoles,
      );
      if (mounted) setState(() => _estimate = estimate);
    } on Object catch (error) {
      if (mounted) {
        _showError(context, 'Could not estimate inactive members.', error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _prune() async {
    final estimate = _estimate;
    if (estimate == null) {
      _showError(
        context,
        'Estimate inactive members first.',
        UserInputException('Run an estimate before pruning.'),
      );
      return;
    }
    if (!await _confirm(
      context,
      title: 'Prune $estimate inactive member${estimate == 1 ? '' : 's'}?',
      body:
          'Eligible members inactive for at least $_days days will be removed. '
          'Bots, owners, and members above your role are protected.',
      action: 'Prune members',
    )) {
      return;
    }
    setState(() => _busy = true);
    try {
      final result = await widget.repository.pruneMembers(
        widget.guild.ref,
        days: _days,
        includeRoles: _includeRoles,
        reason: _pruneReason.text,
      );
      if (!mounted) return;
      setState(() {
        _pruneResult = result;
        _estimate = null;
      });
    } on Object catch (error) {
      if (mounted) {
        _showError(context, 'Could not prune inactive members.', error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _bulkBan() async {
    late final List<EntityRef> users;
    try {
      users = parseCanonicalUserRefs(_bulkUsers.text, widget.guild.ref.domain);
    } on Object catch (error) {
      _showError(
        context,
        'Check the user references.',
        UserInputException(
          error is FormatException
              ? error.message.toString()
              : 'Enter one id@domain user reference per line.',
        ),
      );
      return;
    }
    if (!await _confirm(
      context,
      title: 'Ban ${users.length} user${users.length == 1 ? '' : 's'}?',
      body: 'Each user is checked independently against guild ownership and '
          'role hierarchy. Failures will be listed after the operation.',
      action: 'Ban users',
    )) {
      return;
    }
    setState(() => _busy = true);
    try {
      final result = await widget.repository.bulkBanMembers(
        widget.guild.ref,
        users,
        deleteMessageSeconds: _deleteMessageSeconds,
        reason: _bulkReason.text,
      );
      if (!mounted) return;
      setState(() => _bulkResult = result);
    } on Object catch (error) {
      if (mounted) {
        _showError(context, 'Could not complete the bulk ban.', error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: settingsSurface(context),
        body: ListView(
          padding: EdgeInsets.all(14),
          children: [
            const _AdminHint(
              'Bulk moderation keeps Discord-style per-user hierarchy checks. '
              'A partial failure never hides which members were skipped.',
            ),
            if (widget.canPrune)
              Card(
                child: Padding(
                  padding: EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text('Prune inactive members',
                          style: Theme.of(context).textTheme.titleLarge),
                      SizedBox(height: 8),
                      Text('Inactive for at least $_days days'),
                      Slider(
                        value: _days.toDouble(),
                        min: 1,
                        max: 30,
                        divisions: 29,
                        label: '$_days days',
                        onChanged: _busy
                            ? null
                            : (value) => setState(() {
                                  _days = value.round();
                                  _estimate = null;
                                }),
                      ),
                      _MultiRefPicker(
                        label: 'Include members with selected roles',
                        options: [
                          for (final role in widget.guild.roles)
                            if (role.ref != widget.guild.ref)
                              (role.ref, role.name),
                        ],
                        selected: _includeRoles,
                        maximum: 100,
                        onChanged: () => setState(() {
                          _estimate = null;
                          _pruneResult = null;
                        }),
                      ),
                      TextField(
                        controller: _pruneReason,
                        maxLength: 512,
                        decoration: InputDecoration(
                          labelText: 'Audit-log reason (optional)',
                        ),
                      ),
                      if (_estimate != null)
                        Text(
                          '$_estimate member${_estimate == 1 ? '' : 's'} eligible',
                          style: TextStyle(fontWeight: FontWeight.w800),
                        ),
                      if (_pruneResult != null)
                        _ModerationResult(
                          succeeded: _pruneResult!.prunedUserRefs.length,
                          successLabel: 'pruned',
                          failures: _pruneResult!.failures,
                        ),
                      SizedBox(height: 10),
                      Wrap(
                        alignment: WrapAlignment.end,
                        spacing: 8,
                        children: [
                          OutlinedButton(
                            onPressed: _busy ? null : _estimatePrune,
                            child: Text(_busy ? 'Checking…' : 'Estimate'),
                          ),
                          FilledButton(
                            style: FilledButton.styleFrom(
                              backgroundColor: context.kaede.danger,
                            ),
                            onPressed:
                                _busy || (_estimate ?? 0) == 0 ? null : _prune,
                            child: Text('Prune eligible members'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            if (widget.canBulkBan)
              Card(
                child: Padding(
                  padding: EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text('Bulk ban',
                          style: Theme.of(context).textTheme.titleLarge),
                      SizedBox(height: 8),
                      TextField(
                        controller: _bulkUsers,
                        minLines: 5,
                        maxLines: 10,
                        decoration: InputDecoration(
                          labelText: 'User references',
                          helperText: 'One id@domain per line; up to 200.',
                          hintText: '123456789@chat.example',
                          alignLabelWithHint: true,
                        ),
                      ),
                      SizedBox(height: 12),
                      DropdownButtonFormField<int>(
                        initialValue: _deleteMessageSeconds,
                        decoration: InputDecoration(
                            labelText: 'Delete recent messages'),
                        items: const [
                          DropdownMenuItem(
                              value: 0, child: Text('Do not delete')),
                          DropdownMenuItem(
                              value: 3600, child: Text('Previous hour')),
                          DropdownMenuItem(
                              value: 86400, child: Text('Previous day')),
                          DropdownMenuItem(
                              value: 604800, child: Text('Previous 7 days')),
                        ],
                        onChanged: (value) =>
                            setState(() => _deleteMessageSeconds = value ?? 0),
                      ),
                      SizedBox(height: 12),
                      TextField(
                        controller: _bulkReason,
                        maxLength: 512,
                        decoration:
                            InputDecoration(labelText: 'Reason (optional)'),
                      ),
                      if (_bulkResult != null)
                        _ModerationResult(
                          succeeded: _bulkResult!.bannedUserRefs.length,
                          successLabel: 'banned',
                          failures: _bulkResult!.failures,
                        ),
                      SizedBox(height: 8),
                      Align(
                        alignment: Alignment.centerRight,
                        child: FilledButton(
                          style: FilledButton.styleFrom(
                              backgroundColor: context.kaede.danger),
                          onPressed: _busy ? null : _bulkBan,
                          child: Text(_busy ? 'Working…' : 'Review and ban'),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
          ],
        ),
      );
}

final class _ModerationResult extends StatelessWidget {
  const _ModerationResult({
    required this.succeeded,
    required this.successLabel,
    required this.failures,
  });

  final int succeeded;
  final String successLabel;
  final List<ModerationFailure> failures;

  @override
  Widget build(BuildContext context) => Container(
        margin: EdgeInsets.only(top: 12),
        padding: EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(10),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('$succeeded $successLabel · ${failures.length} failed'),
            for (final failure in failures)
              Padding(
                padding: EdgeInsets.only(top: 5),
                child: Text(
                  '${failure.userRef.wire}: ${failure.message}',
                  style: TextStyle(color: context.kaede.danger),
                ),
              ),
          ],
        ),
      );
}

final class GuildSoundboardTab extends ConsumerStatefulWidget {
  const GuildSoundboardTab({
    required this.guild,
    required this.repository,
    required this.currentUserRef,
    required this.canCreate,
    required this.canManage,
    required this.canUse,
    super.key,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final EntityRef? currentUserRef;
  final bool canCreate;
  final bool canManage;
  final bool canUse;

  @override
  ConsumerState<GuildSoundboardTab> createState() => _GuildSoundboardTabState();
}

final class _GuildSoundboardTabState extends ConsumerState<GuildSoundboardTab> {
  List<SoundboardSound> _sounds = const [];
  List<Map<String, Object?>> _guildEmojis = const [];
  var _loading = true;
  var _busy = false;
  String? _emojiWarning;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant GuildSoundboardTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.currentUserRef != widget.currentUserRef ||
        oldWidget.canCreate != widget.canCreate ||
        oldWidget.canManage != widget.canManage) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final sounds = await widget.repository.soundboardSounds(widget.guild.ref);
      var guildEmojis = const <Map<String, Object?>>[];
      String? emojiWarning;
      if (widget.canCreate || widget.canManage) {
        try {
          guildEmojis = await widget.repository.guildEmojis(widget.guild.ref);
        } on Object {
          emojiWarning =
              'Custom emoji choices could not be loaded. Unicode emoji are still available.';
        }
      }
      if (!mounted) return;
      setState(() {
        _sounds = sounds;
        _guildEmojis = guildEmojis;
        _emojiWarning = emojiWarning;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      _showError(context, 'Could not load guild sounds.', error);
      setState(() => _loading = false);
    }
  }

  Future<void> _upload() async {
    final picked = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: const ['mp3', 'ogg', 'oga'],
      allowMultiple: false,
      withData: false,
    );
    final selected = picked?.files.singleOrNull;
    if (selected == null || !mounted) return;
    if (selected.path == null) {
      _showError(
        context,
        'Could not open the audio file.',
        UserInputException('Choose a file stored on this device.'),
      );
      return;
    }
    final contentType = soundboardContentType(selected.name);
    if (contentType == null) {
      _showError(
        context,
        'Could not upload the sound.',
        UserInputException('Choose an MP3 or Ogg audio file.'),
      );
      return;
    }
    final draft = await showSoundboardSoundEditor(
      context,
      title: 'Upload sound',
      action: 'Upload',
      initialName: selected.name.replaceFirst(RegExp(r'\.[^.]+$'), ''),
      guildEmojis: _guildEmojis,
      fallbackDomain: widget.guild.ref.domain,
    );
    if (draft == null || !mounted) return;
    setState(() => _busy = true);
    try {
      await widget.repository.uploadSoundboardSound(
        guild: widget.guild.ref,
        name: draft.name,
        filename: selected.name,
        contentType: contentType,
        file: File(selected.path!),
        volume: draft.volume,
        emojiRef: draft.emojiRef,
        emojiName: draft.emojiName,
      );
      await _load();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('“${draft.name}” is ready to play.')),
        );
      }
    } on Object catch (error) {
      if (mounted) _showError(context, 'Could not upload the sound.', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _edit(SoundboardSound sound) async {
    final draft = await showSoundboardSoundEditor(
      context,
      title: 'Edit sound',
      action: 'Save',
      initialName: sound.name,
      initialEmojiRef: sound.emojiRef,
      initialEmojiName: sound.emojiName ?? '',
      initialVolume: sound.volume,
      guildEmojis: _guildEmojis,
      fallbackDomain: widget.guild.ref.domain,
    );
    if (draft == null || !mounted) return;
    setState(() => _busy = true);
    try {
      await widget.repository.updateSoundboardSound(
        widget.guild.ref,
        sound.ref,
        <String, Object?>{
          'name': draft.name,
          'volume': draft.volume,
          'emoji_id': draft.emojiRef?.id.value,
          'emoji_name': draft.emojiRef == null && draft.emojiName.isNotEmpty
              ? draft.emojiName
              : null,
        },
      );
      await _load();
    } on Object catch (error) {
      if (mounted) _showError(context, 'Could not save the sound.', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _delete(SoundboardSound sound) async {
    if (!await _confirm(
      context,
      title: 'Delete “${sound.name}”?',
      body: 'The sound will immediately stop being available in this guild.',
      action: 'Delete sound',
    )) {
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.repository
          .deleteSoundboardSound(widget.guild.ref, sound.ref);
      await _load();
    } on Object catch (error) {
      if (mounted) _showError(context, 'Could not delete the sound.', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _play(SoundboardSound sound) async {
    final voice = ref.read(voiceSessionProvider);
    final channel = voice.channel;
    if (!voice.joined ||
        channel == null ||
        channel.guildRef != widget.guild.ref ||
        !channel.type.isVoiceLike) {
      _showError(
        context,
        'Join one of this guild’s voice channels first.',
        UserInputException(
          'Soundboard playback is sent to the voice channel currently connected on this device.',
        ),
      );
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.repository.playSoundboardSound(
        channel.ref,
        sound.ref,
        sound.guildRef ?? widget.guild.ref,
        soundVersion: sound.version,
      );
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
              content: Text(
                  'Playing “${sound.name}” in ${channel.name ?? 'voice'}.')),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        _showError(context, 'Could not play the sound in voice.', error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final voice = ref.watch(voiceSessionProvider);
    final connected =
        voice.joined && voice.channel?.guildRef == widget.guild.ref
            ? voice.channel
            : null;
    return Scaffold(
      backgroundColor: settingsSurface(context),
      body: _loading
          ? Center(child: CircularProgressIndicator())
          : ListView(
              padding: EdgeInsets.fromLTRB(14, 12, 14, 90),
              children: [
                _AdminHint(
                  connected == null
                      ? 'Join a voice channel in this guild to play sounds. Clips are limited to 512 KiB and about five seconds.'
                      : 'Playback will be sent to ${connected.name ?? 'the connected voice channel'}.',
                ),
                if (_emojiWarning case final warning?)
                  Padding(
                    padding: EdgeInsets.only(top: 8),
                    child: Text(
                      warning,
                      style: TextStyle(
                        color: context.kaede.warning,
                        fontSize: 12.5,
                      ),
                    ),
                  ),
                if (_sounds.isEmpty)
                  ListTile(
                    leading: Icon(Icons.music_note_outlined),
                    title: Text('No soundboard clips'),
                    subtitle: Text('Upload an MP3 or Ogg clip to get started.'),
                  ),
                for (final sound in _sounds)
                  Card(
                    child: ListTile(
                      leading: _SoundboardEmoji(sound: sound),
                      title: Text(sound.name),
                      subtitle: Text(
                        '${(sound.durationMilliseconds / 1000).toStringAsFixed(1)} seconds · '
                        '${(sound.volume * 100).round()}% default volume · '
                        '${sound.available ? 'available' : 'unavailable'}',
                      ),
                      trailing: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (widget.canUse)
                            IconButton(
                              tooltip: connected == null
                                  ? 'Join voice to play'
                                  : 'Play in ${connected.name ?? 'voice'}',
                              onPressed:
                                  _busy || !sound.available || connected == null
                                      ? null
                                      : () => _play(sound),
                              icon: Icon(Icons.play_arrow_rounded),
                            ),
                          if (canModifyGuildExpression(
                            creatorRef: sound.creatorRef,
                            currentUserRef: widget.currentUserRef,
                            canCreate: widget.canCreate,
                            canManage: widget.canManage,
                          ))
                            PopupMenuButton<String>(
                              key: ValueKey(
                                'soundboard-actions-${sound.ref.wire}',
                              ),
                              enabled: !_busy,
                              onSelected: (value) => value == 'edit'
                                  ? _edit(sound)
                                  : _delete(sound),
                              itemBuilder: (_) => [
                                const PopupMenuItem(
                                    value: 'edit', child: Text('Edit sound')),
                                PopupMenuItem(
                                  value: 'delete',
                                  child: Text('Delete sound',
                                      style: TextStyle(
                                          color: context.kaede.danger)),
                                ),
                              ],
                            ),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
      floatingActionButton: widget.canCreate
          ? FloatingActionButton.extended(
              onPressed: _busy || _sounds.length >= 48 ? null : _upload,
              icon: Icon(Icons.upload_file_rounded),
              label: Text(_busy ? 'Processing…' : 'Upload sound'),
            )
          : null,
    );
  }
}

typedef SoundboardSoundDraft = ({
  String name,
  EntityRef? emojiRef,
  String emojiName,
  double volume,
});

Future<SoundboardSoundDraft?> showSoundboardSoundEditor(
  BuildContext context, {
  required String title,
  required String action,
  required String initialName,
  required List<Map<String, Object?>> guildEmojis,
  required Domain fallbackDomain,
  EntityRef? initialEmojiRef,
  String initialEmojiName = '',
  double initialVolume = 1,
}) =>
    showDialog<SoundboardSoundDraft>(
      context: context,
      builder: (_) => _SoundDialog(
        title: title,
        action: action,
        initialName: initialName,
        guildEmojis: guildEmojis,
        fallbackDomain: fallbackDomain,
        initialEmojiRef: initialEmojiRef,
        initialEmojiName: initialEmojiName,
        initialVolume: initialVolume,
      ),
    );

final class _SoundDialog extends StatefulWidget {
  const _SoundDialog({
    required this.title,
    required this.action,
    required this.initialName,
    required this.guildEmojis,
    required this.fallbackDomain,
    this.initialEmojiRef,
    this.initialEmojiName = '',
    this.initialVolume = 1,
  });

  final String title;
  final String action;
  final String initialName;
  final List<Map<String, Object?>> guildEmojis;
  final Domain fallbackDomain;
  final EntityRef? initialEmojiRef;
  final String initialEmojiName;
  final double initialVolume;

  @override
  State<_SoundDialog> createState() => _SoundDialogState();
}

final class _SoundDialogState extends State<_SoundDialog> {
  late final _name = TextEditingController(text: widget.initialName);
  late final _emojiName = TextEditingController(text: widget.initialEmojiName);
  late String _emojiSelection = widget.initialEmojiRef != null
      ? 'custom:${widget.initialEmojiRef!.wire}'
      : widget.initialEmojiName.trim().isNotEmpty
          ? 'unicode'
          : 'none';
  late var _volume = widget.initialVolume;
  String? _emojiError;

  List<({EntityRef ref, String name})> get _customEmojis {
    final result = <({EntityRef ref, String name})>[];
    for (final emoji in widget.guildEmojis) {
      if (emoji['available'] == false || emoji['id'] == null) continue;
      try {
        final ref = EntityRef(
          Snowflake('${emoji['id']}'),
          Domain('${emoji['origin_domain'] ?? widget.fallbackDomain.value}'),
        );
        result.add((ref: ref, name: '${emoji['name'] ?? 'emoji'}'));
      } on FormatException {
        // Ignore stale or malformed choices instead of submitting a bad ID.
      }
    }
    if (widget.initialEmojiRef case final current?
        when !result.any((item) => item.ref == current)) {
      result.add((ref: current, name: 'current custom emoji'));
    }
    return result;
  }

  @override
  void dispose() {
    _name.dispose();
    _emojiName.dispose();
    super.dispose();
  }

  void _save() {
    final name = _name.text.trim();
    if (name.length < 2) return;
    EntityRef? emojiRef;
    var emojiName = '';
    if (_emojiSelection == 'unicode') {
      emojiName = _emojiName.text.trim();
      if (emojiName.isEmpty || emojiName.length > 64) {
        setState(() {
          _emojiError = 'Enter a Unicode emoji of at most 64 characters.';
        });
        return;
      }
    } else if (_emojiSelection.startsWith('custom:')) {
      final wire = _emojiSelection.substring('custom:'.length);
      emojiRef =
          _customEmojis.where((item) => item.ref.wire == wire).firstOrNull?.ref;
      if (emojiRef == null) {
        setState(() => _emojiError = 'Choose an available guild emoji.');
        return;
      }
    }
    Navigator.pop(
      context,
      (
        name: name,
        emojiRef: emojiRef,
        emojiName: emojiName,
        volume: _volume,
      ),
    );
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(widget.title),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: _name,
              maxLength: 32,
              decoration: InputDecoration(
                labelText: 'Name',
                helperText: '2–32 characters',
              ),
            ),
            DropdownButtonFormField<String>(
              key: Key('soundboard-emoji-source'),
              initialValue: _emojiSelection,
              decoration: InputDecoration(labelText: 'Display emoji'),
              items: [
                DropdownMenuItem(
                  value: 'none',
                  child: Text('No emoji'),
                ),
                DropdownMenuItem(
                  value: 'unicode',
                  child: Text('Unicode emoji'),
                ),
                for (final emoji in _customEmojis)
                  DropdownMenuItem(
                    value: 'custom:${emoji.ref.wire}',
                    child: Text(':${emoji.name}: · custom'),
                  ),
              ],
              onChanged: (value) {
                if (value != null) {
                  setState(() {
                    _emojiSelection = value;
                    _emojiError = null;
                  });
                }
              },
            ),
            if (_emojiSelection == 'unicode')
              TextField(
                key: Key('soundboard-unicode-emoji'),
                controller: _emojiName,
                maxLength: 64,
                decoration: InputDecoration(
                  labelText: 'Unicode emoji',
                  hintText: '🎉',
                ),
              ),
            if (_emojiError case final error?)
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  error,
                  style: TextStyle(color: context.kaede.danger),
                ),
              ),
            SizedBox(height: 8),
            Text('Default volume: ${(_volume * 100).round()}%'),
            Slider(
              value: _volume,
              min: 0,
              max: 1,
              divisions: 20,
              onChanged: (value) => setState(() => _volume = value),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('Cancel'),
          ),
          ValueListenableBuilder<TextEditingValue>(
            valueListenable: _name,
            builder: (_, value, __) => FilledButton(
              onPressed: value.text.trim().length < 2 ? null : _save,
              child: Text(widget.action),
            ),
          ),
        ],
      );
}

final class _SoundboardEmoji extends StatelessWidget {
  const _SoundboardEmoji({required this.sound});

  final SoundboardSound sound;

  @override
  Widget build(BuildContext context) {
    final emoji = sound.emojiRef;
    if (emoji == null) {
      return CircleAvatar(child: Text(sound.displayEmoji));
    }
    return CircleAvatar(
      backgroundColor: context.kaede.raised,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(15),
        child: CachedNetworkImage(
          imageUrl: Uri.https(
            emoji.domain.value,
            '/media/emojis/${emoji.id.value}/thumbnail_128',
          ).toString(),
          width: 30,
          height: 30,
          fit: BoxFit.contain,
          errorWidget: (_, __, ___) => Icon(
            Icons.emoji_emotions_outlined,
            size: 18,
          ),
        ),
      ),
    );
  }
}
