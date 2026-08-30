import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/application_media_repository.dart';
import 'package:kaede_mobile/src/api/developer_portal_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/user_identity.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_media.dart';
import 'package:kaede_mobile/src/domain/developer_portal.dart';
import 'package:kaede_mobile/src/domain/permission_selection.dart';
import 'package:kaede_mobile/src/features/settings/application_media_screen.dart';
import 'package:kaede_mobile/src/features/shared/permission_picker.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

const developerApplicationScopes = <String>[
  'applications.assets.manage',
  'applications.commands',
  'applications.emojis.manage',
  'interactions.respond',
  'audit_logs.read',
  'automod.executions.read',
  'automod.rules.read',
  'automod.rules.manage',
  'guilds.read',
  'guilds.manage',
  'guilds.assets.manage',
  'channels.read',
  'channels.manage',
  'channels.overwrites.read',
  'channels.overwrites.manage',
  'members.read',
  'roles.read',
  'roles.manage',
  'events.read',
  'events.manage',
  'expressions.read',
  'expressions.manage',
  'installations.read',
  'integrations.read',
  'integrations.manage',
  'messages.metadata',
  'messages.content',
  'messages.history',
  'messages.send',
  'messages.edit.own',
  'messages.delete.own',
  'messages.manage',
  'tasks.read',
  'tasks.write',
  'tasks.manage',
  'attachments.read',
  'attachments.write',
  'reactions.read',
  'reactions.write',
  'polls.read',
  'polls.write',
  'moderation.bans',
  'moderation.members',
  'moderation.messages',
  'moderation.prune',
  'soundboard.read',
  'soundboard.use',
  'soundboard.manage',
  'voice.states.read',
  'voice.connect',
  'voice.listen',
  'voice.speak',
  'voice.stream',
  'voice.moderate',
  'invites.read',
  'invites.manage',
  'webhooks.read',
  'webhooks.manage',
  'emojis.manage',
  'dm.send',
];

const _userInstallScopes = <String>[
  'applications.commands',
  'interactions.respond',
  'attachments.read',
  'attachments.write',
];

KaedeRepository _repository(WidgetRef ref, KaedeRepository? override) =>
    override ?? ref.read(mobileControllerProvider.notifier).repository;

/// Native Developer Portal entry point. It mirrors the web portal's two
/// primary locations: Applications and Teams.
final class DeveloperPortalScreen extends ConsumerStatefulWidget {
  const DeveloperPortalScreen({super.key, this.repository});

  final KaedeRepository? repository;

  @override
  ConsumerState<DeveloperPortalScreen> createState() =>
      _DeveloperPortalScreenState();
}

