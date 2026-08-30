import 'dart:async';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_command_permissions.dart';
import 'package:kaede_mobile/src/domain/models.dart';
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
              summary: 'Could not load command permissions',
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
      setState(() =>
          _error = 'That role, member, or channel already has an override.');
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
      _notice = 'Application defaults copied. Save to synchronize.';
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
            ? 'This command now uses the app defaults.'
            : 'Command access updated.';
      });
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not update command permissions',
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
        appBar: AppBar(title: Text('${widget.applicationName} commands')),
        body: _loading
            ? Center(child: CircularProgressIndicator())
            : _error != null && _scopes.isEmpty
                ? _LoadError(message: _error!, onRetry: _load)
                : _scopes.isEmpty
                    ? Center(
                        child: Padding(
                          padding: EdgeInsets.all(24),
                          child: Text(
                            'This app has no guild commands to configure.',
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
                            decoration: InputDecoration(labelText: 'Command'),
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
                                ? 'Default access inherited by commands without custom overrides.'
                                : _scope!.synced
                                    ? 'Synced with this app’s default command access.'
                                    : 'This command has custom access.',
                            style: TextStyle(color: context.kaede.muted),
                          ),
                          SizedBox(height: 12),
                          if (_draft.isEmpty)
                            Card(
                              child: Padding(
                                padding: EdgeInsets.all(14),
                                child: Text(
                                  'No role, member, or channel overrides.',
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
                                      items: const [
                                        DropdownMenuItem(
                                          value: true,
                                          child: Text('Allow'),
                                        ),
                                        DropdownMenuItem(
                                          value: false,
                                          child: Text('Deny'),
                                        ),
                                      ],
                                    ),
                                    IconButton(
                                      tooltip: 'Remove override',
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
                                  ? '100 overrides maximum'
                                  : 'Add role, member, or channel'),
                            ),
                            if (_scope?.command != null)
                              TextButton(
                                onPressed:
                                    _saving ? null : _syncWithApplication,
                                child: Text('Use app defaults'),
                              ),
                            FilledButton(
                              onPressed: _saving ? null : _save,
                              child: Text(_saving
                                  ? 'Saving…'
                                  : 'Save command permissions'),
                            ),
                          ] else
                            Padding(
                              padding: EdgeInsets.only(top: 10),
                              child: Text(
                                'Manage Server and Manage Roles are required to change command access.',
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
              label: 'All channels'
            ),
            for (final channel in widget.guild.channels)
              if (!const <ChannelType>{
                ChannelType.category,
                ChannelType.announcementThread,
                ChannelType.publicThread,
                ChannelType.privateThread,
              }.contains(channel.type))
                (ref: channel.ref, label: '#${channel.name ?? 'channel'}'),
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
              Text('Add command access',
                  style: Theme.of(context).textTheme.titleLarge),
              SizedBox(height: 14),
              DropdownButtonFormField<String>(
                initialValue: _type,
                decoration: InputDecoration(labelText: 'Target type'),
                items: const [
                  DropdownMenuItem(value: 'role', child: Text('Role')),
                  DropdownMenuItem(value: 'user', child: Text('Member')),
                  DropdownMenuItem(value: 'channel', child: Text('Channel')),
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
                    labelText: 'Search members',
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
                decoration: InputDecoration(labelText: 'Choose a $_type'),
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
                title: Text('Allow this command'),
                subtitle: Text(_permission
                    ? 'Explicitly allow access.'
                    : 'Explicitly deny access.'),
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
                child: Text('Add override'),
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
              FilledButton(onPressed: onRetry, child: Text('Retry')),
            ],
          ),
        ),
      );
}
