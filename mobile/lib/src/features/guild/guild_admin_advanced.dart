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
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
            child: Text(L10n.of(context).ui_cancel_35afca3b),
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
      _showError(context,
          L10n.of(context).ui_could_not_load_automod_rules_71b7a617, error);
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
                ? L10n.of(context).ui_automod_rule_created_eb47fd32
                : L10n.of(context).ui_automod_rule_saved_703a3fb7),
          ),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        _showError(
            context,
            L10n.of(context).ui_could_not_save_the_automod_rule_ff27bd9e,
            error);
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
        _showError(
            context,
            L10n.of(context).ui_could_not_update_the_automod_rule_0c714e2a,
            error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _delete(AutoModRule rule) async {
    if (!await _confirm(
      context,
      title: L10n.of(context).ui_delete_value0_2d1b16fb((rule.name).toString()),
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
        _showError(
            context,
            L10n.of(context).ui_could_not_delete_the_automod_rule_82d07320,
            error);
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
                      title:
                          Text(L10n.of(context).ui_no_automod_rules_7c6bb928),
                      subtitle: Text(L10n.of(context)
                          .ui_create_a_rule_to_start_filtering_activity_9fda4f8a),
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
                          L10n.of(context).ui_value0_value1_947523d9(
                              (rule.triggerType.replaceAll('_', ' '))
                                  .toString(),
                              (rule.actions
                                      .map((item) =>
                                          item.type.replaceAll('_', ' '))
                                      .join(', '))
                                  .toString()),
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
                                  child: Text(
                                      L10n.of(context).ui_edit_rule_f6a4a6bd),
                                ),
                                PopupMenuItem(
                                  value: 'delete',
                                  child: Text(
                                    L10n.of(context).ui_delete_rule_77459648,
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
          label: Text(L10n.of(context).ui_automod_rule_da11eec8),
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
            ? L10n.of(context).ui_create_automod_rule_494814d6
            : L10n.of(context).ui_edit_automod_rule_4f7ef0ba),
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
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_rule_name_b052ee02),
                ),
                DropdownButtonFormField<String>(
                  initialValue: _trigger,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_trigger_a5ea0da3),
                  items: [
                    DropdownMenuItem(
                        value: 'keyword',
                        child: Text(
                            L10n.of(context).ui_keyword_or_regex_d4f3e4d2)),
                    DropdownMenuItem(
                        value: 'spam',
                        child: Text(L10n.of(context).ui_spam_a8a83328)),
                    DropdownMenuItem(
                        value: 'keyword_preset',
                        child:
                            Text(L10n.of(context).ui_keyword_preset_a5983093)),
                    DropdownMenuItem(
                        value: 'mention_spam',
                        child: Text(L10n.of(context).ui_mention_spam_5659ad2e)),
                    DropdownMenuItem(
                        value: 'member_profile',
                        child:
                            Text(L10n.of(context).ui_member_profile_0f72e78a)),
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
                      labelText: L10n.of(context).ui_keywords_f16d9775,
                      helperText: L10n.of(context)
                          .ui_one_per_line_use_as_a_wildcard_3c4f3998,
                      alignLabelWithHint: true,
                    ),
                  ),
                  SizedBox(height: 12),
                  TextField(
                    controller: _regex,
                    minLines: 2,
                    maxLines: 5,
                    decoration: InputDecoration(
                      labelText: L10n.of(context)
                          .ui_safe_regular_expressions_optional_a7825092,
                      helperText: L10n.of(context)
                          .ui_one_expression_per_line_up_to_10_e2a2be00,
                      alignLabelWithHint: true,
                    ),
                  ),
                ],
                if (_trigger == 'keyword_preset') ...[
                  SizedBox(height: 12),
                  Text(L10n.of(context).ui_keyword_presets_8e94f0a0,
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
                      labelText:
                          L10n.of(context).ui_allowed_terms_optional_eb570b67,
                      helperText:
                          L10n.of(context).ui_one_exception_per_line_98fb579d,
                      alignLabelWithHint: true,
                    ),
                  ),
                ],
                if (_trigger == 'mention_spam') ...[
                  SizedBox(height: 12),
                  Text(L10n.of(context).ui_mention_limit_value0_bf7b11a5(
                      (_mentionLimit).toString())),
                  Slider(
                    value: _mentionLimit.toDouble(),
                    min: 1,
                    max: 50,
                    divisions: 49,
                    label: L10n.of(context)
                        .ui_value0_26e9163c((_mentionLimit).toString()),
                    onChanged: (value) =>
                        setState(() => _mentionLimit = value.round()),
                  ),
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _mentionRaid,
                    title: Text(
                        L10n.of(context).ui_mention_raid_protection_8d156a02),
                    subtitle: Text(L10n.of(context)
                        .ui_use_account_wide_burst_signals_in_addition_to_3d4643d9),
                    onChanged: (value) => setState(() => _mentionRaid = value),
                  ),
                ],
                Divider(height: 28),
                Text(L10n.of(context).ui_actions_e65fb504,
                    style: TextStyle(fontWeight: FontWeight.w900)),
                if (_trigger != 'member_profile')
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _block,
                    title: Text(L10n.of(context).ui_block_message_f88e1fe5),
                    onChanged: (value) => setState(() => _block = value),
                  ),
                if (_block)
                  TextField(
                    controller: _blockMessage,
                    maxLength: 150,
                    decoration: InputDecoration(
                      labelText: L10n.of(context)
                          .ui_message_shown_to_the_author_optional_426a9d0b,
                    ),
                  ),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: _alert,
                  title:
                      Text(L10n.of(context).ui_send_moderator_alert_3e1f3952),
                  onChanged: (value) => setState(() => _alert = value),
                ),
                if (_alert)
                  DropdownButtonFormField<String>(
                    initialValue: _alertChannels
                            .any((item) => item.ref.wire == _alertChannel)
                        ? _alertChannel
                        : null,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_alert_channel_64af8af2),
                    items: [
                      for (final channel in _alertChannels)
                        DropdownMenuItem(
                          value: channel.ref.wire,
                          child: Text(L10n.of(context).ui_value0_ea2f080f(
                              (channel.name ?? 'channel').toString())),
                        ),
                    ],
                    onChanged: (value) => setState(() => _alertChannel = value),
                  ),
                if ({'keyword', 'mention_spam'}.contains(_trigger)) ...[
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _timeout,
                    title: Text(L10n.of(context).ui_timeout_member_f494c0ea),
                    onChanged: (value) => setState(() => _timeout = value),
                  ),
                  if (_timeout)
                    DropdownButtonFormField<int>(
                      initialValue: _timeoutSeconds,
                      decoration: InputDecoration(
                          labelText:
                              L10n.of(context).ui_timeout_duration_e54242e8),
                      items: [
                        DropdownMenuItem(
                            value: 60,
                            child: Text(L10n.of(context).ui_1_minute_983277c4)),
                        DropdownMenuItem(
                            value: 600,
                            child:
                                Text(L10n.of(context).ui_10_minutes_5da9f7ad)),
                        DropdownMenuItem(
                            value: 3600,
                            child: Text(L10n.of(context).ui_1_hour_0c43fb4a)),
                        DropdownMenuItem(
                            value: 86400,
                            child: Text(L10n.of(context).ui_1_day_db83588e)),
                        DropdownMenuItem(
                            value: 604800,
                            child: Text(L10n.of(context).ui_7_days_ba4b8279)),
                        DropdownMenuItem(
                            value: 2419200,
                            child: Text(L10n.of(context).ui_28_days_ddd98ada)),
                      ],
                      onChanged: (value) =>
                          setState(() => _timeoutSeconds = value ?? 600),
                    ),
                ],
                if (_trigger == 'member_profile')
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: _blockInteraction,
                    title: Text(
                        L10n.of(context).ui_block_member_interaction_1dbd61b8),
                    subtitle: Text(L10n.of(context)
                        .ui_prevent_the_member_from_messaging_or_joining__c6d31f30),
                    onChanged: (value) =>
                        setState(() => _blockInteraction = value),
                  ),
                Divider(height: 28),
                _MultiRefPicker(
                  label: L10n.of(context).ui_exempt_roles_424fc5cf,
                  options: [
                    for (final role in widget.guild.roles)
                      (role.ref, role.name),
                  ],
                  selected: _exemptRoles,
                  maximum: 20,
                ),
                SizedBox(height: 12),
                _MultiRefPicker(
                  label: L10n.of(context).ui_exempt_channels_7046844e,
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
                  title: Text(L10n.of(context).ui_enable_immediately_98b65cb4),
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
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
              onPressed: _save,
              child: Text(L10n.of(context).ui_save_rule_0e059e66)),
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
              ? L10n.of(context).ui_none_304ff7fb
              : L10n.of(context).ui_value0_selected_9b815dfd(
                  (widget.selected.length).toString()),
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
        _showError(
            context,
            L10n.of(context).ui_could_not_estimate_inactive_members_a69596af,
            error);
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
        L10n.of(context).ui_estimate_inactive_members_first_779e1801,
        UserInputException('Run an estimate before pruning.'),
      );
      return;
    }
    if (!await _confirm(
      context,
      title: L10n.of(context).ui_prune_value0_inactive_member_value1_4ef74ea2(
          (estimate).toString(), (estimate == 1 ? '' : 's').toString()),
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
        _showError(
            context,
            L10n.of(context).ui_could_not_prune_inactive_members_8003176f,
            error);
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
        L10n.of(context).ui_check_the_user_references_02a97e8f,
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
      title: L10n.of(context).ui_ban_value0_user_value1_3e8a7edf(
          (users.length).toString(), (users.length == 1 ? '' : 's').toString()),
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
        _showError(
            context,
            L10n.of(context).ui_could_not_complete_the_bulk_ban_191bca08,
            error);
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
                      Text(L10n.of(context).ui_prune_inactive_members_fcdae99b,
                          style: Theme.of(context).textTheme.titleLarge),
                      SizedBox(height: 8),
                      Text(L10n.of(context)
                          .ui_inactive_for_at_least_value0_days_cb2a820d(
                              (_days).toString())),
                      Slider(
                        value: _days.toDouble(),
                        min: 1,
                        max: 30,
                        divisions: 29,
                        label: L10n.of(context)
                            .ui_value0_days_e87125e7((_days).toString()),
                        onChanged: _busy
                            ? null
                            : (value) => setState(() {
                                  _days = value.round();
                                  _estimate = null;
                                }),
                      ),
                      _MultiRefPicker(
                        label: L10n.of(context)
                            .ui_include_members_with_selected_roles_cf186bbc,
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
                          labelText: L10n.of(context)
                              .ui_audit_log_reason_optional_76d5ed0c,
                        ),
                      ),
                      if (_estimate != null)
                        Text(
                          L10n.of(context)
                              .ui_value0_member_value1_eligible_f231a11f(
                                  (_estimate).toString(),
                                  (_estimate == 1 ? '' : 's').toString()),
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
                            child: Text(_busy
                                ? L10n.of(context).ui_checking_87f62cb7
                                : L10n.of(context).ui_estimate_2649bd57),
                          ),
                          FilledButton(
                            style: FilledButton.styleFrom(
                              backgroundColor: context.kaede.danger,
                            ),
                            onPressed:
                                _busy || (_estimate ?? 0) == 0 ? null : _prune,
                            child: Text(L10n.of(context)
                                .ui_prune_eligible_members_97dfe373),
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
                      Text(L10n.of(context).ui_bulk_ban_44425fac,
                          style: Theme.of(context).textTheme.titleLarge),
                      SizedBox(height: 8),
                      TextField(
                        controller: _bulkUsers,
                        minLines: 5,
                        maxLines: 10,
                        decoration: InputDecoration(
                          labelText:
                              L10n.of(context).ui_user_references_56c6a9f0,
                          helperText: L10n.of(context)
                              .ui_one_id_domain_per_line_up_to_200_7ddcd74e,
                          hintText: L10n.of(context)
                              .ui_123456789_chat_example_4e1d684a,
                          alignLabelWithHint: true,
                        ),
                      ),
                      SizedBox(height: 12),
                      DropdownButtonFormField<int>(
                        initialValue: _deleteMessageSeconds,
                        decoration: InputDecoration(
                            labelText: L10n.of(context)
                                .ui_delete_recent_messages_c26243d7),
                        items: [
                          DropdownMenuItem(
                              value: 0,
                              child: Text(
                                  L10n.of(context).ui_do_not_delete_a97967f4)),
                          DropdownMenuItem(
                              value: 3600,
                              child: Text(
                                  L10n.of(context).ui_previous_hour_3c77cb2e)),
                          DropdownMenuItem(
                              value: 86400,
                              child: Text(
                                  L10n.of(context).ui_previous_day_dfd421a2)),
                          DropdownMenuItem(
                              value: 604800,
                              child: Text(L10n.of(context)
                                  .ui_previous_7_days_ee76f450)),
                        ],
                        onChanged: (value) =>
                            setState(() => _deleteMessageSeconds = value ?? 0),
                      ),
                      SizedBox(height: 12),
                      TextField(
                        controller: _bulkReason,
                        maxLength: 512,
                        decoration: InputDecoration(
                            labelText:
                                L10n.of(context).ui_reason_optional_8e7414f4),
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
                          child: Text(_busy
                              ? L10n.of(context).ui_working_d5507854
                              : L10n.of(context).ui_review_and_ban_79a5b82d),
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
            Text(L10n.of(context).ui_value0_value1_value2_failed_284995e7(
                (succeeded).toString(),
                (successLabel).toString(),
                (failures.length).toString())),
            for (final failure in failures)
              Padding(
                padding: EdgeInsets.only(top: 5),
                child: Text(
                  L10n.of(context).ui_value0_value1_cf87e38e(
                      (failure.userRef.wire).toString(),
                      (failure.message).toString()),
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
          emojiWarning = L10n.current
              .ui_custom_emoji_choices_could_not_be_loaded_unic_fee95380;
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
      _showError(context,
          L10n.of(context).ui_could_not_load_guild_sounds_0e4c3380, error);
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
        L10n.of(context).ui_could_not_open_the_audio_file_841fdedc,
        UserInputException('Choose a file stored on this device.'),
      );
      return;
    }
    final contentType = soundboardContentType(selected.name);
    if (contentType == null) {
      _showError(
        context,
        L10n.of(context).ui_could_not_upload_the_sound_165d6dae,
        UserInputException('Choose an MP3 or Ogg audio file.'),
      );
      return;
    }
    final draft = await showSoundboardSoundEditor(
      context,
      title: L10n.of(context).ui_upload_sound_8385b2ab,
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
          SnackBar(
              content: Text(L10n.of(context)
                  .ui_value0_is_ready_to_play_8faa88cd(
                      (draft.name).toString()))),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        _showError(context,
            L10n.of(context).ui_could_not_upload_the_sound_165d6dae, error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _edit(SoundboardSound sound) async {
    final draft = await showSoundboardSoundEditor(
      context,
      title: L10n.of(context).ui_edit_sound_1fb48f02,
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
      if (mounted) {
        _showError(context,
            L10n.of(context).ui_could_not_save_the_sound_2b04bdba, error);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _delete(SoundboardSound sound) async {
    if (!await _confirm(
      context,
      title:
          L10n.of(context).ui_delete_value0_2d1b16fb((sound.name).toString()),
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
      if (mounted) {
        _showError(context,
            L10n.of(context).ui_could_not_delete_the_sound_87663458, error);
      }
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
        L10n.of(context)
            .ui_join_one_of_this_guild_s_voice_channels_first_fd77f301,
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
              content: Text(L10n.of(context)
                  .ui_playing_value0_in_value1_e274c3f4((sound.name).toString(),
                      (channel.name ?? 'voice').toString()))),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        _showError(
            context,
            L10n.of(context).ui_could_not_play_the_sound_in_voice_c1e02436,
            error);
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
                    title:
                        Text(L10n.of(context).ui_no_soundboard_clips_2440507c),
                    subtitle: Text(L10n.of(context)
                        .ui_upload_an_mp3_or_ogg_clip_to_get_started_289b9d57),
                  ),
                for (final sound in _sounds)
                  Card(
                    child: ListTile(
                      leading: _SoundboardEmoji(sound: sound),
                      title: Text(sound.name),
                      subtitle: Text(
                        L10n.of(context)
                            .ui_value0_seconds_value1_default_volume_value2_20c853b6(
                                ((sound.durationMilliseconds / 1000)
                                        .toStringAsFixed(1))
                                    .toString(),
                                ((sound.volume * 100).round()).toString(),
                                (sound.available ? 'available' : 'unavailable')
                                    .toString()),
                      ),
                      trailing: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (widget.canUse)
                            IconButton(
                              tooltip: connected == null
                                  ? L10n.of(context)
                                      .ui_join_voice_to_play_83f9c06e
                                  : L10n.of(context).ui_play_in_value0_b2fa7bf3(
                                      (connected.name ?? 'voice').toString()),
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
                                PopupMenuItem(
                                    value: 'edit',
                                    child: Text(L10n.of(context)
                                        .ui_edit_sound_1fb48f02)),
                                PopupMenuItem(
                                  value: 'delete',
                                  child: Text(
                                      L10n.of(context).ui_delete_sound_35292f7d,
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
              label: Text(_busy
                  ? L10n.of(context).ui_processing_a38b68a6
                  : L10n.of(context).ui_upload_sound_8385b2ab),
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
          _emojiError = L10n.of(context)
              .ui_enter_a_unicode_emoji_of_at_most_64_character_954cd154;
        });
        return;
      }
    } else if (_emojiSelection.startsWith('custom:')) {
      final wire = _emojiSelection.substring('custom:'.length);
      emojiRef =
          _customEmojis.where((item) => item.ref.wire == wire).firstOrNull?.ref;
      if (emojiRef == null) {
        setState(() => _emojiError =
            L10n.of(context).ui_choose_an_available_guild_emoji_a097f4a5);
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
                labelText: L10n.of(context).ui_name_0fe07306,
                helperText: L10n.of(context).ui_2_32_characters_12a9229f,
              ),
            ),
            DropdownButtonFormField<String>(
              key: Key('soundboard-emoji-source'),
              initialValue: _emojiSelection,
              decoration: InputDecoration(
                  labelText: L10n.of(context).ui_display_emoji_685584d3),
              items: [
                DropdownMenuItem(
                  value: 'none',
                  child: Text(L10n.of(context).ui_no_emoji_e86dfba8),
                ),
                DropdownMenuItem(
                  value: 'unicode',
                  child: Text(L10n.of(context).ui_unicode_emoji_11f432fe),
                ),
                for (final emoji in _customEmojis)
                  DropdownMenuItem(
                    value: 'custom:${emoji.ref.wire}',
                    child: Text(L10n.of(context)
                        .ui_value0_custom_a1bc0988((emoji.name).toString())),
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
                  labelText: L10n.of(context).ui_unicode_emoji_11f432fe,
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
            Text(L10n.of(context).ui_default_volume_value0_402f0e1a(
                ((_volume * 100).round()).toString())),
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
            child: Text(L10n.of(context).ui_cancel_35afca3b),
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