final class _DeveloperPortalScreenState
    extends ConsumerState<DeveloperPortalScreen> {
  List<DeveloperApplication> _applications = const [];
  List<DeveloperTeam> _teams = const [];
  var _loading = true;
  String? _error;

  KaedeRepository get repository => _repository(ref, widget.repository);

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
      final results = await Future.wait<Object>([
        repository.developerApplications(),
        repository.developerTeams(),
      ]);
      if (!mounted) return;
      setState(() {
        _applications = results[0] as List<DeveloperApplication>;
        _teams = results[1] as List<DeveloperTeam>;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(error,
            summary: 'Could not load the Developer Portal');
        _loading = false;
      });
    }
  }

  Future<void> _createApplication() async {
    final name = TextEditingController();
    final description = TextEditingController();
    EntityRef? selectedTeam = _teams.firstOrNull?.ref;
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: const Text('Create an application'),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: name,
                    autofocus: true,
                    maxLength: 100,
                    decoration: const InputDecoration(labelText: 'Name'),
                  ),
                  TextField(
                    controller: description,
                    maxLength: 1000,
                    minLines: 2,
                    maxLines: 4,
                    decoration: const InputDecoration(labelText: 'Description'),
                  ),
                  if (_teams.isNotEmpty)
                    DropdownButtonFormField<EntityRef>(
                      initialValue: selectedTeam,
                      decoration: const InputDecoration(labelText: 'Team'),
                      items: [
                        for (final team in _teams)
                          DropdownMenuItem(
                            value: team.ref,
                            child: Text(team.name),
                          ),
                      ],
                      onChanged: (value) =>
                          setDialogState(() => selectedTeam = value),
                    ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(dialogContext, false),
                child: const Text('Cancel'),
              ),
              ValueListenableBuilder<TextEditingValue>(
                valueListenable: name,
                builder: (context, value, child) => FilledButton(
                  onPressed: value.text.trim().isEmpty
                      ? null
                      : () => Navigator.pop(dialogContext, true),
                  child: const Text('Create'),
                ),
              ),
            ],
          ),
        ),
      );
      if (accepted != true) return;
      final created = await repository.createDeveloperApplication(
        name: name.text,
        description: description.text,
        team: selectedTeam,
      );
      await _load();
      if (!mounted) return;
      await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => DeveloperApplicationScreen(
          application: created.ref,
          repository: repository,
        ),
      ));
      await _load();
    } on Object catch (error) {
      if (mounted) _showError(error, 'Could not create the application');
    } finally {
      name.dispose();
      description.dispose();
    }
  }

  void _showError(Object error, String summary) =>
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(userFacingError(error, summary: summary))),
      );

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Developer Portal'),
          actions: [
            IconButton(
              tooltip: 'Refresh',
              onPressed: _loading ? null : _load,
              icon: const Icon(Icons.refresh_rounded),
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: _loading ? null : _createApplication,
          icon: const Icon(Icons.add_rounded),
          label: const Text('New app'),
        ),
        body: SafeArea(
          child: RefreshIndicator(
            onRefresh: _load,
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 96),
              children: [
                const SettingsInfo(
                  'Applications are managed at their home instance. Qualified references keep remote team projects on the correct authority.',
                ),
                const SettingsSectionHeader('Applications'),
                if (_loading)
                  const Center(
                    child: Padding(
                      padding: EdgeInsets.all(32),
                      child: CircularProgressIndicator(),
                    ),
                  )
                else if (_error case final error?)
                  SettingsStatusPanel.error(message: error, onRetry: _load)
                else if (_applications.isEmpty)
                  const SettingsInfo(
                    'Create your first application to register commands, workers, assets and install links.',
                  )
                else
                  for (var index = 0; index < _applications.length; index++)
                    SettingsRow.chevron(
                      title: _applications[index].name,
                      subtitle:
                          '${_applications[index].status} · ${_applications[index].ref.wire}',
                      divider: index != _applications.length - 1,
                      leading: const Icon(Icons.smart_toy_outlined),
                      onTap: () async {
                        await Navigator.of(context).push(
                          MaterialPageRoute<void>(
                            builder: (_) => DeveloperApplicationScreen(
                              application: _applications[index].ref,
                              repository: repository,
                            ),
                          ),
                        );
                        await _load();
                      },
                    ),
                const SettingsSectionHeader(
                  'Teams',
                  subheading:
                      'Share application access with local developers and security staff.',
                ),
                SettingsRow.chevron(
                  title: 'Manage teams',
                  subtitle:
                      '${_teams.length} workspace${_teams.length == 1 ? '' : 's'}',
                  leading: const Icon(Icons.groups_outlined),
                  onTap: () async {
                    await Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => DeveloperTeamsScreen(
                          repository: repository,
                        ),
                      ),
                    );
                    await _load();
                  },
                ),
              ],
            ),
          ),
        ),
      );
}

final class DeveloperTeamsScreen extends ConsumerStatefulWidget {
  const DeveloperTeamsScreen({super.key, this.repository});

  final KaedeRepository? repository;

  @override
  ConsumerState<DeveloperTeamsScreen> createState() =>
      _DeveloperTeamsScreenState();
}

