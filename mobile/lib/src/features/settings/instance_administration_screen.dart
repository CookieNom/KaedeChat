import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/instance_administration_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/user_identity.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/instance_administration.dart';
import 'package:kaede_mobile/src/features/settings/administration_attachment_viewer.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

enum _AdminSection {
  overview('Overview', 'admin.read'),
  users('Users', 'admin.read'),
  applications('Applications', 'admin.read'),
  reports('Reports', 'reports.read'),
  instances('Instance blocks', 'admin.read'),
  operators('Operators', 'admin.read'),
  audit('Audit log', 'audit.read');

  const _AdminSection(this.label, this.capability);
  final String label;
  final String capability;
}

/// Native, capability-gated instance administration and Trust & Safety.
/// Every mutation uses the audited human administration API.
final class InstanceAdministrationScreen extends ConsumerStatefulWidget {
  const InstanceAdministrationScreen({super.key, this.repository});

  final KaedeRepository? repository;

  @override
  ConsumerState<InstanceAdministrationScreen> createState() =>
      _InstanceAdministrationScreenState();
}

final class _InstanceAdministrationScreenState
    extends ConsumerState<InstanceAdministrationScreen> {
  AdministrationIdentity? _identity;
  Map<String, int> _overview = const {};
  List<AdministrationUser> _users = const [];
  List<AdministrationApplication> _applications = const [];
  List<AdministrationReport> _reports = const [];
  List<AdministrationInstanceBlock> _blocks = const [];
  List<AdministrationOperator> _operators = const [];
  List<AdministrationAuditEvent> _audit = const [];
  var _section = _AdminSection.overview;
  var _loading = true;
  String? _error;
  final _userSearch = TextEditingController();

  KaedeRepository get repository =>
      widget.repository ??
      ref.read(mobileControllerProvider.notifier).repository;
  bool can(String capability) => _identity?.can(capability) == true;
  List<_AdminSection> get sections => _AdminSection.values
      .where((section) => can(section.capability))
      .toList(growable: false);

  @override
  void initState() {
    super.initState();
    unawaited(_initialize());
  }

  @override
  void dispose() {
    _userSearch.dispose();
    super.dispose();
  }

  Future<void> _initialize() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final identity = await repository.administrationIdentity();
      if (!mounted) return;
      setState(() {
        _identity = identity;
        _section = _AdminSection.values.firstWhere(
          (section) => identity.can(section.capability),
          orElse: () => _AdminSection.overview,
        );
      });
      await _loadSection();
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(error,
            summary: L10n.of(context)
                .ui_administration_is_unavailable_for_this_accoun_8ced1d3f);
        _loading = false;
      });
    }
  }

  Future<void> _loadSection() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      switch (_section) {
        case _AdminSection.overview:
          _overview = await repository.administrationOverview();
        case _AdminSection.users:
          _users =
              await repository.administrationUsers(query: _userSearch.text);
        case _AdminSection.applications:
          _applications = await repository.administrationApplications();
        case _AdminSection.reports:
          _reports = await repository.administrationReports();
        case _AdminSection.instances:
          _blocks = await repository.administrationBlocks();
        case _AdminSection.operators:
          _operators = await repository.administrationOperators();
        case _AdminSection.audit:
          _audit = await repository.administrationAudit();
      }
      if (mounted) setState(() => _loading = false);
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(error,
            summary: L10n.of(context).ui_could_not_load_value0_786258fa(
                (_section.label.toLowerCase()).toString()));
        _loading = false;
      });
    }
  }

  Future<void> _selectSection(_AdminSection section) async {
    if (section == _section) return;
    setState(() {
      _section = section;
    });
    await _loadSection();
  }

  Future<void> _userActions(AdministrationUser user) async {
    final action = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ActionTile(
              title: Text(user.label),
              subtitle: Text(user.ref.wire),
            ),
            if (can('users.manage'))
              SettingsRow(
                leading: Icon(user.restricted
                    ? Icons.lock_open_outlined
                    : Icons.block_outlined),
                title: user.restricted
                    ? L10n.of(context).ui_restore_access_f5893039
                    : L10n.of(context).ui_ban_account_ed1ab91d,
                danger: !user.restricted,
                onTap: () => Navigator.pop(context, 'access'),
              ),
            if (can('users.manage') && user.accountType == 'human')
              for (final state in const ['unknown', 'adult', 'minor'])
                ActionTile(
                  leading: Icon(user.ageAssuranceState == state
                      ? Icons.radio_button_checked
                      : Icons.radio_button_unchecked),
                  title: Text(L10n.of(context)
                      .ui_age_assurance_value0_25fb9d7a((state).toString())),
                  onTap: user.ageAssuranceState == state
                      ? null
                      : () => Navigator.pop(context, 'age:$state'),
                ),
          ],
        ),
      ),
    );
    if (action == null) return;
    try {
      if (action == 'access') {
        final verb = user.restricted ? 'restore' : 'ban';
        if (!await _confirm('Really $verb ${user.label}?')) return;
        await repository.updateAdministrationUser(
          user.ref,
          <String, Object?>{'disabled': !user.restricted, 'reason': null},
        );
        if (!mounted) return;
        showActionFeedback(
            context,
            L10n.current.ui_value0_access_was_updated_607c5a7e(
                (user.label).toString()));
      } else if (action.startsWith('age:')) {
        final state = action.substring(4);
        if (!await _confirm('Set ${user.label} age assurance to $state?')) {
          return;
        }
        await repository.updateAdministrationUser(
          user.ref,
          <String, Object?>{'age_assurance_state': state, 'reason': null},
        );
        if (!mounted) return;
        showActionFeedback(
            context,
            L10n.current.ui_value0_age_assurance_is_now_value1_415c4a90(
                (user.label).toString(), (state).toString()));
      }
      await _loadSection();
    } on Object catch (error) {
      _setError(error, 'Could not update the account');
    }
  }

  Future<void> _applicationActions(
    AdministrationApplication application,
  ) async {
    if (!can('bots.manage') || !application.canManageState) return;
    final next = application.status == 'suspended' ? 'active' : 'suspended';
    if (!await _confirm(
        '${next == 'active' ? 'Activate' : 'Suspend'} ${application.name}?')) {
      return;
    }
    try {
      await repository.updateAdministrationApplication(
        application.ref,
        status: next,
      );
      if (!mounted) return;
      showActionFeedback(
          context,
          L10n.current.ui_value0_is_now_value1_f1974346(
              (application.name).toString(), (next).toString()));
      await _loadSection();
    } on Object catch (error) {
      _setError(error, 'Could not update the application');
    }
  }

  Future<void> _reportActions(AdministrationReport report) async {
    final resolution = TextEditingController(text: report.resolution ?? '');
    final reason = TextEditingController();
    var status = report.status;
    var accountAction = 'none';
    var messageAction = 'none';
    try {
      final action = await showModalBottomSheet<String>(
        context: context,
        isScrollControlled: true,
        showDragHandle: true,
        builder: (sheetContext) => StatefulBuilder(
          builder: (context, setSheetState) => SafeArea(
            child: Padding(
              padding: EdgeInsets.fromLTRB(
                16,
                0,
                16,
                MediaQuery.viewInsetsOf(context).bottom + 16,
              ),
              child: SingleChildScrollView(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text(
                        L10n.of(context)
                            .ui_report_value0_f2ff09eb((report.id).toString()),
                        style: Theme.of(context).textTheme.titleLarge),
                    const SizedBox(height: 6),
                    Text(L10n.of(context).ui_value0_value1_947523d9(
                        (report.category).toString(),
                        (report.targetType).toString())),
                    if (report.description case final description?) ...[
                      const SizedBox(height: 8),
                      Text(description),
                    ],
                    const SizedBox(height: 12),
                    Text(L10n.of(context).ui_evidence_f800be44,
                        style: Theme.of(context).textTheme.titleMedium),
                    if (report.attachments.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      for (final attachment in report.attachments)
                        _ReportAttachmentRow(
                          report: report,
                          attachment: attachment,
                          localDomain: _identity!.userRef.domain,
                          onOpen: () => Navigator.of(context).push<void>(
                            MaterialPageRoute<void>(
                              builder: (_) => AdministrationAttachmentViewer(
                                repository: repository,
                                report: report,
                                attachment: attachment,
                              ),
                            ),
                          ),
                        ),
                      const SizedBox(height: 10),
                    ],
                    for (final entry in <Map<String, Object?>>[
                      if (report.evidence['content'] is String)
                        {
                          ...report.evidence,
                          'reported': true,
                          'message_ref': report.targetRef
                        },
                      if (report.evidence['context_messages'] is List)
                        for (final item
                            in report.evidence['context_messages'] as List)
                          if (item is Map) Map<String, Object?>.from(item),
                    ]..sort((a, b) => '${a['created_at']}'
                                .compareTo('${b['created_at']}') !=
                            0
                        ? '${a['created_at']}'.compareTo('${b['created_at']}')
                        : '${a['message_ref']}'
                            .split('@')
                            .first
                            .padLeft(20, '0')
                            .compareTo('${b['message_ref']}'
                                .split('@')
                                .first
                                .padLeft(20, '0'))))
                      Container(
                        width: double.infinity,
                        margin: const EdgeInsets.only(top: 8),
                        padding: const EdgeInsets.all(16),
                        decoration: BoxDecoration(
                          color: entry['reported'] == true
                              ? Theme.of(context).colorScheme.errorContainer
                              : Theme.of(context).colorScheme.surfaceContainer,
                          borderRadius: BorderRadius.circular(12),
                          border: entry['reported'] == true
                              ? Border.all(
                                  color: Theme.of(context).colorScheme.error,
                                  width: 2)
                              : null,
                        ),
                        child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                  entry['reported'] == true
                                      ? 'Reported message'
                                      : 'Context message',
                                  style:
                                      Theme.of(context).textTheme.titleSmall),
                              const SizedBox(height: 4),
                              Text(
                                  '${entry['author_ref']} · ${entry['created_at']}',
                                  style: Theme.of(context).textTheme.bodySmall),
                              const SizedBox(height: 12),
                              SelectableText(
                                  '${entry['content'] ?? "No text / attachment message"}'),
                              if (entry['reported'] != true &&
                                  entry['attachments'] is List)
                                for (final attachment
                                    in entry['attachments'] as List)
                                  if (attachment is Map)
                                    Text(
                                        'Attachment: ${attachment['filename'] ?? "Encrypted attachment"}'),
                              if (entry['disclosure'] != null)
                                const Padding(
                                    padding: EdgeInsets.only(top: 8),
                                    child: Text(
                                        'Reporter-disclosed encrypted text · not verified by the server')),
                            ]),
                      ),
                    ExpansionTile(
                      tilePadding: EdgeInsets.zero,
                      title: const Text('Technical evidence'),
                      children: [
                        SelectableText(
                            const JsonEncoder.withIndent('  ')
                                .convert(report.evidence),
                            style: const TextStyle(fontFamily: 'monospace'))
                      ],
                    ),
                    const SizedBox(height: 16),
                    if (can('reports.manage')) ...[
                      DropdownButtonFormField<String>(
                        initialValue: status,
                        decoration: InputDecoration(
                            labelText: L10n.of(context).ui_status_005ef20f),
                        items: [
                          for (final value in _reportStatuses)
                            DropdownMenuItem(value: value, child: Text(value)),
                        ],
                        onChanged: (value) =>
                            setSheetState(() => status = value ?? status),
                      ),
                      TextField(
                        controller: resolution,
                        maxLength: 2000,
                        minLines: 2,
                        maxLines: 4,
                        decoration: InputDecoration(
                            labelText: L10n.of(context).ui_resolution_aee818af),
                      ),
                      ActionButton(
                        onPressed: () => Navigator.pop(sheetContext, 'update'),
                        child: Text(L10n.of(context).ui_update_report_8cfeddee),
                      ),
                      if (report.subjectRef != null) ...[
                        const Divider(height: 32),
                        Text(L10n.of(context).ui_enforcement_d891dff9,
                            style: Theme.of(context).textTheme.titleMedium),
                        DropdownButtonFormField<String>(
                          initialValue: accountAction,
                          decoration: InputDecoration(
                              labelText:
                                  L10n.of(context).ui_account_action_c48c1176),
                          items: [
                            for (final value in _accountActions)
                              DropdownMenuItem(
                                  value: value, child: Text(value)),
                          ],
                          onChanged: (value) => setSheetState(
                              () => accountAction = value ?? accountAction),
                        ),
                        DropdownButtonFormField<String>(
                          initialValue: messageAction,
                          decoration: InputDecoration(
                              labelText:
                                  L10n.of(context).ui_message_action_8395e6fe),
                          items: [
                            for (final value in _messageActions)
                              DropdownMenuItem(
                                  value: value, child: Text(value)),
                          ],
                          onChanged: (value) => setSheetState(
                              () => messageAction = value ?? messageAction),
                        ),
                        TextField(
                          controller: reason,
                          minLines: 2,
                          maxLines: 4,
                          maxLength: 500,
                          decoration: InputDecoration(
                              labelText:
                                  L10n.of(context).ui_audit_reason_e1feab54),
                        ),
                        ActionButton(
                          kind: ActionButtonKind.tonal,
                          onPressed: accountAction == 'none' &&
                                  messageAction == 'none'
                              ? null
                              : () => Navigator.pop(sheetContext, 'enforce'),
                          icon: const Icon(Icons.gavel_outlined),
                          label: Text(
                              L10n.of(context).ui_apply_enforcement_b557d875),
                        ),
                      ],
                    ],
                    const SizedBox(height: 8),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
      if (action == null) return;
      if (action == 'update') {
        await repository.updateAdministrationReport(
          reportId: report.id,
          status: status,
          resolution: resolution.text,
        );
        if (!mounted) return;
        showActionFeedback(
            context,
            L10n.current
                .ui_report_value0_was_updated_06afce71((report.id).toString()));
      } else {
        if (reason.text.trim().length < 3) {
          throw const UserInputException(
              'Enforcement requires an audit reason of at least 3 characters.');
        }
        if (!await _confirm(
            'Apply the selected enforcement to ${report.subjectRef}? Message deletion cannot be undone.')) {
          return;
        }
        await repository.enforceAdministrationReport(
          reportId: report.id,
          accountAction: accountAction,
          messageAction: messageAction,
          reason: reason.text,
        );
        if (!mounted) return;
        showActionFeedback(
            context,
            L10n.current.ui_enforcement_was_applied_to_report_value0_b86dd132(
                (report.id).toString()));
      }
      await _loadSection();
    } on Object catch (error) {
      _setError(error, 'Could not update the report');
    } finally {
      resolution.dispose();
      reason.dispose();
    }
  }

  Future<void> _addBlock() async {
    final domain = TextEditingController();
    final reason = TextEditingController();
    var level = 'suspend';
    var includeSubdomains = false;
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: Text(L10n.of(context).ui_federation_restriction_f38c3ae2),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: domain,
                    autofocus: true,
                    autocorrect: false,
                    decoration: InputDecoration(
                        labelText:
                            L10n.of(context).ui_instance_domain_f42d351a),
                  ),
                  DropdownButtonFormField<String>(
                    initialValue: level,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_level_4155597d),
                    items: [
                      DropdownMenuItem(
                          value: 'silence',
                          child: Text(L10n.of(context).ui_silence_76177b2c)),
                      DropdownMenuItem(
                          value: 'suspend',
                          child: Text(L10n.of(context).ui_suspend_e196ab03)),
                    ],
                    onChanged: (value) =>
                        setDialogState(() => level = value ?? level),
                  ),
                  CheckboxListTile(
                    contentPadding: EdgeInsets.zero,
                    value: includeSubdomains,
                    title:
                        Text(L10n.of(context).ui_include_subdomains_19bcf456),
                    onChanged: (value) => setDialogState(
                        () => includeSubdomains = value ?? false),
                  ),
                  TextField(
                    controller: reason,
                    maxLength: 500,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_audit_reason_e1feab54),
                  ),
                ],
              ),
            ),
            actions: [
              ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              ActionButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(L10n.of(context).ui_save_4d2d5d68),
              ),
            ],
          ),
        ),
      );
      if (accepted != true) return;
      await repository.putAdministrationBlock(
        domain: domain.text,
        level: level,
        includeSubdomains: includeSubdomains,
        reason: reason.text,
      );
      if (!mounted) return;
      showActionFeedback(
          context, L10n.current.ui_federation_policy_updated_8e141fc1);
      await _loadSection();
    } on Object catch (error) {
      _setError(error, 'Could not update federation policy');
    } finally {
      domain.dispose();
      reason.dispose();
    }
  }

  Future<void> _removeBlock(AdministrationInstanceBlock block) async {
    if (!await _confirm('Remove the restriction for ${block.domain}?')) return;
    try {
      await repository.deleteAdministrationBlock(block.domain);
      await _loadSection();
    } on Object catch (error) {
      _setError(error, 'Could not remove federation policy');
    }
  }

  Future<void> _addOperator() async {
    final user = TextEditingController();
    var role = 'administrator';
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: Text(L10n.of(context).ui_delegate_administration_b2c8dbd0),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: user,
                  autofocus: true,
                  decoration: InputDecoration(
                    labelText: L10n.of(context)
                        .ui_username_or_qualified_local_user_id_f8fc62ab,
                  ),
                ),
                DropdownButtonFormField<String>(
                  initialValue: role,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_role_902b7e39),
                  items: [
                    for (final value in _operatorRoles)
                      DropdownMenuItem(value: value, child: Text(value)),
                  ],
                  onChanged: (value) =>
                      setDialogState(() => role = value ?? role),
                ),
              ],
            ),
            actions: [
              ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              ActionButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(L10n.of(context).ui_grant_5710d90d),
              ),
            ],
          ),
        ),
      );
      if (accepted != true) return;
      await repository.addAdministrationOperator(
        user: await repository.resolveUserIdentity(user.text),
        role: role,
      );
      if (!mounted) return;
      showActionFeedback(
          context, L10n.current.ui_administrative_role_granted_f99615fc);
      await _loadSection();
    } on Object catch (error) {
      _setError(error, 'Could not grant the role');
    } finally {
      user.dispose();
    }
  }

  Future<void> _removeOperator(AdministrationOperator operator) async {
    if (operator.role == 'owner') return;
    if (!await _confirm('Revoke ${operator.role} from ${operator.username}?')) {
      return;
    }
    try {
      await repository.removeAdministrationOperator(operator.id);
      await _loadSection();
    } on Object catch (error) {
      _setError(error, 'Could not revoke the role');
    }
  }

  void _setError(Object error, String summary) {
    if (mounted) {
      setState(() => _error = userFacingError(error, summary: summary));
      showActionFeedback(context, _error!, error: true);
    }
  }

  Future<bool> _confirm(String message) => showSettingsConfirmation(
        context,
        title: L10n.of(context).ui_confirm_administrative_action_7085757f,
        message: message,
      );

  String _date(DateTime value) => DateFormat.yMd(
        Localizations.localeOf(context).toLanguageTag(),
      ).add_jm().format(value.toLocal());

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: Text(L10n.of(context).ui_administration_8fd91fef),
          actions: [
            ActionButton(
              kind: ActionButtonKind.icon,
              tooltip: L10n.of(context).ui_refresh_0815aad4,
              onPressed: _loading ? null : _loadSection,
              icon: const Icon(Icons.refresh_rounded),
            ),
          ],
        ),
        body: SafeArea(
          child: RefreshIndicator(
            onRefresh: _loadSection,
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 40),
              children: [
                if (_identity case final identity?) ...[
                  Text(
                    L10n.of(context).ui_signed_in_as_value0_value1_b1cc472c(
                        (identity.username).toString(),
                        (identity.roles.join(', ')).toString()),
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: 10),
                  DropdownButtonFormField<_AdminSection>(
                    initialValue: _section,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_section_2d877b6c),
                    items: [
                      for (final section in sections)
                        DropdownMenuItem(
                          value: section,
                          child: Text(section.label),
                        ),
                    ],
                    onChanged: (value) {
                      if (value != null) unawaited(_selectSection(value));
                    },
                  ),
                ],
                if (_error case final error?)
                  SettingsStatusPanel.error(
                    message: error,
                    onRetry: _identity == null ? _initialize : _loadSection,
                  ),
                if (_loading)
                  const Center(
                    child: Padding(
                      padding: EdgeInsets.all(36),
                      child: CircularProgressIndicator(),
                    ),
                  )
                else if (_identity != null)
                  ..._sectionWidgets(),
              ],
            ),
          ),
        ),
      );

  List<Widget> _sectionWidgets() => switch (_section) {
        _AdminSection.overview => _overviewWidgets(),
        _AdminSection.users => _userWidgets(),
        _AdminSection.applications => _applicationWidgets(),
        _AdminSection.reports => _reportWidgets(),
        _AdminSection.instances => _blockWidgets(),
        _AdminSection.operators => _operatorWidgets(),
        _AdminSection.audit => _auditWidgets(),
      };

  List<Widget> _overviewWidgets() => [
        SettingsSectionHeader(
          L10n.of(context).ui_instance_overview_f5d9d875,
          subheading: L10n.of(context)
              .ui_live_local_counts_and_trust_safety_workload_f_ad776ba5,
        ),
        GridView.count(
          crossAxisCount: 2,
          childAspectRatio: 1.55,
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          children: [
            for (final entry in _overview.entries)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Text(
                          L10n.of(context)
                              .ui_value0_26e9163c((entry.value).toString()),
                          style: Theme.of(context).textTheme.headlineMedium),
                      Text(entry.key.replaceAll('_', ' ')),
                    ],
                  ),
                ),
              ),
          ],
        ),
      ];

  List<Widget> _userWidgets() => [
        SettingsSectionHeader(L10n.of(context).ui_local_users_82c9b538),
        TextField(
          controller: _userSearch,
          textInputAction: TextInputAction.search,
          onSubmitted: (_) => _loadSection(),
          decoration: InputDecoration(
            labelText: L10n.of(context).ui_search_username_95ab926b,
            prefixIcon: const Icon(Icons.search_rounded),
            suffixIcon: ActionButton(
              kind: ActionButtonKind.icon,
              tooltip: L10n.of(context).ui_search_c646a2c9,
              onPressed: _loadSection,
              icon: const Icon(Icons.arrow_forward_rounded),
            ),
          ),
        ),
        for (final user in _users)
          ActionTile(
            leading: Icon(user.restricted
                ? Icons.person_off_outlined
                : Icons.person_outline_rounded),
            title: Text(user.label),
            subtitle: Text(
              L10n.of(context).ui_value0_value1_age_value2_fd663362(
                  (user.ref.wire).toString(),
                  (user.accountType).toString(),
                  (user.ageAssuranceState).toString()),
            ),
            trailing: can('users.manage')
                ? const Icon(Icons.more_horiz_rounded)
                : null,
            onTap: () => _userActions(user),
          ),
      ];

  List<Widget> _applicationWidgets() => [
        SettingsSectionHeader(
          L10n.of(context).ui_applications_2f7f7f22,
          subheading: L10n.of(context)
              .ui_suspend_unsafe_integrations_across_local_inst_c7890512,
        ),
        for (final application in _applications)
          ActionTile(
            leading: const Icon(Icons.smart_toy_outlined),
            title: Text(application.name),
            subtitle: Text(
              L10n.of(context).ui_value0_value1_value2_value3_d8f7a34d(
                  (application.ref.wire).toString(),
                  (application.status).toString(),
                  (_date(application.updatedAt)).toString(),
                  (application.canManageState
                          ? ''
                          : '\nState managed by ${application.stateAuthority.value}')
                      .toString()),
            ),
            trailing: can('bots.manage')
                ? ActionButton(
                    kind: ActionButtonKind.icon,
                    tooltip: !application.canManageState
                        ? L10n.of(context).ui_state_managed_by_value0_a952f851(
                            (application.stateAuthority.value).toString())
                        : application.status == 'suspended'
                            ? L10n.of(context).ui_activate_3b8f0190
                            : L10n.of(context).ui_suspend_e196ab03,
                    onPressed: application.canManageState
                        ? () => _applicationActions(application)
                        : null,
                    icon: Icon(application.status == 'suspended'
                        ? Icons.play_circle_outline
                        : Icons.pause_circle_outline),
                  )
                : null,
          ),
      ];

  List<Widget> _reportWidgets() => [
        SettingsSectionHeader(
          L10n.of(context).ui_trust_safety_reports_522da1b4,
          subheading: L10n.of(context)
              .ui_review_server_verified_evidence_and_apply_aud_122b0cad,
        ),
        for (final report in _reports)
          Card(
            child: ActionTile(
              leading: const Icon(Icons.flag_outlined),
              title: Text(L10n.of(context).ui_value0_value1_947523d9(
                  (report.category).toString(),
                  (report.targetType).toString())),
              subtitle: Text(
                L10n.of(context).ui_value0_value1_value2_value3_f9e0d55d(
                    (report.status).toString(),
                    (report.severity).toString(),
                    (_date(report.createdAt)).toString(),
                    (report.description ?? report.targetRef).toString()),
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
              ),
              isThreeLine: true,
              trailing: const Icon(Icons.chevron_right_rounded),
              onTap: () => _reportActions(report),
            ),
          ),
      ];

  List<Widget> _blockWidgets() => [
        SettingsSectionHeader(
          L10n.of(context).ui_federation_restrictions_f6c03243,
          subheading: L10n.of(context)
              .ui_silence_or_suspend_traffic_from_a_remote_inst_45798a43,
        ),
        for (final block in _blocks)
          ActionTile(
            leading: const Icon(Icons.public_off_outlined),
            title: Text(block.domain),
            subtitle: Text(
              L10n.of(context).ui_value0_value1_value2_d1940c97(
                  (block.level).toString(),
                  (block.includeSubdomains ? ' · includes subdomains' : '')
                      .toString(),
                  (block.reason == null ? '' : '\n${block.reason}').toString()),
            ),
            trailing: can('instances.manage')
                ? ActionButton(
                    kind: ActionButtonKind.icon,
                    tooltip: L10n.of(context).ui_remove_restriction_e9623e5b,
                    onPressed: () => _removeBlock(block),
                    icon: const Icon(Icons.delete_outline),
                  )
                : null,
          ),
        if (can('instances.manage'))
          ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: _addBlock,
            icon: const Icon(Icons.add_rounded),
            label: Text(L10n.of(context).ui_add_restriction_10b486ea),
          ),
      ];

  List<Widget> _operatorWidgets() => [
        SettingsSectionHeader(
          L10n.of(context).ui_delegated_operators_69573ea7,
          subheading: L10n.of(context)
              .ui_scoped_roles_are_auditable_and_can_be_revoked_c3271608,
        ),
        for (final operator in _operators)
          ActionTile(
            leading: const Icon(Icons.admin_panel_settings_outlined),
            title: Text(operator.displayName ?? operator.username),
            subtitle: Text(L10n.of(context).ui_value0_value1_947523d9(
                (operator.userRef.wire).toString(),
                (operator.role).toString())),
            trailing: _identity?.roles.contains('owner') == true &&
                    operator.role != 'owner'
                ? ActionButton(
                    kind: ActionButtonKind.icon,
                    tooltip: L10n.of(context).ui_revoke_role_0709c115,
                    onPressed: () => _removeOperator(operator),
                    icon: const Icon(Icons.delete_outline),
                  )
                : null,
          ),
        if (_identity?.roles.contains('owner') == true)
          ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: _addOperator,
            icon: const Icon(Icons.person_add_alt_1_outlined),
            label: Text(L10n.of(context).ui_grant_role_6149e6e3),
          ),
      ];

  List<Widget> _auditWidgets() => [
        SettingsSectionHeader(
          L10n.of(context).ui_instance_audit_log_a6713c43,
          subheading: L10n.of(context)
              .ui_security_sensitive_administrative_changes_3bb3f36d,
        ),
        for (final event in _audit)
          ExpansionTile(
            leading: const Icon(Icons.history_rounded),
            title: Text(event.action),
            subtitle: Text(
              L10n.of(context).ui_value0_value1_value2_1f260e62(
                  (event.targetType).toString(),
                  (event.targetRef).toString(),
                  (_date(event.createdAt)).toString()),
            ),
            childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            children: [
              Align(
                alignment: Alignment.centerLeft,
                child: SelectableText(
                  L10n.of(context).ui_actor_value0_value1_2f109519(
                      (event.actorRef ?? event.actorKind).toString(),
                      (const JsonEncoder.withIndent('  ').convert(event.detail))
                          .toString()),
                  style: const TextStyle(fontFamily: 'monospace'),
                ),
              ),
            ],
          ),
      ];
}

