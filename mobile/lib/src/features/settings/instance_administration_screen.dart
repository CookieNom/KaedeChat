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
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';

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
  String? _notice;
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
            summary: 'Administration is unavailable for this account');
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
            summary: 'Could not load ${_section.label.toLowerCase()}');
        _loading = false;
      });
    }
  }

  Future<void> _selectSection(_AdminSection section) async {
    if (section == _section) return;
    setState(() {
      _section = section;
      _notice = null;
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
            ListTile(
              title: Text(user.label),
              subtitle: Text(user.ref.wire),
            ),
            if (can('users.manage'))
              ListTile(
                leading: Icon(user.restricted
                    ? Icons.lock_open_outlined
                    : Icons.block_outlined),
                title: Text(user.restricted ? 'Restore access' : 'Ban account'),
                onTap: () => Navigator.pop(context, 'access'),
              ),
            if (can('users.manage') && user.accountType == 'human')
              for (final state in const ['unknown', 'adult', 'minor'])
                ListTile(
                  leading: Icon(user.ageAssuranceState == state
                      ? Icons.radio_button_checked
                      : Icons.radio_button_unchecked),
                  title: Text('Age assurance: $state'),
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
        _notice = '${user.label} access was updated.';
      } else if (action.startsWith('age:')) {
        final state = action.substring(4);
        if (!await _confirm('Set ${user.label} age assurance to $state?')) {
          return;
        }
        await repository.updateAdministrationUser(
          user.ref,
          <String, Object?>{'age_assurance_state': state, 'reason': null},
        );
        _notice = '${user.label} age assurance is now $state.';
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
      _notice = '${application.name} is now $next.';
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
                    Text('Report #${report.id}',
                        style: Theme.of(context).textTheme.titleLarge),
                    const SizedBox(height: 6),
                    Text('${report.category} · ${report.targetType}'),
                    if (report.description case final description?) ...[
                      const SizedBox(height: 8),
                      Text(description),
                    ],
                    const SizedBox(height: 12),
                    Text('Evidence',
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
                    SelectableText(
                      const JsonEncoder.withIndent('  ')
                          .convert(report.evidence),
                      style: const TextStyle(fontFamily: 'monospace'),
                    ),
                    if (can('reports.manage')) ...[
                      DropdownButtonFormField<String>(
                        initialValue: status,
                        decoration: const InputDecoration(labelText: 'Status'),
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
                        decoration:
                            const InputDecoration(labelText: 'Resolution'),
                      ),
                      FilledButton(
                        onPressed: () => Navigator.pop(sheetContext, 'update'),
                        child: const Text('Update report'),
                      ),
                      if (report.subjectRef != null) ...[
                        const Divider(height: 32),
                        Text('Enforcement',
                            style: Theme.of(context).textTheme.titleMedium),
                        DropdownButtonFormField<String>(
                          initialValue: accountAction,
                          decoration: const InputDecoration(
                              labelText: 'Account action'),
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
                          decoration: const InputDecoration(
                              labelText: 'Message action'),
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
                          decoration:
                              const InputDecoration(labelText: 'Audit reason'),
                        ),
                        FilledButton.tonalIcon(
                          onPressed: accountAction == 'none' &&
                                  messageAction == 'none'
                              ? null
                              : () => Navigator.pop(sheetContext, 'enforce'),
                          icon: const Icon(Icons.gavel_outlined),
                          label: const Text('Apply enforcement'),
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
        _notice = 'Report #${report.id} was updated.';
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
        _notice = 'Enforcement was applied to report #${report.id}.';
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
            title: const Text('Federation restriction'),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: domain,
                    autofocus: true,
                    autocorrect: false,
                    decoration:
                        const InputDecoration(labelText: 'Instance domain'),
                  ),
                  DropdownButtonFormField<String>(
                    initialValue: level,
                    decoration: const InputDecoration(labelText: 'Level'),
                    items: const [
                      DropdownMenuItem(
                          value: 'silence', child: Text('Silence')),
                      DropdownMenuItem(
                          value: 'suspend', child: Text('Suspend')),
                    ],
                    onChanged: (value) =>
                        setDialogState(() => level = value ?? level),
                  ),
                  CheckboxListTile(
                    contentPadding: EdgeInsets.zero,
                    value: includeSubdomains,
                    title: const Text('Include subdomains'),
                    onChanged: (value) => setDialogState(
                        () => includeSubdomains = value ?? false),
                  ),
                  TextField(
                    controller: reason,
                    maxLength: 500,
                    decoration:
                        const InputDecoration(labelText: 'Audit reason'),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(dialogContext, false),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: const Text('Save'),
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
      _notice = 'Federation policy updated.';
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
            title: const Text('Delegate administration'),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: user,
                  autofocus: true,
                  decoration: const InputDecoration(
                    labelText: 'Username or qualified local user ID',
                  ),
                ),
                DropdownButtonFormField<String>(
                  initialValue: role,
                  decoration: const InputDecoration(labelText: 'Role'),
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
              TextButton(
                onPressed: () => Navigator.pop(dialogContext, false),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: const Text('Grant'),
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
      _notice = 'Administrative role granted.';
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
    }
  }

  Future<bool> _confirm(String message) => showSettingsConfirmation(
        context,
        title: 'Confirm administrative action',
        message: message,
      );

  String _date(DateTime value) => DateFormat.yMd(
        Localizations.localeOf(context).toLanguageTag(),
      ).add_jm().format(value.toLocal());

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Administration'),
          actions: [
            IconButton(
              tooltip: 'Refresh',
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
                    'Signed in as ${identity.username} · ${identity.roles.join(', ')}',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: 10),
                  DropdownButtonFormField<_AdminSection>(
                    initialValue: _section,
                    decoration: const InputDecoration(labelText: 'Section'),
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
                if (_notice case final notice?)
                  SettingsStatusPanel.notice(message: notice),
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
        const SettingsSectionHeader(
          'Instance overview',
          subheading:
              'Live local counts and Trust & Safety workload from the authoritative instance.',
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
                      Text('${entry.value}',
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
        const SettingsSectionHeader('Local users'),
        TextField(
          controller: _userSearch,
          textInputAction: TextInputAction.search,
          onSubmitted: (_) => _loadSection(),
          decoration: InputDecoration(
            labelText: 'Search username',
            prefixIcon: const Icon(Icons.search_rounded),
            suffixIcon: IconButton(
              tooltip: 'Search',
              onPressed: _loadSection,
              icon: const Icon(Icons.arrow_forward_rounded),
            ),
          ),
        ),
        for (final user in _users)
          ListTile(
            leading: Icon(user.restricted
                ? Icons.person_off_outlined
                : Icons.person_outline_rounded),
            title: Text(user.label),
            subtitle: Text(
              '${user.ref.wire} · ${user.accountType} · age ${user.ageAssuranceState}',
            ),
            trailing: can('users.manage')
                ? const Icon(Icons.more_horiz_rounded)
                : null,
            onTap: () => _userActions(user),
          ),
      ];

  List<Widget> _applicationWidgets() => [
        const SettingsSectionHeader(
          'Applications',
          subheading: 'Suspend unsafe integrations across local installs.',
        ),
        for (final application in _applications)
          ListTile(
            leading: const Icon(Icons.smart_toy_outlined),
            title: Text(application.name),
            subtitle: Text(
              '${application.ref.wire} · ${application.status} · ${_date(application.updatedAt)}'
              '${application.canManageState ? '' : '\nState managed by ${application.stateAuthority.value}'}',
            ),
            trailing: can('bots.manage')
                ? IconButton(
                    tooltip: !application.canManageState
                        ? 'State managed by ${application.stateAuthority.value}'
                        : application.status == 'suspended'
                            ? 'Activate'
                            : 'Suspend',
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
        const SettingsSectionHeader(
          'Trust & Safety reports',
          subheading:
              'Review server-verified evidence and apply audited enforcement.',
        ),
        for (final report in _reports)
          Card(
            child: ListTile(
              leading: const Icon(Icons.flag_outlined),
              title: Text('${report.category} · ${report.targetType}'),
              subtitle: Text(
                '${report.status} · ${report.severity} · ${_date(report.createdAt)}\n${report.description ?? report.targetRef}',
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
        const SettingsSectionHeader(
          'Federation restrictions',
          subheading:
              'Silence or suspend traffic from a remote instance and optionally its subdomains.',
        ),
        for (final block in _blocks)
          ListTile(
            leading: const Icon(Icons.public_off_outlined),
            title: Text(block.domain),
            subtitle: Text(
              '${block.level}${block.includeSubdomains ? ' · includes subdomains' : ''}${block.reason == null ? '' : '\n${block.reason}'}',
            ),
            trailing: can('instances.manage')
                ? IconButton(
                    tooltip: 'Remove restriction',
                    onPressed: () => _removeBlock(block),
                    icon: const Icon(Icons.delete_outline),
                  )
                : null,
          ),
        if (can('instances.manage'))
          OutlinedButton.icon(
            onPressed: _addBlock,
            icon: const Icon(Icons.add_rounded),
            label: const Text('Add restriction'),
          ),
      ];

  List<Widget> _operatorWidgets() => [
        const SettingsSectionHeader(
          'Delegated operators',
          subheading:
              'Scoped roles are auditable and can be revoked by owners.',
        ),
        for (final operator in _operators)
          ListTile(
            leading: const Icon(Icons.admin_panel_settings_outlined),
            title: Text(operator.displayName ?? operator.username),
            subtitle: Text('${operator.userRef.wire} · ${operator.role}'),
            trailing: _identity?.roles.contains('owner') == true &&
                    operator.role != 'owner'
                ? IconButton(
                    tooltip: 'Revoke role',
                    onPressed: () => _removeOperator(operator),
                    icon: const Icon(Icons.delete_outline),
                  )
                : null,
          ),
        if (_identity?.roles.contains('owner') == true)
          OutlinedButton.icon(
            onPressed: _addOperator,
            icon: const Icon(Icons.person_add_alt_1_outlined),
            label: const Text('Grant role'),
          ),
      ];

  List<Widget> _auditWidgets() => [
        const SettingsSectionHeader(
          'Instance audit log',
          subheading: 'Security-sensitive administrative changes.',
        ),
        for (final event in _audit)
          ExpansionTile(
            leading: const Icon(Icons.history_rounded),
            title: Text(event.action),
            subtitle: Text(
              '${event.targetType} ${event.targetRef} · ${_date(event.createdAt)}',
            ),
            childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            children: [
              Align(
                alignment: Alignment.centerLeft,
                child: SelectableText(
                  'Actor: ${event.actorRef ?? event.actorKind}\n${const JsonEncoder.withIndent('  ').convert(event.detail)}',
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
                      if (disclosed) 'reporter disclosed',
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
            IconButton(
              tooltip: restriction ??
                  (previewable ? 'Preview or download' : 'Open or download'),
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