final class _DeveloperTeamsScreenState
    extends ConsumerState<DeveloperTeamsScreen> {
  List<DeveloperTeam> _teams = const [];
  EntityRef? _selected;
  List<DeveloperTeamMember> _members = const [];
  var _loading = true;
  String? _error;

  KaedeRepository get repository => _repository(ref, widget.repository);
  DeveloperTeam? get selected =>
      _teams.where((team) => team.ref == _selected).firstOrNull;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load([EntityRef? preferred]) async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final teams = await repository.developerTeams();
      final selectedRef = preferred ?? _selected ?? teams.firstOrNull?.ref;
      final members = selectedRef == null
          ? const <DeveloperTeamMember>[]
          : await repository.developerTeamMembers(selectedRef);
      if (!mounted) return;
      setState(() {
        _teams = teams;
        _selected = selectedRef;
        _members = members;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error =
            userFacingError(error, summary: 'Could not load developer teams');
        _loading = false;
      });
    }
  }

  Future<void> _createTeam() async {
    final name = await showSettingsTextDialog(
      context,
      title: 'Create a team',
      label: 'Team name',
      maxLength: 100,
    );
    if (name == null) return;
    try {
      final team = await repository.createDeveloperTeam(name);
      await _load(team.ref);
    } on Object catch (error) {
      _snackError(error, 'Could not create the team');
    }
  }

  Future<void> _select(DeveloperTeam team) async {
    setState(() {
      _selected = team.ref;
      _loading = true;
    });
    try {
      final members = await repository.developerTeamMembers(team.ref);
      if (mounted) {
        setState(() {
          _members = members;
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() {
          _error =
              userFacingError(error, summary: 'Could not load team members');
          _loading = false;
        });
      }
    }
  }

  Future<void> _addMember() async {
    final team = selected;
    if (team == null || !team.canManageMembers) return;
    final input = TextEditingController();
    var role = 'developer';
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: const Text('Add team member'),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: input,
                  autofocus: true,
                  decoration: const InputDecoration(
                    labelText: 'Username or qualified user ID',
                    hintText: 'name@instance.example',
                  ),
                ),
                DropdownButtonFormField<String>(
                  initialValue: role,
                  decoration: const InputDecoration(labelText: 'Role'),
                  items: [
                    for (final value in _teamRoles)
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
                child: const Text('Add'),
              ),
            ],
          ),
        ),
      );
      if (accepted != true) return;
      await repository.addDeveloperTeamMember(
        team: team.ref,
        user: await repository.resolveUserIdentity(input.text),
        role: role,
      );
      await _load(team.ref);
    } on Object catch (error) {
      _snackError(error, 'Could not add the team member');
    } finally {
      input.dispose();
    }
  }

  Future<void> _memberActions(DeveloperTeamMember member) async {
    final team = selected;
    if (team == null || !team.canManageMembers) return;
    final action = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
                title: Text(member.label), subtitle: Text(member.ref.wire)),
            RadioGroup<String>(
              groupValue: member.role,
              onChanged: (value) => Navigator.pop(context, 'role:$value'),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  for (final role in _teamRoles)
                    RadioListTile<String>(
                      value: role,
                      title: Text(role),
                    ),
                ],
              ),
            ),
            ListTile(
              leading: const Icon(Icons.person_remove_outlined),
              title: const Text('Remove from team'),
              onTap: () => Navigator.pop(context, 'remove'),
            ),
          ],
        ),
      ),
    );
    if (action == null) return;
    if (!mounted) return;
    try {
      if (action == 'remove') {
        if (!await showSettingsConfirmation(
          context,
          message: 'Remove ${member.label} from ${team.name}?',
        )) {
          return;
        }
        await repository.removeDeveloperTeamMember(
          team: team.ref,
          user: member.ref,
        );
      } else if (action.startsWith('role:')) {
        await repository.updateDeveloperTeamMember(
          team: team.ref,
          user: member.ref,
          role: action.substring(5),
        );
      }
      await _load(team.ref);
    } on Object catch (error) {
      _snackError(error, 'Could not update the team member');
    }
  }

  void _snackError(Object error, String summary) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(userFacingError(error, summary: summary))),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Developer teams'),
          actions: [
            IconButton(
              tooltip: 'New team',
              onPressed: _createTeam,
              icon: const Icon(Icons.group_add_outlined),
            ),
          ],
        ),
        body: RefreshIndicator(
          onRefresh: _load,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
            children: [
              const SettingsSectionHeader('Workspaces'),
              if (_error case final error?)
                SettingsStatusPanel.error(message: error, onRetry: _load),
              for (final team in _teams)
                ListTile(
                  selected: team.ref == _selected,
                  leading: Icon(team.personal
                      ? Icons.person_outline_rounded
                      : Icons.groups_outlined),
                  title: Text(team.name),
                  subtitle: Text(team.personal ? 'Only you' : team.role),
                  onTap: () => _select(team),
                ),
              if (selected case final team?) ...[
                SettingsSectionHeader(
                  '${team.name} members',
                  subheading: team.personal
                      ? 'The personal workspace cannot be shared.'
                      : 'Owners and administrators can assign scoped roles.',
                ),
                if (_loading)
                  const Center(child: CircularProgressIndicator())
                else
                  for (final member in _members)
                    ListTile(
                      leading: const CircleAvatar(
                        child: Icon(Icons.person_outline_rounded),
                      ),
                      title: Text(member.label),
                      subtitle: Text('${member.ref.wire} · ${member.role}'),
                      trailing: team.canManageMembers
                          ? const Icon(Icons.more_horiz_rounded)
                          : null,
                      onTap: team.canManageMembers
                          ? () => _memberActions(member)
                          : null,
                    ),
                if (team.canManageMembers)
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: OutlinedButton.icon(
                      onPressed: _addMember,
                      icon: const Icon(Icons.person_add_alt_1_outlined),
                      label: const Text('Add member'),
                    ),
                  ),
              ],
            ],
          ),
        ),
      );
}

const _teamRoles = <String>[
  'owner',
  'administrator',
  'developer',
  'security',
  'analyst',
  'support',
];

final class DeveloperApplicationScreen extends ConsumerStatefulWidget {
  const DeveloperApplicationScreen({
    required this.application,
    super.key,
    this.repository,
  });

  final EntityRef application;
  final KaedeRepository? repository;

  @override
  ConsumerState<DeveloperApplicationScreen> createState() =>
      _DeveloperApplicationScreenState();
}