final class _ReportAttachmentRow extends StatelessWidget {
  const _ReportAttachmentRow({
    required this.report,
    required this.attachment,
    required this.localDomain,
    required this.onOpen,
  });

  final AdministrationReport report;
  final AdministrationReportAttachment attachment;
  final Domain localDomain;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context) {
    final restriction = report.attachmentRestriction(attachment, localDomain);
    final previewable = report.canPreview(attachment, localDomain);
    final contentType = report.attachmentContentType(attachment);
    final disclosed = report.isDisclosed(attachment);
    return Card.outlined(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 10, 8, 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(_attachmentIcon(contentType)),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    report.attachmentFilename(attachment),
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    <String>[
                      if (contentType != null) contentType,
                      if (attachment.size case final size?) _bytes(size),
                      if (disclosed)
                        L10n.of(context).ui_reporter_disclosed_59b3796c,
                    ].join(' · '),
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  if (restriction != null) ...[
                    const SizedBox(height: 5),
                    Text(
                      restriction,
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                            color: Theme.of(context).colorScheme.error,
                          ),
                    ),
                  ],
                ],
              ),
            ),
            ActionButton(
              kind: ActionButtonKind.icon,
              tooltip: restriction ??
                  (previewable
                      ? L10n.of(context).ui_preview_or_download_fdc18684
                      : L10n.of(context).ui_open_or_download_86382136),
              onPressed: restriction == null ? onOpen : null,
              icon: Icon(previewable
                  ? Icons.visibility_outlined
                  : Icons.download_outlined),
            ),
          ],
        ),
      ),
    );
  }
}

