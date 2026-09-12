import 'dart:async';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_command_permissions.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class ApplicationCommandPermissionsScreen extends StatefulWidget {
  const ApplicationCommandPermissionsScreen({
    super.key,
    required this.guild,
    required this.application,
    required this.applicationName,
    required this.repository,
    required this.canManage,
  });

  final KaedeGuild guild;
  final EntityRef application;
  final String applicationName;
  final KaedeRepository repository;
  final bool canManage;

  @override
  State<ApplicationCommandPermissionsScreen> createState() =>
      _ApplicationCommandPermissionsScreenState();
}

final class _ApplicationCommandPermissionsScreenState
    extends State<ApplicationCommandPermissionsScreen> {
  List<ApplicationCommandPermissionScope> _scopes = const [];
  List<ApplicationCommandPermissionEntry> _draft = const [];
  List<GuildMember> _members = const [];
  EntityRef? _selected;
  var _loading = true;
  var _saving = false;
  String? _error;
  String? _notice;

  ApplicationCommandPermissionScope? get _scope =>
      _scopes.where((scope) => scope.id == _selected).firstOrNull;

  ApplicationCommandPermissionScope? get _applicationDefaults =>
      _scopes.where((scope) => scope.command == null).firstOrNull;

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
      final scopes = await widget.repository.applicationCommandPermissions(
        widget.application,
        widget.guild.ref,
      );
      List<GuildMember> members = const [];
      try {
        members = await widget.repository.members(widget.guild.ref);
      } on Object {
        // Member search in the add sheet can retry independently.
      }
      if (!mounted) return;
      setState(() {
        _scopes = scopes;
        _members = members;
        _select(scopes.firstOrNull);
      });
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_load_command_permissions_f8962a14,
            ));
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _select(ApplicationCommandPermissionScope? scope) {
    _selected = scope?.id;
    _draft = scope == null
        ? const []
        : scope.permissions
            .map((entry) => ApplicationCommandPermissionEntry(
                  target: entry.target,
                  type: entry.type,
                  permission: entry.permission,
                ))
            .toList(growable: false);
    _error = null;
    _notice = null;
  }

  Future<void> _add() async {
    if (!widget.canManage || _draft.length >= 100) return;
    final entry = await showModalBottomSheet<ApplicationCommandPermissionEntry>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (context) => _CommandPermissionTargetSheet(
        guild: widget.guild,
        repository: widget.repository,
        initialMembers: _members,
      ),
    );
    if (entry == null || !mounted) return;
    if (_draft.any(
        (item) => item.type == entry.type && item.target == entry.target)) {
      setState(() => _error = L10n.of(context)
          .ui_that_role_member_or_channel_already_has_an_ov_b42235fd);
      return;
    }
    setState(() {
      _draft = <ApplicationCommandPermissionEntry>[..._draft, entry];
      _error = null;
    });
  }

  void _syncWithApplication() {
    final defaults = _applicationDefaults;
    if (_scope?.command == null || defaults == null) return;
    setState(() {
      _draft = defaults.permissions
          .map((entry) => ApplicationCommandPermissionEntry(
                target: entry.target,
                type: entry.type,
                permission: entry.permission,
              ))
          .toList(growable: false);
      _notice = L10n.of(context)
          .ui_application_defaults_copied_save_to_synchroni_eefae67f;
    });
  }

  Future<void> _save() async {
    final scope = _scope;
    if (!widget.canManage || scope == null || _saving) return;
    setState(() {
      _saving = true;
      _error = null;
      _notice = null;
    });
    try {
      final saved = await widget.repository.updateApplicationCommandPermissions(
        widget.application,
        widget.guild.ref,
        scope.id,
        _draft,
      );
      if (!mounted) return;
      setState(() {
        _scopes = <ApplicationCommandPermissionScope>[
          for (final item in _scopes) item.id == saved.id ? saved : item,
        ];
        _select(saved);
        _notice = saved.synced
            ? L10n.of(context)
                .ui_this_command_now_uses_the_app_defaults_54a15a14
            : L10n.of(context).ui_command_access_updated_1c92cfb3;
      });
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_update_command_permissions_3fb16c19,
            ));
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  String _targetLabel(ApplicationCommandPermissionEntry entry) {
    if (entry.type == 'role') {
      return widget.guild.roles
              .where((role) => role.ref == entry.target)
              .firstOrNull
              ?.name ??
          entry.target.wire;
    }
    if (entry.type == 'channel') {
      if (entry.target == allChannelsPermissionRef(widget.guild.ref)) {
        return 'All channels';
      }
      final channel = widget.guild.channels
          .where((channel) => channel.ref == entry.target)
          .firstOrNull;
      return channel == null ? entry.target.wire : '#${channel.name}';
    }
    final member =
        _members.where((member) => member.user.ref == entry.target).firstOrNull;
    return member?.nickname ?? member?.user.name ?? entry.target.wire;
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
            title: Text(L10n.of(context).ui_value0_commands_d214a1ca(
                (widget.applicationName).toString()))),
        body: _loading
            ? Center(child: CircularProgressIndicator())
            : _error != null && _scopes.isEmpty
                ? _LoadError(message: _error!, onRetry: _load)
                : _scopes.isEmpty
                    ? Center(
                        child: Padding(
                          padding: EdgeInsets.all(24),
                          child: Text(
                            L10n.of(context)
                                .ui_this_app_has_no_guild_commands_to_configure_4fa678bd,
                            textAlign: TextAlign.center,
                          ),
                        ),
                      )
                    : ListView(
                        padding: EdgeInsets.fromLTRB(16, 16, 16, 32),
                        children: [
                          DropdownButtonFormField<String>(
                            key: ValueKey(_selected?.wire),
                            initialValue: _selected?.wire,
                            decoration: InputDecoration(
                                labelText:
                                    L10n.of(context).ui_command_c67c8f52),
                            items: [
                              for (final scope in _scopes)
                                DropdownMenuItem(
                                  value: scope.id.wire,
                                  child: Text(scope.label),
                                ),
                            ],
                            onChanged: _saving
                                ? null
                                : (value) => setState(() => _select(_scopes
                                    .where((scope) => scope.id.wire == value)
                                    .firstOrNull)),
                          ),
                          SizedBox(height: 12),
                          Text(
                            _scope?.command == null
                                ? L10n.of(context)
                                    .ui_default_access_inherited_by_commands_without__3483bb8b
                                : _scope!.synced
                                    ? L10n.of(context)
                                        .ui_synced_with_this_app_s_default_command_access_68b398be
                                    : L10n.of(context)
                                        .ui_this_command_has_custom_access_0171e18b,
                            style: TextStyle(color: context.kaede.muted),
                          ),
                          SizedBox(height: 12),
                          if (_draft.isEmpty)
                            Card(
                              child: Padding(
                                padding: EdgeInsets.all(14),
                                child: Text(
                                  L10n.of(context)
                                      .ui_no_role_member_or_channel_overrides_e2f2f8b7,
                                  style: TextStyle(color: context.kaede.muted),
                                ),
                              ),
                            ),
                          for (final (index, entry) in _draft.indexed)
                            Card(
                              child: ListTile(
                                title: Text(_targetLabel(entry)),
                                subtitle: Text(entry.type),
                                trailing: Wrap(
                                  crossAxisAlignment: WrapCrossAlignment.center,
                                  children: [
                                    DropdownButton<bool>(
                                      value: entry.permission,
                                      underline: SizedBox.shrink(),
                                      onChanged: !widget.canManage || _saving
                                          ? null
                                          : (value) {
                                              if (value == null) return;
                                              setState(() => _draft = [
                                                    for (final (itemIndex, item)
                                                        in _draft.indexed)
                                                      itemIndex == index
                                                          ? item.copyWith(
                                                              permission: value)
                                                          : item,
                                                  ]);
                                            },
                                      items: [
                                        DropdownMenuItem(
                                          value: true,
                                          child: Text(L10n.of(context)
                                              .ui_allow_546d19d2),
                                        ),
                                        DropdownMenuItem(
                                          value: false,
                                          child: Text(L10n.of(context)
                                              .ui_deny_07d5fa87),
                                        ),
                                      ],
                                    ),
                                    IconButton(
                                      tooltip: L10n.of(context)
                                          .ui_remove_override_54f30797,
                                      onPressed: !widget.canManage || _saving
                                          ? null
                                          : () => setState(() => _draft = [
                                                for (final (itemIndex, item)
                                                    in _draft.indexed)
                                                  if (itemIndex != index) item,
                                              ]),
                                      icon: Icon(Icons.close_rounded),
                                    ),
                                  ],
                                ),
                              ),
                            ),
                          if (widget.canManage) ...[
                            SizedBox(height: 8),
                            OutlinedButton.icon(
                              onPressed:
                                  _saving || _draft.length >= 100 ? null : _add,
                              icon: Icon(Icons.add_rounded),
                              label: Text(_draft.length >= 100
                                  ? L10n.of(context)
                                      .ui_100_overrides_maximum_601a8fdf
                                  : L10n.of(context)
                                      .ui_add_role_member_or_channel_13e42e08),
                            ),
                            if (_scope?.command != null)
                              TextButton(
                                onPressed:
                                    _saving ? null : _syncWithApplication,
                                child: Text(L10n.of(context)
                                    .ui_use_app_defaults_2c61078f),
                              ),
                            FilledButton(
                              onPressed: _saving ? null : _save,
                              child: Text(_saving
                                  ? L10n.of(context).ui_saving_bd79b37d
                                  : L10n.of(context)
                                      .ui_save_command_permissions_6f1ad435),
                            ),
                          ] else
                            Padding(
                              padding: EdgeInsets.only(top: 10),
                              child: Text(
                                L10n.of(context)
                                    .ui_manage_server_and_manage_roles_are_required_t_cce5987f,
                                style: TextStyle(color: context.kaede.muted),
                              ),
                            ),
                          if (_error case final error?)
                            Padding(
                              padding: EdgeInsets.only(top: 12),
                              child: Text(error,
                                  style:
                                      TextStyle(color: context.kaede.danger)),
                            ),
                          if (_notice case final notice?)
                            Padding(
                              padding: EdgeInsets.only(top: 12),
                              child: Text(notice,
                                  style: TextStyle(
                                      color: context.kaede.coralText)),
                            ),
                        ],
                      ),
      );
}