final class _DeveloperApplicationScreenState
    extends ConsumerState<DeveloperApplicationScreen> {
  final _name = TextEditingController();
  final _description = TextEditingController();
  final _supportUrl = TextEditingController();
  final _privacyUrl = TextEditingController();
  final _permissionBits = TextEditingController();
  final _commands = TextEditingController();
  DeveloperApplicationDetail? _application;
  List<DeveloperCredential> _credentials = const [];
  List<DeveloperWorker> _workers = const [];
  List<DeveloperInstallTemplate> _templates = const [];
  List<DeveloperInstallation> _installations = const [];
  List<DeveloperInstanceRule> _rules = const [];
  Set<String> _defaultScopes = const {};
  Set<String> _defaultIntents = const {};
  Set<String> _installTypes = const {};
  Set<String> _userScopes = const {};
  Set<String> _userContexts = const {};
  Set<String> _e2eeModes = const {};
  var _targetPolicy = 'open';
  var _loading = true;
  var _busy = false;
  String? _error;
  String? _notice;

  KaedeRepository get repository => _repository(ref, widget.repository);

  String get _permissionSummary {
    try {
      final selected = selectedApplicationPermissions(_permissionBits.text);
      if (selected.isEmpty) return 'No server permissions requested';
      final labels = selected.map((item) => item.label).take(4).join(', ');
      return '${selected.length} selected · $labels';
    } on FormatException {
      return 'Invalid saved permission mask · reload this application';
    }
  }

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _name.dispose();
    _description.dispose();
    _supportUrl.dispose();
    _privacyUrl.dispose();
    _permissionBits.dispose();
    _commands.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final results = await Future.wait<Object>([
        repository.developerApplication(widget.application),
        repository.applicationCommands(widget.application),
        repository.applicationCredentials(widget.application),
        repository.applicationWorkers(widget.application),
        repository.applicationInstallTemplates(widget.application),
        repository.applicationInstallations(widget.application),
        repository.applicationInstanceRules(widget.application),
      ]);
      final application = results[0] as DeveloperApplicationDetail;
      if (!mounted) return;
      setState(() {
        _application = application;
        _name.text = application.name;
        _description.text = application.description ?? '';
        _supportUrl.text = application.supportUrl ?? '';
        _privacyUrl.text = application.privacyUrl ?? '';
        _permissionBits.text = application.defaultPermissions;
        _targetPolicy = application.targetPolicy;
        _defaultScopes = application.defaultScopes.toSet();
        _defaultIntents = application.defaultIntents.toSet();
        _installTypes = application.supportedInstallTypes.toSet();
        _userScopes = application.userInstallScopes.toSet();
        _userContexts = application.userInstallContexts.toSet();
        _e2eeModes = application.e2eeModes.toSet();
        _commands.text = const JsonEncoder.withIndent('  ').convert(results[1]);
        _credentials = results[2] as List<DeveloperCredential>;
        _workers = results[3] as List<DeveloperWorker>;
        _templates = results[4] as List<DeveloperInstallTemplate>;
        _installations = results[5] as List<DeveloperInstallation>;
        _rules = results[6] as List<DeveloperInstanceRule>;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error =
            userFacingError(error, summary: 'Could not load this application');
        _loading = false;
      });
    }
  }

  Future<void> _saveApplication() async {
    if (_busy) return;
    final permissions = BigInt.tryParse(_permissionBits.text.trim());
    if (permissions == null || permissions.isNegative) {
      _showErrorMessage(
          'The saved permission selection is invalid. Reload the application.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      final updated = await repository.updateDeveloperApplication(
        widget.application,
        <String, Object?>{
          'name': _name.text.trim(),
          'description': _nullable(_description.text),
          'support_url': _nullable(_supportUrl.text),
          'privacy_url': _nullable(_privacyUrl.text),
          'target_policy': _targetPolicy,
          'default_scopes': _defaultScopes.toList()..sort(),
          'default_intents': _defaultIntents.toList()..sort(),
          'default_permissions': permissions.toString(),
          'supported_install_types': _installTypes.toList()..sort(),
          'user_install_scopes': _userScopes.toList()..sort(),
          'user_install_contexts': _userContexts.toList()..sort(),
          'e2ee_modes': _e2eeModes.toList()..sort(),
        },
      );
      if (!mounted) return;
      setState(() {
        _application = updated;
        _notice = 'Application settings saved.';
      });
    } on Object catch (error) {
      _showError(error, 'Could not save the application');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _chooseDefaultPermissions() async {
    late final BigInt current;
    try {
      current = applicationPermissionMask(_permissionBits.text);
    } on FormatException {
      _showErrorMessage(
          'The saved permission mask is invalid. Reload the application.');
      return;
    }
    final selected = await showApplicationPermissionPicker(
      context,
      selected: current,
    );
    if (selected == null || !mounted) return;
    setState(() => _permissionBits.text = selected.toString());
  }

  Future<void> _saveCommands() async {
    try {
      final decoded = jsonDecode(_commands.text);
      if (decoded is! List) {
        throw const FormatException('Commands must be a JSON array.');
      }
      setState(() => _busy = true);
      await repository.replaceApplicationCommands(
        widget.application,
        decoded.cast<Object?>(),
      );
      if (mounted) setState(() => _notice = 'Commands published.');
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not publish commands');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _createCredential() async {
    final label = await showSettingsTextDialog(
      context,
      title: 'Create control credential',
      label: 'Label',
      initialValue: 'Deployment',
      maxLength: 100,
    );
    if (label == null) return;
    try {
      final token = await repository.createApplicationCredential(
        widget.application,
        label,
      );
      if (!mounted) return;
      await showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (context) => AlertDialog(
          title: const Text('Copy this credential now'),
          content: SelectableText(token),
          actions: [
            TextButton.icon(
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: token));
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('Credential copied.')),
                  );
                }
              },
              icon: const Icon(Icons.copy_rounded),
              label: const Text('Copy'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Done'),
            ),
          ],
        ),
      );
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not create the control credential');
    }
  }

  Future<void> _revokeCredential(DeveloperCredential credential) async {
    if (!await showSettingsConfirmation(
      context,
      message: 'Revoke ${credential.label}?',
    )) {
      return;
    }
    try {
      await repository.revokeApplicationCredential(
        widget.application,
        credential.id,
      );
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not revoke the credential');
    }
  }

  Future<void> _createWorker() async {
    final name = TextEditingController(text: 'Production worker');
    final publicKey = TextEditingController();
    final targets = TextEditingController();
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Enroll worker'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: name,
                  maxLength: 100,
                  decoration: const InputDecoration(labelText: 'Worker name'),
                ),
                TextField(
                  controller: publicKey,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: 'Ed25519 public key',
                    helperText: 'Base64url, 32 bytes',
                  ),
                ),
                TextField(
                  controller: targets,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: 'Target instances',
                    helperText: 'Comma-separated hostnames',
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Enroll'),
            ),
          ],
        ),
      );
      if (accepted != true) return;
      await repository.createApplicationWorker(
        application: widget.application,
        name: name.text,
        publicKey: publicKey.text,
        scopes: _defaultScopes.toList(),
        intents: _defaultIntents.toList(),
        targetDomains: targets.text
            .split(',')
            .map((item) => item.trim())
            .where((item) => item.isNotEmpty)
            .toList(growable: false),
      );
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not enroll the worker');
    } finally {
      name.dispose();
      publicKey.dispose();
      targets.dispose();
    }
  }

  Future<void> _revokeWorker(DeveloperWorker worker) async {
    if (!await showSettingsConfirmation(
      context,
      message: 'Revoke ${worker.name}? Existing sessions will stop.',
    )) {
      return;
    }
    try {
      await repository.revokeApplicationWorker(widget.application, worker.id);
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not revoke the worker');
    }
  }

  Future<void> _createTemplate() async {
    final slug = TextEditingController(text: 'install');
    final name = TextEditingController(text: 'Install bot');
    final description = TextEditingController();
    var mode = _e2eeModes.contains('participant') ? 'participant' : 'disabled';
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: const Text('Create bot invite'),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: slug,
                    decoration: const InputDecoration(labelText: 'Link slug'),
                  ),
                  TextField(
                    controller: name,
                    decoration: const InputDecoration(labelText: 'Name'),
                  ),
                  TextField(
                    controller: description,
                    decoration: const InputDecoration(labelText: 'Description'),
                  ),
                  DropdownButtonFormField<String>(
                    initialValue: mode,
                    decoration:
                        const InputDecoration(labelText: 'Encryption mode'),
                    items: [
                      for (final value in <String>{
                        'disabled',
                        ..._e2eeModes,
                      })
                        DropdownMenuItem(value: value, child: Text(value)),
                    ],
                    onChanged: (value) =>
                        setDialogState(() => mode = value ?? mode),
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
                child: const Text('Create link'),
              ),
            ],
          ),
        ),
      );
      if (accepted != true) return;
      await repository.createApplicationInstallTemplate(
        application: widget.application,
        slug: slug.text,
        name: name.text,
        description: description.text,
        scopes: _defaultScopes.toList(),
        intents: _defaultIntents.toList(),
        permissions: _permissionBits.text.trim(),
        e2eeMode: mode,
      );
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not create the invite link');
    } finally {
      slug.dispose();
      name.dispose();
      description.dispose();
    }
  }

  Future<void> _addRule() async {
    final domain = TextEditingController();
    var effect = 'deny';
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: const Text('Instance rule'),
            content: Column(
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
                  initialValue: effect,
                  decoration: const InputDecoration(labelText: 'Effect'),
                  items: const [
                    DropdownMenuItem(value: 'allow', child: Text('Allow')),
                    DropdownMenuItem(value: 'deny', child: Text('Deny')),
                  ],
                  onChanged: (value) =>
                      setDialogState(() => effect = value ?? effect),
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
                child: const Text('Save'),
              ),
            ],
          ),
        ),
      );
      if (accepted != true) return;
      await repository.putApplicationInstanceRule(
        application: widget.application,
        targetDomain: domain.text,
        effect: effect,
      );
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not save the instance rule');
    } finally {
      domain.dispose();
    }
  }

  Future<void> _deleteRule(DeveloperInstanceRule rule) async {
    if (!await showSettingsConfirmation(
      context,
      message: 'Remove the rule for ${rule.targetDomain}?',
    )) {
      return;
    }
    try {
      await repository.deleteApplicationInstanceRule(
        application: widget.application,
        targetDomain: rule.targetDomain,
      );
      await _load();
    } on Object catch (error) {
      _showError(error, 'Could not remove the instance rule');
    }
  }

  Future<void> _chooseValues({
    required String title,
    required Set<String> selected,
    required Iterable<String> choices,
    required ValueChanged<Set<String>> update,
    Set<String> requiredValues = const {},
    int minimum = 0,
  }) async {
    final draft = selected.toSet();
    final all = <String>{...choices, ...selected}.toList()..sort();
    final result = await showModalBottomSheet<Set<String>>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, setSheetState) => SafeArea(
          child: SizedBox(
            height: MediaQuery.sizeOf(context).height * .78,
            child: Column(
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(18, 0, 10, 6),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(title,
                            style: Theme.of(context).textTheme.titleLarge),
                      ),
                      FilledButton(
                        onPressed: draft.length < minimum
                            ? null
                            : () => Navigator.pop(sheetContext, draft),
                        child: const Text('Done'),
                      ),
                    ],
                  ),
                ),
                Expanded(
                  child: ListView.builder(
                    itemCount: all.length,
                    itemBuilder: (context, index) {
                      final value = all[index];
                      return CheckboxListTile(
                        value: draft.contains(value),
                        title: Text(value),
                        subtitle: requiredValues.contains(value)
                            ? const Text('Required for user installs')
                            : null,
                        onChanged: requiredValues.contains(value)
                            ? null
                            : (enabled) => setSheetState(() {
                                  if (enabled == true) {
                                    draft.add(value);
                                  } else {
                                    draft.remove(value);
                                  }
                                }),
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    if (result != null) update(result);
  }

  void _showError(Object error, String summary) {
    if (!mounted) return;
    setState(() => _error = userFacingError(error, summary: summary));
  }

  void _showErrorMessage(String message) {
    if (mounted) setState(() => _error = message);
  }

  @override
  Widget build(BuildContext context) {
    final application = _application;
    return Scaffold(
      appBar: AppBar(
        title: Text(application?.name ?? 'Application'),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            onPressed: _loading ? null : _load,
            icon: const Icon(Icons.refresh_rounded),
          ),
          TextButton(
            onPressed: application == null || _busy ? null : _saveApplication,
            child: const Text('Save'),
          ),
        ],
      ),
      body: _loading && application == null
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _load,
              child: ListView(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 40),
                children: [
                  if (_error case final error?)
                    SettingsStatusPanel.error(
                      message: error,
                      onRetry: _load,
                    ),
                  if (_notice case final notice?)
                    SettingsStatusPanel.notice(message: notice),
                  if (application != null) ...[
                    Text(application.botHandle,
                        style: Theme.of(context).textTheme.bodySmall),
                    SelectableText(application.ref.wire),
                    const SettingsSectionHeader(
                      'General information',
                      subheading:
                          'Identity, support links and application-wide authorization ceilings.',
                    ),
                    TextField(
                      controller: _name,
                      maxLength: 100,
                      decoration: const InputDecoration(labelText: 'Name'),
                    ),
                    TextField(
                      controller: _description,
                      minLines: 2,
                      maxLines: 5,
                      maxLength: 1000,
                      decoration:
                          const InputDecoration(labelText: 'Description'),
                    ),
                    TextField(
                      controller: _supportUrl,
                      keyboardType: TextInputType.url,
                      decoration:
                          const InputDecoration(labelText: 'Support URL'),
                    ),
                    TextField(
                      controller: _privacyUrl,
                      keyboardType: TextInputType.url,
                      decoration:
                          const InputDecoration(labelText: 'Privacy URL'),
                    ),
                    DropdownButtonFormField<String>(
                      initialValue: _targetPolicy,
                      decoration:
                          const InputDecoration(labelText: 'Target policy'),
                      items: const [
                        DropdownMenuItem(
                          value: 'open',
                          child: Text('Open federation'),
                        ),
                        DropdownMenuItem(
                          value: 'allowlist',
                          child: Text('Allowlist only'),
                        ),
                        DropdownMenuItem(
                          value: 'blocklist',
                          child: Text('Open except blocked instances'),
                        ),
                        DropdownMenuItem(
                          value: 'local_only',
                          child: Text('Local instance only'),
                        ),
                      ],
                      onChanged: (value) => setState(
                          () => _targetPolicy = value ?? _targetPolicy),
                    ),
                    ListTile(
                      contentPadding: EdgeInsets.zero,
                      leading: Icon(Icons.key_rounded),
                      title: Text('Default permissions'),
                      subtitle: Text(
                        _permissionSummary,
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                      ),
                      trailing: Icon(Icons.chevron_right_rounded),
                      onTap: _busy ? null : _chooseDefaultPermissions,
                    ),
                    _MultiValueRow(
                      title: 'Gateway scopes',
                      values: _defaultScopes,
                      onTap: () => _chooseValues(
                        title: 'Gateway scopes',
                        selected: _defaultScopes,
                        choices: developerApplicationScopes,
                        requiredValues: _installTypes.contains('user_install')
                            ? _userScopes
                            : const <String>{},
                        update: (value) =>
                            setState(() => _defaultScopes = value),
                      ),
                    ),
                    _MultiValueRow(
                      title: 'Gateway intents',
                      values: _defaultIntents,
                      onTap: () => _chooseValues(
                        title: 'Gateway intents',
                        selected: _defaultIntents,
                        choices: botIntentNames,
                        requiredValues: _installTypes.contains('user_install')
                            ? const {'interactions'}
                            : const <String>{},
                        update: (value) =>
                            setState(() => _defaultIntents = value),
                      ),
                    ),
                    _MultiValueRow(
                      title: 'Installation contexts',
                      values: _installTypes,
                      onTap: () => _chooseValues(
                        title: 'Installation contexts',
                        selected: _installTypes,
                        choices: const ['guild_install', 'user_install'],
                        minimum: 1,
                        update: (value) => setState(() {
                          _installTypes = value;
                          if (value.contains('user_install')) {
                            _userScopes = {
                              ..._userScopes,
                              'applications.commands',
                              'interactions.respond',
                            };
                            _defaultScopes = {
                              ..._defaultScopes,
                              ..._userScopes
                            };
                            _defaultIntents = {
                              ..._defaultIntents,
                              'interactions',
                            };
                          }
                        }),
                      ),
                    ),
                    if (_installTypes.contains('user_install')) ...[
                      _MultiValueRow(
                        title: 'User-install scopes',
                        values: _userScopes,
                        onTap: () => _chooseValues(
                          title: 'User-install scopes',
                          selected: _userScopes,
                          choices: _userInstallScopes,
                          requiredValues: const {
                            'applications.commands',
                            'interactions.respond',
                          },
                          update: (value) => setState(() {
                            _userScopes = {
                              ...value,
                              'applications.commands',
                              'interactions.respond',
                            };
                            _defaultScopes = {
                              ..._defaultScopes,
                              ..._userScopes,
                            };
                          }),
                        ),
                      ),
                      _MultiValueRow(
                        title: 'User-install command contexts',
                        values: _userContexts,
                        onTap: () => _chooseValues(
                          title: 'User-install command contexts',
                          selected: _userContexts,
                          choices: const [
                            'guild',
                            'bot_dm',
                            'private_channel',
                          ],
                          minimum: 1,
                          update: (value) =>
                              setState(() => _userContexts = value),
                        ),
                      ),
                    ],
                    _MultiValueRow(
                      title: 'Encrypted interaction modes',
                      values: _e2eeModes,
                      onTap: () => _chooseValues(
                        title: 'Encrypted interaction modes',
                        selected: _e2eeModes,
                        choices: const ['participant'],
                        update: (value) => setState(() => _e2eeModes = value),
                      ),
                    ),
                    Padding(
                      padding: const EdgeInsets.only(top: 12),
                      child: FilledButton.icon(
                        onPressed: _busy ? null : _saveApplication,
                        icon: const Icon(Icons.save_outlined),
                        label: const Text('Save application settings'),
                      ),
                    ),
                    const SettingsSectionHeader('Commands'),
                    const SettingsInfo(
                      'Register chat-input, user and message commands with the same HTTP JSON contract used by the web portal. Limits are 100 chat-input, 15 user and 15 message commands.',
                    ),
                    TextField(
                      controller: _commands,
                      minLines: 10,
                      maxLines: 24,
                      autocorrect: false,
                      style: const TextStyle(fontFamily: 'monospace'),
                      decoration: const InputDecoration(
                          labelText: 'Command JSON array'),
                    ),
                    Align(
                      alignment: Alignment.centerRight,
                      child: FilledButton.icon(
                        onPressed: _busy ? null : _saveCommands,
                        icon: const Icon(Icons.publish_outlined),
                        label: const Text('Publish commands'),
                      ),
                    ),
                    _ResourceSection(
                      title: 'Control credentials',
                      actionLabel: 'New credential',
                      onAction: _createCredential,
                      children: [
                        for (final credential in _credentials)
                          ListTile(
                            leading: const Icon(Icons.key_outlined),
                            title: Text(credential.label),
                            subtitle: Text(
                              '${credential.tokenHint} · ${credential.revokedAt == null ? 'active' : 'revoked'}',
                            ),
                            trailing: credential.revokedAt == null
                                ? IconButton(
                                    tooltip: 'Revoke',
                                    onPressed: () =>
                                        _revokeCredential(credential),
                                    icon: const Icon(Icons.delete_outline),
                                  )
                                : null,
                          ),
                      ],
                    ),
                    _ResourceSection(
                      title: 'Worker keys',
                      actionLabel: 'Enroll worker',
                      onAction: _createWorker,
                      children: [
                        for (final worker in _workers)
                          ListTile(
                            leading: const Icon(
                                Icons.precision_manufacturing_outlined),
                            title: Text(worker.name),
                            subtitle: Text(
                              '${worker.targetDomains.isEmpty ? 'Any delegated target' : worker.targetDomains.join(', ')} · ${worker.revokedAt == null ? 'active' : 'revoked'}',
                            ),
                            trailing: worker.revokedAt == null
                                ? IconButton(
                                    tooltip: 'Revoke',
                                    onPressed: () => _revokeWorker(worker),
                                    icon: const Icon(Icons.delete_outline),
                                  )
                                : null,
                          ),
                      ],
                    ),
                    const SettingsSectionHeader('Application media'),
                    SettingsRow.chevron(
                      title: 'Assets and application emoji',
                      subtitle:
                          'Upload safety-scanned icons, artwork and custom emoji.',
                      leading: const Icon(Icons.collections_outlined),
                      onTap: () => Navigator.of(context).push(
                        MaterialPageRoute<void>(
                          builder: (_) => ApplicationMediaManagerScreen(
                            application: DeveloperApplication(
                              ref: application.ref,
                              name: application.name,
                              description: application.description,
                              iconHash: application.iconHash,
                              status: application.status,
                            ),
                            repository: repository,
                          ),
                        ),
                      ),
                    ),
                    _ResourceSection(
                      title: 'Bot invite links',
                      actionLabel: 'New invite',
                      onAction: _createTemplate,
                      children: [
                        for (final template in _templates)
                          ListTile(
                            leading: const Icon(Icons.add_link_rounded),
                            title: Text(template.name),
                            subtitle: Text(
                              '${template.slug} · ${template.e2eeMode} · ${template.active ? 'active' : 'inactive'}',
                            ),
                            trailing: IconButton(
                              tooltip: 'Copy invite link',
                              onPressed: () async {
                                await Clipboard.setData(
                                  ClipboardData(text: template.inviteUrl),
                                );
                                if (context.mounted) {
                                  ScaffoldMessenger.of(context).showSnackBar(
                                    const SnackBar(
                                        content: Text('Invite link copied.')),
                                  );
                                }
                              },
                              icon: const Icon(Icons.copy_rounded),
                            ),
                          ),
                      ],
                    ),
                    _ResourceSection(
                      title: 'Federated instance policy',
                      actionLabel: 'Add rule',
                      onAction: _addRule,
                      children: [
                        for (final rule in _rules)
                          ListTile(
                            leading: Icon(rule.effect == 'allow'
                                ? Icons.check_circle_outline
                                : Icons.block_outlined),
                            title: Text(rule.targetDomain),
                            subtitle: Text(rule.effect),
                            trailing: IconButton(
                              tooltip: 'Remove rule',
                              onPressed: () => _deleteRule(rule),
                              icon: const Icon(Icons.delete_outline),
                            ),
                          ),
                      ],
                    ),
                    _ResourceSection(
                      title: 'Installations',
                      children: [
                        for (final installation in _installations)
                          ListTile(
                            leading: const Icon(Icons.hub_outlined),
                            title: Text(installation.guildRef.wire),
                            subtitle: Text(
                              '${installation.status} · ${installation.e2eeMode} · revision ${installation.grantRevision} · ${installation.scopes.length} scopes · ${installation.channelRestrictions.isEmpty ? 'all role-permitted channels' : '${installation.channelRestrictions.length} channel restrictions'}',
                            ),
                          ),
                      ],
                    ),
                  ],
                ],
              ),
            ),
    );
  }
}

String? _nullable(String value) => value.trim().isEmpty ? null : value.trim();

final class _MultiValueRow extends StatelessWidget {
  const _MultiValueRow({
    required this.title,
    required this.values,
    required this.onTap,
  });

  final String title;
  final Set<String> values;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => ListTile(
        contentPadding: EdgeInsets.zero,
        title: Text(title),
        subtitle: Text(
          values.isEmpty
              ? 'None selected'
              : (values.toList()..sort()).join(', '),
          maxLines: 3,
          overflow: TextOverflow.ellipsis,
        ),
        trailing: const Icon(Icons.chevron_right_rounded),
        onTap: onTap,
      );
}

final class _ResourceSection extends StatelessWidget {
  const _ResourceSection({
    required this.title,
    required this.children,
    this.actionLabel,
    this.onAction,
  });

  final String title;
  final List<Widget> children;
  final String? actionLabel;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SettingsSectionHeader(title),
          if (children.isEmpty)
            const SettingsInfo('Nothing has been configured here yet.')
          else
            ...children,
          if (onAction != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: OutlinedButton.icon(
                onPressed: onAction,
                icon: const Icon(Icons.add_rounded),
                label: Text(actionLabel ?? 'Add'),
              ),
            ),
        ],
      );
}
