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
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/permission_picker.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
            summary: L10n.of(context)
                .ui_could_not_load_the_developer_portal_d4cc2a62);
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
            title: Text(L10n.of(context).ui_create_an_application_1d7260a2),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: name,
                    autofocus: true,
                    maxLength: 100,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_name_0fe07306),
                  ),
                  TextField(
                    controller: description,
                    maxLength: 1000,
                    minLines: 2,
                    maxLines: 4,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_description_66de7a09),
                  ),
                  if (_teams.isNotEmpty)
                    DropdownButtonFormField<EntityRef>(
                      initialValue: selectedTeam,
                      decoration: InputDecoration(
                          labelText: L10n.of(context).ui_team_2329c92c),
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
              ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              ValueListenableBuilder<TextEditingValue>(
                valueListenable: name,
                builder: (context, value, child) => ActionButton(
                  onPressed: value.text.trim().isEmpty
                      ? null
                      : () => Navigator.pop(dialogContext, true),
                  child: Text(L10n.of(context).ui_create_990de47d),
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
      if (mounted) {
        _showError(error,
            L10n.of(context).ui_could_not_create_the_application_46822e3c);
      }
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
          title: Text(L10n.of(context).ui_developer_portal_f5349d3d),
          actions: [
            ActionButton(
              kind: ActionButtonKind.icon,
              tooltip: L10n.of(context).ui_refresh_0815aad4,
              onPressed: _loading ? null : _load,
              icon: const Icon(Icons.refresh_rounded),
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: _loading ? null : _createApplication,
          icon: const Icon(Icons.add_rounded),
          label: Text(L10n.of(context).ui_new_app_bee20db6),
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
                SettingsSectionHeader(
                    L10n.of(context).ui_applications_2f7f7f22),
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
                      subtitle: L10n.of(context).ui_value0_value1_947523d9(
                          (_applications[index].status).toString(),
                          (_applications[index].ref.wire).toString()),
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
                SettingsSectionHeader(
                  L10n.of(context).ui_teams_b9c8008d,
                  subheading: L10n.of(context)
                      .ui_share_application_access_with_local_developer_f8d92919,
                ),
                SettingsRow.chevron(
                  title: L10n.of(context).ui_manage_teams_af176100,
                  subtitle: L10n.of(context)
                      .ui_value0_workspace_value1_d87206e5(
                          (_teams.length).toString(),
                          (_teams.length == 1 ? '' : 's').toString()),
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
        _error = userFacingError(error,
            summary:
                L10n.of(context).ui_could_not_load_developer_teams_a24d1429);
        _loading = false;
      });
    }
  }

  Future<void> _createTeam() async {
    final name = await showSettingsTextDialog(
      context,
      title: L10n.of(context).ui_create_a_team_3f986c5f,
      label: L10n.of(context).ui_team_name_3267531f,
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
          _error = userFacingError(error,
              summary:
                  L10n.of(context).ui_could_not_load_team_members_8175c9d9);
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
            title: Text(L10n.of(context).ui_add_team_member_70858075),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: input,
                  autofocus: true,
                  decoration: InputDecoration(
                    labelText: L10n.of(context)
                        .ui_username_or_qualified_user_id_00ed17b8,
                    hintText:
                        L10n.of(context).ui_name_instance_example_db23a0cd,
                  ),
                ),
                DropdownButtonFormField<String>(
                  initialValue: role,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_role_902b7e39),
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
              ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              ActionButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(L10n.of(context).ui_add_9dc3aa14),
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
            ActionTile(
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
            SettingsRow(
              leading: const Icon(Icons.person_remove_outlined),
              title: L10n.of(context).ui_remove_from_team_53fc9400,
              danger: true,
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
          message: L10n.of(context).ui_remove_value0_from_value1_1b8070df(
              (member.label).toString(), (team.name).toString()),
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
          title: Text(L10n.of(context).ui_developer_teams_edf19d0f),
          actions: [
            ActionButton(
              kind: ActionButtonKind.icon,
              tooltip: L10n.of(context).ui_new_team_8f6b244a,
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
              SettingsSectionHeader(L10n.of(context).ui_workspaces_f14d873f),
              if (_error case final error?)
                SettingsStatusPanel.error(message: error, onRetry: _load),
              for (final team in _teams)
                ActionTile(
                  selected: team.ref == _selected,
                  leading: Icon(team.personal
                      ? Icons.person_outline_rounded
                      : Icons.groups_outlined),
                  title: Text(team.name),
                  subtitle: Text(team.personal
                      ? L10n.of(context).ui_only_you_cc5108cc
                      : team.role),
                  onTap: () => _select(team),
                ),
              if (selected case final team?) ...[
                SettingsSectionHeader(
                  L10n.of(context)
                      .ui_value0_members_174769cf((team.name).toString()),
                  subheading: team.personal
                      ? L10n.of(context)
                          .ui_the_personal_workspace_cannot_be_shared_d7d73864
                      : L10n.of(context)
                          .ui_owners_and_administrators_can_assign_scoped_r_ad4d1d5c,
                ),
                if (_loading)
                  const Center(child: CircularProgressIndicator())
                else
                  for (final member in _members)
                    ActionTile(
                      leading: const CircleAvatar(
                        child: Icon(Icons.person_outline_rounded),
                      ),
                      title: Text(member.label),
                      subtitle: Text(L10n.of(context).ui_value0_value1_947523d9(
                          (member.ref.wire).toString(),
                          (member.role).toString())),
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
                    child: ActionButton(
                      kind: ActionButtonKind.outlined,
                      onPressed: _addMember,
                      icon: const Icon(Icons.person_add_alt_1_outlined),
                      label: Text(L10n.of(context).ui_add_member_fa100f0e),
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
        _savedSettings = _settingsDraft;
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
        _error = userFacingError(error,
            summary:
                L10n.of(context).ui_could_not_load_this_application_351a7ba1);
        _loading = false;
      });
    }
  }

  String _savedSettings = '';
  String get _settingsDraft => jsonEncode([
        _name.text.trim(),
        _nullable(_description.text),
        _nullable(_supportUrl.text),
        _nullable(_privacyUrl.text),
        _targetPolicy,
        _permissionBits.text.trim(),
        _defaultScopes.toList()..sort(),
        _defaultIntents.toList()..sort(),
        _installTypes.toList()..sort(),
        _userScopes.toList()..sort(),
        _userContexts.toList()..sort(),
        _e2eeModes.toList()..sort()
      ]);

  Future<void> _saveApplication() async {
    if (_settingsDraft == _savedSettings) return;
    final submitted = _settingsDraft;
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
        _savedSettings = submitted;
        showActionFeedback(
            context, L10n.of(context).ui_application_settings_saved_471d9be1);
      });
    } on Object catch (error) {
      _showError(
          error, L10n.of(context).ui_could_not_save_the_application_f1e596d1);
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
      if (mounted) {
        setState(() => showActionFeedback(
            context, L10n.of(context).ui_commands_published_30948651));
      }
      await _load();
    } on Object catch (error) {
      _showError(error, L10n.current.ui_could_not_publish_commands_da13d026);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _createCredential() async {
    final label = await showSettingsTextDialog(
      context,
      title: L10n.of(context).ui_create_control_credential_790e3bb9,
      label: L10n.of(context).ui_label_9eccf29d,
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
          title: Text(L10n.of(context).ui_copy_this_credential_now_a6ebbaf5),
          content: SelectableText(token),
          actions: [
            ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: token));
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                        content: Text(
                            L10n.of(context).ui_credential_copied_d89a23b6)),
                  );
                }
              },
              icon: const Icon(Icons.copy_rounded),
              label: Text(L10n.of(context).ui_copy_658f3664),
            ),
            ActionButton(
              onPressed: () => Navigator.pop(context),
              child: Text(L10n.of(context).ui_done_8dd31791),
            ),
          ],
        ),
      );
      await _load();
    } on Object catch (error) {
      _showError(error,
          L10n.current.ui_could_not_create_the_control_credential_cd09e764);
    }
  }

  Future<void> _revokeCredential(DeveloperCredential credential) async {
    if (!await showSettingsConfirmation(
      context,
      message: L10n.of(context)
          .ui_revoke_value0_8d1f4335((credential.label).toString()),
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
      _showError(
          error, L10n.current.ui_could_not_revoke_the_credential_5b11dbf9);
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
          title: Text(L10n.of(context).ui_enroll_worker_0932c39d),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: name,
                  maxLength: 100,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_worker_name_d5b53d16),
                ),
                TextField(
                  controller: publicKey,
                  autocorrect: false,
                  decoration: InputDecoration(
                    labelText: L10n.of(context).ui_ed25519_public_key_310eaf48,
                    helperText: L10n.of(context).ui_base64url_32_bytes_f4f89385,
                  ),
                ),
                TextField(
                  controller: targets,
                  autocorrect: false,
                  decoration: InputDecoration(
                    labelText: L10n.of(context).ui_target_instances_9aac8dd0,
                    helperText:
                        L10n.of(context).ui_comma_separated_hostnames_be107438,
                  ),
                ),
              ],
            ),
          ),
          actions: [
            ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => Navigator.pop(context, false),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            ActionButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text(L10n.of(context).ui_enroll_8c14fe85),
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
      _showError(error, L10n.current.ui_could_not_enroll_the_worker_41983290);
    } finally {
      name.dispose();
      publicKey.dispose();
      targets.dispose();
    }
  }

  Future<void> _revokeWorker(DeveloperWorker worker) async {
    if (!await showSettingsConfirmation(
      context,
      message: L10n.of(context)
          .ui_revoke_value0_existing_sessions_will_stop_02ae71d7(
              (worker.name).toString()),
    )) {
      return;
    }
    try {
      await repository.revokeApplicationWorker(widget.application, worker.id);
      await _load();
    } on Object catch (error) {
      _showError(error, L10n.current.ui_could_not_revoke_the_worker_23a59936);
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
            title: Text(L10n.of(context).ui_create_bot_invite_dcb54d9d),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: slug,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_link_slug_8f41bdd4),
                  ),
                  TextField(
                    controller: name,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_name_0fe07306),
                  ),
                  TextField(
                    controller: description,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_description_66de7a09),
                  ),
                  DropdownButtonFormField<String>(
                    initialValue: mode,
                    decoration: InputDecoration(
                        labelText:
                            L10n.of(context).ui_encryption_mode_65635d57),
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
              ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              ActionButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(L10n.of(context).ui_create_link_7d72271b),
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
      _showError(
          error, L10n.current.ui_could_not_create_the_invite_link_90acacf9);
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
            title: Text(L10n.of(context).ui_instance_rule_b3f83690),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: domain,
                  autofocus: true,
                  autocorrect: false,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_instance_domain_f42d351a),
                ),
                DropdownButtonFormField<String>(
                  initialValue: effect,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_effect_6ebc2674),
                  items: [
                    DropdownMenuItem(
                        value: 'allow',
                        child: Text(L10n.of(context).ui_allow_546d19d2)),
                    DropdownMenuItem(
                        value: 'deny',
                        child: Text(L10n.of(context).ui_deny_07d5fa87)),
                  ],
                  onChanged: (value) =>
                      setDialogState(() => effect = value ?? effect),
                ),
              ],
            ),
            actions: [
              ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              SaveButton(
                controllers: [domain],
                hasChanges: () => domain.text.trim().isNotEmpty,
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(L10n.of(context).ui_save_4d2d5d68),
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
      _showError(
          error, L10n.current.ui_could_not_save_the_instance_rule_3a0a2584);
    } finally {
      domain.dispose();
    }
  }

  Future<void> _deleteRule(DeveloperInstanceRule rule) async {
    if (!await showSettingsConfirmation(
      context,
      message: L10n.of(context).ui_remove_the_rule_for_value0_fe935361(
          (rule.targetDomain).toString()),
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
      _showError(
          error, L10n.current.ui_could_not_remove_the_instance_rule_8fb2482d);
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
                      ActionButton(
                        onPressed: draft.length < minimum
                            ? null
                            : () => Navigator.pop(sheetContext, draft),
                        child: Text(L10n.of(context).ui_done_8dd31791),
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
                            ? Text(L10n.of(context)
                                .ui_required_for_user_installs_a8fb5242)
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
    showActionFeedback(context, _error!, error: true);
  }

  void _showErrorMessage(String message) {
    if (mounted) {
      setState(() => _error = message);
      showActionFeedback(context, message, error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final application = _application;
    return Scaffold(
      appBar: AppBar(
        title: Text(application?.name ?? 'Application'),
        actions: [
          ActionButton(
            kind: ActionButtonKind.icon,
            tooltip: L10n.of(context).ui_refresh_0815aad4,
            onPressed: _loading ? null : _load,
            icon: const Icon(Icons.refresh_rounded),
          ),
          SaveButton(
            controllers: [
              _name,
              _description,
              _supportUrl,
              _privacyUrl,
              _permissionBits
            ],
            hasChanges: () => _settingsDraft != _savedSettings,
            kind: ActionButtonKind.text,
            onPressed: application == null || _busy ? null : _saveApplication,
            child: Text(L10n.of(context).ui_save_4d2d5d68),
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
                  if (application != null) ...[
                    Text(application.botHandle,
                        style: Theme.of(context).textTheme.bodySmall),
                    SelectableText(application.ref.wire),
                    SettingsSectionHeader(
                      L10n.of(context).ui_general_information_b6669b51,
                      subheading: L10n.of(context)
                          .ui_identity_support_links_and_application_wide_a_2bfdd5db,
                    ),
                    TextField(
                      controller: _name,
                      maxLength: 100,
                      decoration: InputDecoration(
                          labelText: L10n.of(context).ui_name_0fe07306),
                    ),
                    TextField(
                      controller: _description,
                      minLines: 2,
                      maxLines: 5,
                      maxLength: 1000,
                      decoration: InputDecoration(
                          labelText: L10n.of(context).ui_description_66de7a09),
                    ),
                    TextField(
                      controller: _supportUrl,
                      keyboardType: TextInputType.url,
                      decoration: InputDecoration(
                          labelText: L10n.of(context).ui_support_url_a70962e9),
                    ),
                    TextField(
                      controller: _privacyUrl,
                      keyboardType: TextInputType.url,
                      decoration: InputDecoration(
                          labelText: L10n.of(context).ui_privacy_url_03f968be),
                    ),
                    DropdownButtonFormField<String>(
                      initialValue: _targetPolicy,
                      decoration: InputDecoration(
                          labelText:
                              L10n.of(context).ui_target_policy_57b6cf42),
                      items: [
                        DropdownMenuItem(
                          value: 'open',
                          child: Text(
                              L10n.of(context).ui_open_federation_475dd6ca),
                        ),
                        DropdownMenuItem(
                          value: 'allowlist',
                          child:
                              Text(L10n.of(context).ui_allowlist_only_ec87392c),
                        ),
                        DropdownMenuItem(
                          value: 'blocklist',
                          child: Text(L10n.of(context)
                              .ui_open_except_blocked_instances_f08a6b6e),
                        ),
                        DropdownMenuItem(
                          value: 'local_only',
                          child: Text(
                              L10n.of(context).ui_local_instance_only_c673b853),
                        ),
                      ],
                      onChanged: (value) => setState(
                          () => _targetPolicy = value ?? _targetPolicy),
                    ),
                    ActionTile(
                      contentPadding: EdgeInsets.zero,
                      leading: Icon(Icons.key_rounded),
                      title: Text(
                          L10n.of(context).ui_default_permissions_71810cca),
                      subtitle: Text(
                        _permissionSummary,
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                      ),
                      trailing: Icon(Icons.chevron_right_rounded),
                      onTap: _busy ? null : _chooseDefaultPermissions,
                    ),
                    _MultiValueRow(
                      title: L10n.of(context).ui_gateway_scopes_c8a0d1a6,
                      values: _defaultScopes,
                      onTap: () => _chooseValues(
                        title: L10n.of(context).ui_gateway_scopes_c8a0d1a6,
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
                      title: L10n.of(context).ui_gateway_intents_08144ffe,
                      values: _defaultIntents,
                      onTap: () => _chooseValues(
                        title: L10n.of(context).ui_gateway_intents_08144ffe,
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
                      title: L10n.of(context).ui_installation_contexts_61bdf117,
                      values: _installTypes,
                      onTap: () => _chooseValues(
                        title:
                            L10n.of(context).ui_installation_contexts_61bdf117,
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
                        title: L10n.of(context).ui_user_install_scopes_88aff4ed,
                        values: _userScopes,
                        onTap: () => _chooseValues(
                          title:
                              L10n.of(context).ui_user_install_scopes_88aff4ed,
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
                        title: L10n.of(context)
                            .ui_user_install_command_contexts_2d0210ab,
                        values: _userContexts,
                        onTap: () => _chooseValues(
                          title: L10n.of(context)
                              .ui_user_install_command_contexts_2d0210ab,
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
                      title: L10n.of(context)
                          .ui_encrypted_interaction_modes_694fff23,
                      values: _e2eeModes,
                      onTap: () => _chooseValues(
                        title: L10n.of(context)
                            .ui_encrypted_interaction_modes_694fff23,
                        selected: _e2eeModes,
                        choices: const ['participant'],
                        update: (value) => setState(() => _e2eeModes = value),
                      ),
                    ),
                    Padding(
                      padding: const EdgeInsets.only(top: 12),
                      child: SaveButton(
                        controllers: [
                          _name,
                          _description,
                          _supportUrl,
                          _privacyUrl,
                          _permissionBits
                        ],
                        hasChanges: () => _settingsDraft != _savedSettings,
                        onPressed: _busy ? null : _saveApplication,
                        icon: const Icon(Icons.save_outlined),
                        label: Text(L10n.of(context)
                            .ui_save_application_settings_efb50ad7),
                      ),
                    ),
                    SettingsSectionHeader(
                        L10n.of(context).ui_commands_971550f3),
                    const SettingsInfo(
                      'Register chat-input, user and message commands with the same HTTP JSON contract used by the web portal. Limits are 100 chat-input, 15 user and 15 message commands.',
                    ),
                    TextField(
                      controller: _commands,
                      minLines: 10,
                      maxLines: 24,
                      autocorrect: false,
                      style: const TextStyle(fontFamily: 'monospace'),
                      decoration: InputDecoration(
                          labelText:
                              L10n.of(context).ui_command_json_array_7fa6b52d),
                    ),
                    Align(
                      alignment: Alignment.centerRight,
                      child: ActionButton(
                        onPressed: _busy ? null : _saveCommands,
                        icon: const Icon(Icons.publish_outlined),
                        label:
                            Text(L10n.of(context).ui_publish_commands_667259a6),
                      ),
                    ),
                    _ResourceSection(
                      title: L10n.of(context).ui_control_credentials_b091abbc,
                      actionLabel: 'New credential',
                      onAction: _createCredential,
                      children: [
                        for (final credential in _credentials)
                          ActionTile(
                            leading: const Icon(Icons.key_outlined),
                            title: Text(credential.label),
                            subtitle: Text(
                              L10n.of(context).ui_value0_value1_947523d9(
                                  (credential.tokenHint).toString(),
                                  (credential.revokedAt == null
                                          ? 'active'
                                          : 'revoked')
                                      .toString()),
                            ),
                            trailing: credential.revokedAt == null
                                ? ActionButton(
                                    kind: ActionButtonKind.icon,
                                    tooltip:
                                        L10n.of(context).ui_revoke_2219c453,
                                    onPressed: () =>
                                        _revokeCredential(credential),
                                    icon: const Icon(Icons.delete_outline),
                                  )
                                : null,
                          ),
                      ],
                    ),
                    _ResourceSection(
                      title: L10n.of(context).ui_worker_keys_5a856f3d,
                      actionLabel: 'Enroll worker',
                      onAction: _createWorker,
                      children: [
                        for (final worker in _workers)
                          ActionTile(
                            leading: const Icon(
                                Icons.precision_manufacturing_outlined),
                            title: Text(worker.name),
                            subtitle: Text(
                              L10n.of(context).ui_value0_value1_947523d9(
                                  (worker.targetDomains.isEmpty
                                          ? 'Any delegated target'
                                          : worker.targetDomains.join(', '))
                                      .toString(),
                                  (worker.revokedAt == null
                                          ? 'active'
                                          : 'revoked')
                                      .toString()),
                            ),
                            trailing: worker.revokedAt == null
                                ? ActionButton(
                                    kind: ActionButtonKind.icon,
                                    tooltip:
                                        L10n.of(context).ui_revoke_2219c453,
                                    onPressed: () => _revokeWorker(worker),
                                    icon: const Icon(Icons.delete_outline),
                                  )
                                : null,
                          ),
                      ],
                    ),
                    SettingsSectionHeader(
                        L10n.of(context).ui_application_media_4fb1e2c1),
                    SettingsRow.chevron(
                      title: L10n.of(context)
                          .ui_assets_and_application_emoji_4fe405a1,
                      subtitle: L10n.of(context)
                          .ui_upload_safety_scanned_icons_artwork_and_custo_d500c3ff,
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
                      title: L10n.of(context).ui_bot_invite_links_f2e63a12,
                      actionLabel: 'New invite',
                      onAction: _createTemplate,
                      children: [
                        for (final template in _templates)
                          ActionTile(
                            leading: const Icon(Icons.add_link_rounded),
                            title: Text(template.name),
                            subtitle: Text(
                              L10n.of(context).ui_value0_value1_value2_f1b5843f(
                                  (template.slug).toString(),
                                  (template.e2eeMode).toString(),
                                  (template.active ? 'active' : 'inactive')
                                      .toString()),
                            ),
                            trailing: ActionButton(
                              kind: ActionButtonKind.icon,
                              tooltip:
                                  L10n.of(context).ui_copy_invite_link_a7c3b223,
                              onPressed: () async {
                                await Clipboard.setData(
                                  ClipboardData(text: template.inviteUrl),
                                );
                                if (context.mounted) {
                                  ScaffoldMessenger.of(context).showSnackBar(
                                    SnackBar(
                                        content: Text(L10n.of(context)
                                            .ui_invite_link_copied_b511318e)),
                                  );
                                }
                              },
                              icon: const Icon(Icons.copy_rounded),
                            ),
                          ),
                      ],
                    ),
                    _ResourceSection(
                      title: L10n.of(context)
                          .ui_federated_instance_policy_897e8794,
                      actionLabel: 'Add rule',
                      onAction: _addRule,
                      children: [
                        for (final rule in _rules)
                          ActionTile(
                            leading: Icon(rule.effect == 'allow'
                                ? Icons.check_circle_outline
                                : Icons.block_outlined),
                            title: Text(rule.targetDomain),
                            subtitle: Text(rule.effect),
                            trailing: ActionButton(
                              kind: ActionButtonKind.icon,
                              tooltip: L10n.of(context).ui_remove_rule_eadcf4b9,
                              onPressed: () => _deleteRule(rule),
                              icon: const Icon(Icons.delete_outline),
                            ),
                          ),
                      ],
                    ),
                    _ResourceSection(
                      title: L10n.of(context).ui_installations_3215bb9a,
                      children: [
                        for (final installation in _installations)
                          ActionTile(
                            leading: const Icon(Icons.hub_outlined),
                            title: Text(installation.guildRef.wire),
                            subtitle: Text(
                              L10n.of(context)
                                  .ui_value0_value1_revision_value2_value3_scopes_v_25a51824(
                                      (installation.status).toString(),
                                      (installation.e2eeMode).toString(),
                                      (installation.grantRevision).toString(),
                                      (installation.scopes.length).toString(),
                                      (installation.channelRestrictions.isEmpty
                                              ? 'all role-permitted channels'
                                              : '${installation.channelRestrictions.length} channel restrictions')
                                          .toString()),
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
  Widget build(BuildContext context) => ActionTile(
        contentPadding: EdgeInsets.zero,
        title: Text(title),
        subtitle: Text(
          values.isEmpty
              ? L10n.of(context).ui_none_selected_acd94230
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
              child: ActionButton(
                kind: ActionButtonKind.outlined,
                onPressed: onAction,
                icon: const Icon(Icons.add_rounded),
                label: Text(actionLabel ?? 'Add'),
              ),
            ),
        ],
      );
}