final class _CommandPermissionTargetSheet extends StatefulWidget {
  const _CommandPermissionTargetSheet({
    required this.guild,
    required this.repository,
    required this.initialMembers,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final List<GuildMember> initialMembers;

  @override
  State<_CommandPermissionTargetSheet> createState() =>
      _CommandPermissionTargetSheetState();
}

final class _CommandPermissionTargetSheetState
    extends State<_CommandPermissionTargetSheet> {
  var _type = 'role';
  EntityRef? _target;
  var _permission = true;
  late List<GuildMember> _members = widget.initialMembers;
  var _searching = false;
  Timer? _debounce;

  @override
  void dispose() {
    _debounce?.cancel();
    super.dispose();
  }

  Future<void> _searchMembers(String query) async {
    _debounce?.cancel();
    _debounce = Timer(Duration(milliseconds: 250), () async {
      setState(() => _searching = true);
      try {
        final members = await widget.repository.members(
          widget.guild.ref,
          query: query.trim(),
        );
        if (mounted) setState(() => _members = members);
      } on Object {
        // The authoritative save remains fail-closed if the member disappears.
      } finally {
        if (mounted) setState(() => _searching = false);
      }
    });
  }

  List<({EntityRef ref, String label})> get _options => switch (_type) {
        'role' => [
            for (final role in widget.guild.roles)
              (ref: role.ref, label: role.name),
          ],
        'channel' => [
            (
              ref: allChannelsPermissionRef(widget.guild.ref),
              label: L10n.of(context).ui_all_channels_ada7bc14
            ),
            for (final channel in widget.guild.channels)
              if (!const <ChannelType>{
                ChannelType.category,
                ChannelType.announcementThread,
                ChannelType.publicThread,
                ChannelType.privateThread,
              }.contains(channel.type))
                (
                  ref: channel.ref,
                  label: L10n.of(context).ui_value0_ea2f080f(
                      (channel.name ?? 'channel').toString())
                ),
          ],
        _ => [
            for (final member in _members)
              (
                ref: member.user.ref,
                label: member.nickname ?? member.user.name,
              ),
          ],
      };

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(
          18,
          16,
          18,
          MediaQuery.viewInsetsOf(context).bottom + 20,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(L10n.of(context).ui_add_command_access_15cb4abd,
                  style: Theme.of(context).textTheme.titleLarge),
              SizedBox(height: 14),
              DropdownButtonFormField<String>(
                initialValue: _type,
                decoration: InputDecoration(
                    labelText: L10n.of(context).ui_target_type_163b5790),
                items: [
                  DropdownMenuItem(
                      value: 'role',
                      child: Text(L10n.of(context).ui_role_902b7e39)),
                  DropdownMenuItem(
                      value: 'user',
                      child: Text(L10n.of(context).ui_member_fef5fce3)),
                  DropdownMenuItem(
                      value: 'channel',
                      child: Text(L10n.of(context).ui_channel_655d8a44)),
                ],
                onChanged: (value) => setState(() {
                  _type = value ?? 'role';
                  _target = null;
                }),
              ),
              if (_type == 'user') ...[
                SizedBox(height: 10),
                TextField(
                  decoration: InputDecoration(
                    labelText: L10n.of(context).ui_search_members_d6e1fdce,
                    suffixIcon: _searching
                        ? Padding(
                            padding: EdgeInsets.all(12),
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : Icon(Icons.search_rounded),
                  ),
                  onChanged: _searchMembers,
                ),
              ],
              SizedBox(height: 10),
              DropdownButtonFormField<String>(
                key: ValueKey('$_type-${_options.length}'),
                initialValue: _target?.wire,
                decoration: InputDecoration(
                    labelText: L10n.of(context)
                        .ui_choose_a_value0_046aff48((_type).toString())),
                items: [
                  for (final option in _options)
                    DropdownMenuItem(
                      value: option.ref.wire,
                      child: Text(option.label),
                    ),
                ],
                onChanged: (value) => setState(() =>
                    _target = value == null ? null : EntityRef.parse(value)),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(L10n.of(context).ui_allow_this_command_a0aa5f2b),
                subtitle: Text(_permission
                    ? L10n.of(context).ui_explicitly_allow_access_675d9393
                    : L10n.of(context).ui_explicitly_deny_access_3c6ed788),
                value: _permission,
                onChanged: (value) => setState(() => _permission = value),
              ),
              FilledButton(
                onPressed: _target == null
                    ? null
                    : () => Navigator.pop(
                          context,
                          ApplicationCommandPermissionEntry(
                            target: _target!,
                            type: _type,
                            permission: _permission,
                          ),
                        ),
                child: Text(L10n.of(context).ui_add_override_44154e04),
              ),
            ],
          ),
        ),
      );
}

EntityRef allChannelsPermissionRef(EntityRef guild) => EntityRef(
      Snowflake((BigInt.parse(guild.id.value) - BigInt.one).toString()),
      guild.domain,
    );

final class _LoadError extends StatelessWidget {
  const _LoadError({required this.message, required this.onRetry});

  final String message;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(message, textAlign: TextAlign.center),
              SizedBox(height: 12),
              FilledButton(
                  onPressed: onRetry,
                  child: Text(L10n.of(context).ui_retry_8036af59)),
            ],
          ),
        ),
      );
}