IconData _attachmentIcon(String? contentType) {
  if (contentType?.startsWith('image/') == true) {
    return Icons.image_outlined;
  }
  if (contentType?.startsWith('video/') == true) {
    return Icons.movie_outlined;
  }
  if (contentType?.startsWith('audio/') == true) {
    return Icons.audio_file_outlined;
  }
  return Icons.insert_drive_file_outlined;
}

String _bytes(int value) {
  if (value < 1024) return '$value B';
  if (value < 1024 * 1024) return '${(value / 1024).toStringAsFixed(1)} KiB';
  return '${(value / (1024 * 1024)).toStringAsFixed(1)} MiB';
}

const _reportStatuses = <String>[
  'submitted',
  'triaged',
  'in_review',
  'awaiting_remote',
  'needs_information',
  'action_taken',
  'closed_no_action',
  'duplicate',
  'reopened',
];

const _accountActions = <String>[
  'none',
  'suspend_24h',
  'suspend_7d',
  'suspend_30d',
  'ban_permanent',
];

const _messageActions = <String>[
  'none',
  'delete_reported',
  'delete_1h',
  'delete_24h',
  'delete_7d',
  'delete_30d',
  'delete_all',
];

const _operatorRoles = <String>[
  'administrator',
  'trust_safety',
  'bot_reviewer',
  'operations',
  'auditor',
];
