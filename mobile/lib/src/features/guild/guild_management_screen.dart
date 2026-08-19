import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class GuildManagementScreen extends ConsumerStatefulWidget {
  const GuildManagementScreen({required this.guild, super.key});

  final KaedeGuild guild;

  @override
  ConsumerState<GuildManagementScreen> createState() =>
      _GuildManagementScreenState();
}

final class _GuildManagementScreenState
    extends ConsumerState<GuildManagementScreen> {
  late KaedeGuild _guild = widget.guild;
  var _loading = true;
  var _selectedSection = 0;

  KaedeRepository get _repository =>
      ref.read(mobileControllerProvider.notifier).repository;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  @override
  void didUpdateWidget(covariant GuildManagementScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version) {
      _guild = widget.guild;
      _loading = true;
      unawaited(_reload());
    }
  }

  Future<void> _reload() async {
    try {
      final guild = await _repository.guild(widget.guild.ref);
      if (mounted) {
        setState(() {
          _guild = guild;
          _loading = false;
        });
      }
    } on Object catch (error) {
      _error(error);
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final mobile = ref.watch(mobileControllerProvider);
    final actorRef = mobile.user?.ref;
    final isOwner = actorRef != null && actorRef == _guild.ownerRef;
    final canManageGuild = isOwner || _guild.allows(Permission.manageGuild);
    final canManageChannels =
        isOwner || _guild.allows(Permission.manageChannels);
    final canManageRoles = isOwner || _guild.allows(Permission.manageRoles);
    final canManageMembers = isOwner ||
        _guild.allows(Permission.kickMembers) ||
        _guild.allows(Permission.banMembers) ||
        _guild.allows(Permission.moderateMembers) ||
        _guild.allows(Permission.manageNicknames) ||
        canManageRoles;
    final sections = <({String label, IconData icon, Widget page})>[
      (
        label: 'Overview',
        icon: Icons.home_outlined,
        page: _OverviewTab(
          guild: _guild,
          repository: _repository,
          changed: _changed,
          canManage: canManageGuild,
          isOwner: isOwner,
        ),
      ),
      if (canManageChannels || canManageRoles)
        (
          label: 'Channels',
          icon: Icons.tag_rounded,
          page: _ChannelsTab(
            guild: _guild,
            repository: _repository,
            changed: _changed,
            canManageChannels: canManageChannels,
            canManagePermissions: canManageRoles,
            e2eeClient: () =>
                ref.read(mobileControllerProvider.notifier).e2eeClient(),
          ),
        ),
      if (canManageRoles)
        (
          label: 'Roles',
          icon: Icons.badge_outlined,
          page: _RolesTab(
              guild: _guild,
              actorRef: actorRef,
              repository: _repository,
              changed: _changed)
        ),
      if (canManageMembers)
        (
          label: 'Members',
          icon: Icons.people_outline_rounded,
          page: _MembersTab(
              guild: _guild,
              actorRef: actorRef,
              userProfiles: mobile.userProfiles,
              repository: _repository,
              changed: _changed)
        ),
      if (isOwner ||
          _guild.allows(Permission.banMembers) ||
          _guild.allows(Permission.banInstances))
        (
          label: 'Bans',
          icon: Icons.gavel_outlined,
          page: _BansTab(
            guild: _guild,
            repository: _repository,
            canBanMembers: isOwner || _guild.allows(Permission.banMembers),
            canBanInstances: isOwner || _guild.allows(Permission.banInstances),
          )
        ),
      if (isOwner || _guild.allows(Permission.createInvite) || canManageGuild)
        (
          label: 'Invites',
          icon: Icons.person_add_alt_1_rounded,
          page: _InvitesTab(
            guild: _guild,
            repository: _repository,
            canCreate: isOwner || _guild.allows(Permission.createInvite),
            canManage: canManageGuild,
          )
        ),
      if (isOwner || _guild.allows(Permission.manageEmojis))
        (
          label: 'Emoji',
          icon: Icons.emoji_emotions_outlined,
          page: _EmojiTab(
            guild: _guild,
            repository: _repository,
            canManage: isOwner || _guild.allows(Permission.manageEmojis),
          )
        ),
      if (isOwner || _guild.allows(Permission.manageWebhooks))
        (
          label: 'Webhooks',
          icon: Icons.webhook_rounded,
          page: _WebhooksTab(
            guild: _guild,
            repository: _repository,
            canManage: isOwner || _guild.allows(Permission.manageWebhooks),
          )
        ),
      if (isOwner || _guild.allows(Permission.viewAuditLog))
        (
          label: 'Audit',
          icon: Icons.receipt_long_outlined,
          page: _AuditTab(
            guild: _guild,
            repository: _repository,
            canView: isOwner || _guild.allows(Permission.viewAuditLog),
          )
        ),
    ];
    if (_selectedSection >= sections.length) _selectedSection = 0;
    final title = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(_guild.name),
        const Text('Guild settings',
            style: TextStyle(fontSize: 12, color: KaedeColors.muted)),
      ],
    );

    return LayoutBuilder(builder: (context, constraints) {
      if (constraints.maxWidth >= 900) {
        return Scaffold(
          appBar: AppBar(title: title),
          body: _loading
              ? const Center(child: CircularProgressIndicator())
              : Row(
                  children: [
                    NavigationRail(
                      extended: constraints.maxWidth >= 1120,
                      selectedIndex: _selectedSection,
                      onDestinationSelected: (value) =>
                          setState(() => _selectedSection = value),
                      destinations: [
                        for (final section in sections)
                          NavigationRailDestination(
                            icon: Icon(section.icon),
                            label: Text(section.label),
                          ),
                      ],
                    ),
                    const VerticalDivider(width: 1),
                    Expanded(child: sections[_selectedSection].page),
                  ],
                ),
        );
      }
      return Scaffold(
        appBar: AppBar(title: title),
        body: _loading
            ? const Center(child: CircularProgressIndicator())
            : ListView.separated(
                padding: const EdgeInsets.fromLTRB(12, 10, 12, 30),
                itemCount: sections.length,
                separatorBuilder: (_, __) => const SizedBox(height: 6),
                itemBuilder: (context, index) {
                  final section = sections[index];
                  return Card(
                    margin: EdgeInsets.zero,
                    child: ListTile(
                      leading: Icon(section.icon, color: KaedeColors.coral),
                      title: Text(section.label,
                          style: const TextStyle(fontWeight: FontWeight.w800)),
                      trailing: const Icon(Icons.chevron_right_rounded),
                      onTap: () => Navigator.of(context).push<void>(
                        MaterialPageRoute<void>(
                          builder: (context) => Scaffold(
                            appBar: AppBar(title: Text(section.label)),
                            body: section.page,
                          ),
                        ),
                      ),
                    ),
                  );
                },
              ),
      );
    });
  }

  Future<KaedeGuild> _changed([String? message]) async {
    await _reload();
    await ref.read(mobileControllerProvider.notifier).refreshNavigation();
    if (message != null && mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
    }
    return _guild;
  }

  void _error(Object error) {
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not load the guild settings',
          )),
          backgroundColor: KaedeColors.danger));
    }
  }
}

final class _OverviewTab extends StatefulWidget {
  const _OverviewTab({
    required this.guild,
    required this.repository,
    required this.changed,
    required this.canManage,
    required this.isOwner,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final Future<KaedeGuild> Function([String?]) changed;
  final bool canManage;
  final bool isOwner;

  @override
  State<_OverviewTab> createState() => _OverviewTabState();
}

final class _OverviewTabState extends State<_OverviewTab> {
  late KaedeGuild _guild = widget.guild;
  late final _name = TextEditingController(text: widget.guild.name);
  late final _description =
      TextEditingController(text: widget.guild.description ?? '');
  late var _history = widget.guild.federatedHistoryPolicy;
  var _notification = 'mentions';
  String? _notificationError;
  var _busy = false;

  @override
  void initState() {
    super.initState();
    _loadNotificationSettings();
  }

  @override
  void didUpdateWidget(covariant _OverviewTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version) {
      _guild = widget.guild;
      _name.text = widget.guild.name;
      _description.text = widget.guild.description ?? '';
      _history = widget.guild.federatedHistoryPolicy;
      if (oldWidget.guild.ref != widget.guild.ref) {
        _notification = 'mentions';
        _notificationError = null;
        _loadNotificationSettings();
      }
    }
  }

  Future<void> _loadNotificationSettings() async {
    try {
      final value =
          await widget.repository.guildNotificationSettings(widget.guild.ref);
      if (!mounted) return;
      setState(() {
        _notification = '${value['level'] ?? 'mentions'}';
        _notificationError = null;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _notificationError = userFacingError(
          error,
          summary:
              'This guild’s notification preference could not be loaded. Mentions is shown as a temporary default.',
        );
      });
    }
  }

  @override
  void dispose() {
    _name.dispose();
    _description.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => _PageList(children: [
        _Panel(
          title: 'Guild profile',
          subtitle:
              'Identity and presentation are federated to every joined instance.',
          child: Column(children: [
            Row(children: [
              GuildIcon(guild: _guild, size: 68),
              const SizedBox(width: 12),
              Expanded(
                  child: OutlinedButton.icon(
                      onPressed: _busy || !widget.canManage
                          ? null
                          : () => _asset('icon'),
                      icon: const Icon(Icons.image_outlined),
                      label: const Text('Change icon'))),
              const SizedBox(width: 8),
              Expanded(
                  child: OutlinedButton.icon(
                      onPressed: _busy || !widget.canManage
                          ? null
                          : () => _asset('banner'),
                      icon: const Icon(Icons.panorama_outlined),
                      label: const Text('Change banner'))),
            ]),
            const SizedBox(height: 14),
            TextField(
                controller: _name,
                enabled: widget.canManage,
                decoration: const InputDecoration(labelText: 'Guild name')),
            const SizedBox(height: 12),
            TextField(
                controller: _description,
                minLines: 3,
                maxLines: 6,
                maxLength: 500,
                enabled: widget.canManage,
                decoration: const InputDecoration(labelText: 'Description')),
            const SizedBox(height: 8),
            DropdownButtonFormField<String>(
              key: ValueKey(
                'guild-history-${_guild.ref.wire}-${_guild.version}-$_history',
              ),
              initialValue: _history,
              decoration:
                  const InputDecoration(labelText: 'Federated message history'),
              items: const [
                DropdownMenuItem(
                    value: 'disabled',
                    child: Text('Disabled (recommended default)')),
                DropdownMenuItem(
                    value: 'full_retained',
                    child: Text('Share permitted history')),
              ],
              onChanged: widget.canManage
                  ? (value) => setState(() => _history = value ?? _history)
                  : null,
            ),
            const SizedBox(height: 14),
            Align(
                alignment: Alignment.centerRight,
                child: FilledButton.icon(
                    onPressed: _busy || !widget.canManage ? null : _save,
                    icon: const Icon(Icons.save_outlined),
                    label: const Text('Save changes'))),
          ]),
        ),
        _Panel(
          title: 'Notifications',
          child: Column(children: [
            if (_notificationError case final warning?) ...[
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.warning_amber_rounded,
                    color: KaedeColors.warning),
                title: Text(warning),
                trailing: TextButton(
                  onPressed: _loadNotificationSettings,
                  child: const Text('Retry'),
                ),
              ),
              const SizedBox(height: 8),
            ],
            SegmentedButton<String>(
              segments: const [
                ButtonSegment(value: 'all', label: Text('All messages')),
                ButtonSegment(value: 'mentions', label: Text('Mentions')),
                ButtonSegment(value: 'none', label: Text('Nothing')),
              ],
              selected: {_notification},
              onSelectionChanged: (value) async {
                final previous = _notification;
                setState(() => _notification = value.first);
                try {
                  await widget.repository.updateGuildNotificationSettings(
                      widget.guild.ref, value.first);
                  if (mounted) setState(() => _notificationError = null);
                } on Object catch (error) {
                  if (!mounted) return;
                  setState(() => _notification = previous);
                  _tabError(
                    this.context,
                    'Could not update guild notifications',
                    error,
                  );
                }
              },
            ),
          ]),
        ),
        _Panel(
          title: 'Ownership',
          subtitle:
              'Remote instances keep untrusted replicas. Deletion and cache purges are best effort once data has federated.',
          child: Wrap(spacing: 10, runSpacing: 10, children: [
            OutlinedButton.icon(
                onPressed: widget.isOwner ? _transfer : null,
                icon: const Icon(Icons.swap_horiz_rounded),
                label: const Text('Transfer ownership')),
            OutlinedButton.icon(
                onPressed: _leave,
                icon: const Icon(Icons.logout_rounded),
                label: const Text('Leave guild')),
            FilledButton.tonalIcon(
                onPressed: widget.isOwner ? _delete : null,
                icon: const Icon(Icons.delete_forever_outlined),
                label: const Text('Delete guild')),
          ]),
        ),
      ]);

  Future<void> _save() async {
    setState(() => _busy = true);
    try {
      await widget.repository.updateGuild(_guild.ref, _guild.version ?? '*', {
        'name': _name.text.trim(),
        'description':
            _description.text.trim().isEmpty ? null : _description.text.trim(),
        'federated_history_policy': _history,
      });
      final updated = await widget.changed('Guild saved');
      if (mounted) setState(() => _guild = updated);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not save guild', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _asset(String kind) async {
    final file = await ImagePicker().pickImage(
        source: ImageSource.gallery, maxWidth: 4096, maxHeight: 4096);
    if (file == null) return;
    if (!mounted) return;
    final contentType =
        imageUploadContentType(file.name, reportedType: file.mimeType);
    if (contentType == null) {
      _tabError(context, 'Could not update guild $kind',
          'Choose a PNG, JPEG, GIF, or WebP image.');
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.repository.uploadGuildAsset(
        guild: _guild.ref,
        kind: kind,
        filename: file.name,
        contentType: contentType,
        file: File(file.path),
      );
      final updated = await widget.changed('Guild $kind updated');
      if (mounted) setState(() => _guild = updated);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not update guild $kind', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _transfer() async {
    final value = await _prompt(
        context, 'Transfer ownership', 'Local member ID',
        warning:
            'Ownership can only be transferred to a member on this guild’s home instance.');
    if (value == null) return;
    try {
      late final EntityRef member;
      try {
        member = EntityRef.parse(value, localDomain: _guild.ref.domain);
      } on FormatException {
        throw const UserInputException(
          'Enter a valid member ID or full member reference.',
        );
      }
      await widget.repository.transferGuild(
        _guild.ref,
        member,
        _guild.version ?? '*',
      );
      final updated = await widget.changed('Ownership transferred');
      if (mounted) setState(() => _guild = updated);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not transfer ownership', error);
    }
  }

  Future<void> _leave() async {
    if (!await _confirm(context, 'Leave ${_guild.name}?',
        'You will lose access unless invited again.')) {
      return;
    }
    try {
      await widget.repository.leaveGuild(_guild.ref);
      if (mounted) Navigator.pop(context);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not leave guild', error);
    }
  }

  Future<void> _delete() async {
    if (!await _confirm(context, 'Permanently delete guild?',
        'This destroys the authoritative guild. Remote deletion is best effort.',
        destructive: true)) {
      return;
    }
    try {
      await widget.repository.deleteGuild(_guild.ref, _guild.version ?? '*');
      if (mounted) Navigator.pop(context);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not delete guild', error);
    }
  }
}

final class _ChannelsTab extends StatefulWidget {
  const _ChannelsTab({
    required this.guild,
    required this.repository,
    required this.changed,
    required this.canManageChannels,
    required this.canManagePermissions,
    required this.e2eeClient,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final Future<KaedeGuild> Function([String?]) changed;
  final bool canManageChannels;
  final bool canManagePermissions;
  final Future<MobileE2EEClient> Function() e2eeClient;
  @override
  State<_ChannelsTab> createState() => _ChannelsTabState();
}

final class _ChannelsTabState extends State<_ChannelsTab> {
  late List<KaedeChannel> _channels = [...widget.guild.channels]
    ..sort((a, b) => a.position.compareTo(b.position));
  var _e2eeActivationEnabled = false;

  @override
  void initState() {
    super.initState();
    _loadE2eeActivation();
  }

  Future<void> _loadE2eeActivation() async {
    final instance = widget.repository.api.tokens?.instance;
    if (instance == null) return;
    try {
      final configuration = await widget.repository.authConfig(instance);
      if (mounted) {
        setState(() => _e2eeActivationEnabled =
            configuration['e2ee_activation_enabled'] == true);
      }
    } on Object {
      // Activation stays hidden when the instance configuration cannot be verified.
    }
  }

  @override
  void didUpdateWidget(covariant _ChannelsTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        !identical(oldWidget.guild.channels, widget.guild.channels)) {
      _channels = [...widget.guild.channels]
        ..sort((a, b) => a.position.compareTo(b.position));
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: Colors.transparent,
        body: ReorderableListView.builder(
          buildDefaultDragHandles: false,
          padding: const EdgeInsets.all(14),
          header: const Padding(
              padding: EdgeInsets.fromLTRB(4, 4, 4, 14),
              child: Text(
                  'Drag channels and categories to reorder them. Category children retain their parent.')),
          itemCount: _channels.length,
          onReorder: widget.canManageChannels ? _reorder : (_, __) {},
          itemBuilder: (context, index) {
            final channel = _channels[index];
            return Card(
              key: ValueKey(channel.ref.wire),
              child: ListTile(
                leading: Icon(switch (channel.type) {
                  ChannelType.category => Icons.folder_outlined,
                  ChannelType.voice => Icons.volume_up_outlined,
                  _ => Icons.tag_rounded
                }),
                title: Text(channel.name ?? 'channel'),
                subtitle: Text(channel.type.name),
                trailing: _channelActions(channel, index),
                onTap: widget.canManageChannels ? () => _edit(channel) : null,
              ),
            );
          },
        ),
        floatingActionButton: FloatingActionButton.extended(
            onPressed: widget.canManageChannels ? _create : null,
            icon: const Icon(Icons.add_rounded),
            label: const Text('Channel')),
      );

  Widget _channelActions(KaedeChannel channel, int index) {
    if (!widget.canManageChannels && !widget.canManagePermissions) {
      return const Icon(Icons.lock_outline_rounded);
    }
    return Row(mainAxisSize: MainAxisSize.min, children: [
      PopupMenuButton<String>(
        onSelected: (action) => action == 'permissions'
            ? _permissions(channel)
            : action == 'encryption'
                ? _encryption(channel)
                : action == 'delete'
                    ? _delete(channel)
                    : _edit(channel),
        itemBuilder: (_) => [
          if (widget.canManageChannels)
            const PopupMenuItem(value: 'edit', child: Text('Edit channel')),
          if (widget.canManagePermissions)
            const PopupMenuItem(
                value: 'permissions', child: Text('Permissions')),
          if (widget.canManageChannels &&
              {ChannelType.text, ChannelType.announcement, ChannelType.voice}
                  .contains(channel.type) &&
              (channel.encryptionMode == 'e2ee' || _e2eeActivationEnabled))
            PopupMenuItem(
              value: 'encryption',
              child: Text(channel.encryptionMode == 'e2ee'
                  ? 'Encryption settings'
                  : 'Enable encryption'),
            ),
          if (widget.canManageChannels)
            const PopupMenuItem(
                value: 'delete',
                child: Text('Delete',
                    style: TextStyle(color: KaedeColors.danger))),
        ],
      ),
      if (widget.canManageChannels)
        ReorderableDragStartListener(
          index: index,
          child: const Padding(
            padding: EdgeInsets.all(12),
            child: Icon(Icons.drag_handle_rounded),
          ),
        ),
    ]);
  }

  Future<void> _reorder(int oldIndex, int newIndex) async {
    if (newIndex > oldIndex) newIndex--;
    final previous = [..._channels];
    setState(() {
      final item = _channels.removeAt(oldIndex);
      _channels.insert(newIndex, item);
    });
    try {
      await widget.repository.reorderChannels(
        widget.guild.ref,
        guildChannelPositionRequest(_channels),
      );
      await widget.changed();
    } on Object catch (error) {
      if (!mounted) return;
      setState(() => _channels = previous);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not reorder the channels',
        )),
        backgroundColor: KaedeColors.danger,
      ));
    }
  }

  Future<void> _create() async {
    final value = await showGuildChannelEditorSheet(
      context,
      channels: _channels,
    );
    if (value == null) return;
    try {
      final created =
          await widget.repository.createChannel(widget.guild.ref, value.json);
      if (!mounted) return;
      _upsertChannel(created);
      final refreshed = await widget.changed('Channel created');
      if (mounted) _reconcileChannels(refreshed, preserve: created);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create channel', error);
    }
  }

  Future<void> _edit(KaedeChannel channel) async {
    final value = await showGuildChannelEditorSheet(
      context,
      channel: channel,
      channels: _channels,
    );
    if (value == null) return;
    try {
      final updated = await widget.repository.updateChannel(
          widget.guild.ref, channel.ref, channel.version ?? '*', value.json);
      if (!mounted) return;
      _upsertChannel(updated);
      final refreshed = await widget.changed('Channel saved');
      if (mounted) _reconcileChannels(refreshed, preserve: updated);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not save channel', error);
    }
  }

  Future<void> _delete(KaedeChannel channel) async {
    if (!await _confirm(context, 'Delete #${channel.name}?',
        'Messages in this channel will no longer be accessible.',
        destructive: true)) {
      return;
    }
    try {
      await widget.repository
          .deleteChannel(widget.guild.ref, channel.ref, channel.version ?? '*');
      if (!mounted) return;
      setState(() => _channels.removeWhere((item) => item.ref == channel.ref));
      final refreshed = await widget.changed('Channel deleted');
      if (mounted) {
        setState(() {
          _channels = [...refreshed.channels]
            ..removeWhere((item) => item.ref == channel.ref)
            ..sort((a, b) => a.position.compareTo(b.position));
        });
      }
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not delete channel', error);
    }
  }

  void _upsertChannel(KaedeChannel channel) {
    setState(() {
      _channels = [
        for (final existing in _channels)
          if (existing.ref != channel.ref) existing,
        channel,
      ]..sort((a, b) => a.position.compareTo(b.position));
    });
  }

  void _reconcileChannels(KaedeGuild guild, {KaedeChannel? preserve}) {
    final channels = [...guild.channels];
    if (preserve != null) {
      channels.removeWhere((channel) => channel.ref == preserve.ref);
      channels.add(preserve);
    }
    setState(() =>
        _channels = channels..sort((a, b) => a.position.compareTo(b.position)));
  }

  Future<void> _encryption(KaedeChannel channel) async {
    final encrypted = channel.encryptionMode == 'e2ee';
    final needsRekey =
        encrypted && {'rekeying', 'failed'}.contains(channel.encryptionState);
    String? safetyNumber;
    String? error;
    var busy = false;
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialogState) {
          Future<void> run(
              Future<void> Function(MobileE2EEClient) action) async {
            if (busy) return;
            setDialogState(() {
              busy = true;
              error = null;
            });
            try {
              final client = await widget.e2eeClient();
              await action(client);
            } on Object catch (caught) {
              error = userFacingError(
                caught,
                summary: 'Could not update end-to-end encryption.',
              );
            } finally {
              if (dialogContext.mounted) {
                setDialogState(() => busy = false);
              }
            }
          }

          final media = channel.type == ChannelType.voice;
          return AlertDialog(
            title: Text(encrypted
                ? 'End-to-end encryption'
                : 'Enable end-to-end encryption?'),
            content: SizedBox(
              width: 520,
              child: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(encrypted
                        ? needsRekey
                            ? 'Encrypted activity is paused until a manager rotates the room keys.'
                            : 'Encryption is ${channel.encryptionState}.'
                        : media
                            ? 'Voice, video, screen video, and screen audio will be encrypted on participant devices. The media relay still sees routing, timing, track, traffic, and participant metadata.'
                            : 'New messages and files will be encrypted on participant devices. Existing history remains plaintext.'),
                    const SizedBox(height: 12),
                    const Text(
                      'Until members compare the channel safety number through a separate trusted channel, content is encrypted but identities are unverified. Comparing it is what detects first-contact or active-instance key substitution.',
                    ),
                    const SizedBox(height: 12),
                    Text(media
                        ? 'Server recording, transcription, media moderation, and unsupported clients will be unavailable. A participant can still record on their own device. This change cannot be reversed.'
                        : 'Search, link previews, bots, webhooks, server file previews, and malware scanning will be unavailable. Push wakes contain no message text, but participants, timing, and message-size metadata remain visible. Losing the synchronized encrypted vault, every trusted client’s local state, and the recovery backup permanently loses encrypted history. Removed members retain content already received. This change cannot be reversed.'),
                    if (safetyNumber != null) ...[
                      const SizedBox(height: 14),
                      const Text('Channel safety number',
                          style: TextStyle(fontWeight: FontWeight.w800)),
                      SelectableText(safetyNumber!),
                      const SizedBox(height: 6),
                      const Text(
                          'Compare this with members through a trusted channel. It changes after membership or device changes.'),
                    ],
                    if (error != null) ...[
                      const SizedBox(height: 12),
                      Text(error!,
                          style: const TextStyle(color: KaedeColors.coral)),
                    ],
                  ],
                ),
              ),
            ),
            actions: [
              TextButton(
                onPressed: busy ? null : () => Navigator.pop(dialogContext),
                child: const Text('Done'),
              ),
              if (encrypted && channel.encryptionState == 'active')
                FilledButton.tonal(
                  onPressed: busy
                      ? null
                      : () => run((client) async {
                            await client.syncRoomState(channel);
                            final value = await client.safetyNumber(channel);
                            if (dialogContext.mounted) {
                              setDialogState(() => safetyNumber = value);
                            }
                          }),
                  child: const Text('Verify safety number'),
                ),
              if (!encrypted || needsRekey)
                FilledButton.icon(
                  onPressed: busy
                      ? null
                      : () => run((client) async {
                            final updated = needsRekey
                                ? await client.rekeyRoom(channel)
                                : await client.enableRoom(channel);
                            if (!needsRekey) {
                              final accountRef =
                                  widget.repository.api.tokens?.userRef?.wire;
                              if (accountRef != null) {
                                await acknowledgeEncryptedRoom(
                                  accountRef,
                                  updated.ref.wire,
                                );
                              }
                            }
                            if (!dialogContext.mounted) return;
                            Navigator.pop(dialogContext);
                            await widget.changed(needsRekey
                                ? 'Encryption keys rotated'
                                : 'End-to-end encryption enabled');
                            if (mounted) {
                              setState(() {
                                final index = _channels.indexWhere(
                                    (item) => item.ref == channel.ref);
                                if (index >= 0) _channels[index] = updated;
                              });
                            }
                          }),
                  icon: Icon(needsRekey
                      ? Icons.sync_lock_rounded
                      : Icons.lock_rounded),
                  label: Text(needsRekey ? 'Rotate keys' : 'Enable'),
                ),
            ],
          );
        },
      ),
    );
  }

  Future<void> _permissions(KaedeChannel channel) async {
    late final List<GuildMember> members;
    late final List<Map<String, Object?>> existing;
    try {
      members = await widget.repository.members(widget.guild.ref);
      existing =
          await widget.repository.overwrites(widget.guild.ref, channel.ref);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not load permissions', error);
      return;
    }
    if (!mounted) return;
    await Navigator.push(
        context,
        MaterialPageRoute<void>(
            builder: (_) => _PermissionScreen(
                  guild: widget.guild,
                  channel: channel,
                  members: members,
                  existing: existing,
                  repository: widget.repository,
                )));
  }
}

final class _RolesTab extends StatefulWidget {
  const _RolesTab(
      {required this.guild,
      required this.actorRef,
      required this.repository,
      required this.changed});
  final KaedeGuild guild;
  final EntityRef? actorRef;
  final KaedeRepository repository;
  final Future<KaedeGuild> Function([String?]) changed;
  @override
  State<_RolesTab> createState() => _RolesTabState();
}

final class _RolesTabState extends State<_RolesTab> {
  late List<KaedeRole> _roles = [...widget.guild.roles]
    ..sort((a, b) => b.position.compareTo(a.position));

  @override
  void didUpdateWidget(covariant _RolesTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        !identical(oldWidget.guild.roles, widget.guild.roles)) {
      _roles = [...widget.guild.roles]
        ..sort((a, b) => b.position.compareTo(a.position));
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: Colors.transparent,
        body: ReorderableListView.builder(
          buildDefaultDragHandles: false,
          padding: const EdgeInsets.all(14),
          header: const Padding(
              padding: EdgeInsets.all(4),
              child: Text(
                  'Higher roles can manage only roles and members below them. Drag to reorder; changes save immediately.')),
          itemCount: _roles.length,
          onReorder: (oldIndex, newIndex) async {
            if (!_canMove(_roles[oldIndex])) return;
            if (newIndex > oldIndex) newIndex--;
            final firstMovable = _roles.indexWhere(_canMove);
            final everyoneIndex = _roles.indexWhere(_isEveryoneRole);
            final lastMovable =
                everyoneIndex < 0 ? _roles.length - 1 : everyoneIndex - 1;
            if (firstMovable < 0 ||
                newIndex < firstMovable ||
                newIndex > lastMovable) {
              if (mounted) {
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                  content: Text(
                      'Roles cannot be moved above your role ceiling or below @everyone.'),
                ));
              }
              return;
            }
            final previous = [..._roles];
            setState(() {
              final role = _roles.removeAt(oldIndex);
              _roles.insert(newIndex, role);
            });
            try {
              await widget.repository.reorderRoles(
                widget.guild.ref,
                _roles.reversed
                    .where((role) => !_isEveryoneRole(role))
                    .toList(),
              );
              await widget.changed();
            } on Object catch (error) {
              if (!context.mounted) return;
              setState(() => _roles = previous);
              ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                content: Text(userFacingError(
                  error,
                  summary: 'Could not reorder the roles',
                )),
                backgroundColor: KaedeColors.danger,
              ));
            }
          },
          itemBuilder: (_, index) {
            final role = _roles[index];
            return Card(
                key: ValueKey(role.ref.wire),
                child: ListTile(
                  leading: CircleAvatar(
                      radius: 10,
                      backgroundColor: role.color == 0
                          ? KaedeColors.muted
                          : Color(0xFF000000 | role.color)),
                  title: Text(role.name),
                  subtitle: Text(
                      '${role.position} · ${role.hoist ? 'Displayed separately' : 'Standard'}'),
                  trailing: _canMove(role)
                      ? ReorderableDragStartListener(
                          index: index,
                          child: const Padding(
                            padding: EdgeInsets.all(12),
                            child: Icon(Icons.drag_handle_rounded),
                          ),
                        )
                      : const Tooltip(
                          message: 'This role is above your role ceiling',
                          child: Icon(Icons.lock_outline_rounded),
                        ),
                  onTap: _canMove(role) ? () => _edit(role) : null,
                ));
          },
        ),
        floatingActionButton: FloatingActionButton.extended(
            onPressed: widget.actorRef == widget.guild.ownerRef ||
                    widget.guild.allows(Permission.manageRoles)
                ? () => _edit(null)
                : null,
            icon: const Icon(Icons.add_rounded),
            label: const Text('Role')),
      );

  bool _canMove(KaedeRole role) {
    if (_isEveryoneRole(role)) return false;
    if (widget.actorRef == widget.guild.ownerRef) return true;
    final ceiling = _actorRolePosition(widget.guild);
    return widget.guild.allows(Permission.manageRoles) &&
        ceiling != null &&
        role.position < ceiling;
  }

  bool _isEveryoneRole(KaedeRole role) =>
      role.position == 0 || role.ref == widget.guild.ref;

  Future<void> _edit(KaedeRole? role) async {
    final draft = await Navigator.push<_RoleDraft>(
        context, MaterialPageRoute(builder: (_) => _RoleEditor(role: role)));
    if (draft == null) return;
    KaedeRole? saved;
    try {
      if (draft.delete && role != null) {
        await widget.repository.deleteRole(widget.guild.ref, role);
        if (!mounted) return;
        setState(() => _roles.removeWhere((item) => item.ref == role.ref));
      } else if (role == null) {
        final created =
            await widget.repository.createRole(widget.guild.ref, draft.json);
        if (!mounted) return;
        saved = created;
        _upsertRole(created);
      } else {
        final updated = await widget.repository
            .updateRole(widget.guild.ref, role, draft.json);
        if (!mounted) return;
        saved = updated;
        _upsertRole(updated);
      }
      final message = draft.delete
          ? 'Role deleted'
          : role == null
              ? 'Role created'
              : 'Role saved';
      final refreshed = await widget.changed(message);
      if (mounted) {
        _reconcileRoles(
          refreshed,
          removed: draft.delete ? role?.ref : null,
          preserve: saved,
        );
      }
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not update role', error);
    }
  }

  void _upsertRole(KaedeRole role) {
    setState(() {
      _roles = [
        for (final existing in _roles)
          if (existing.ref != role.ref) existing,
        role,
      ]..sort((a, b) => b.position.compareTo(a.position));
    });
  }

  void _reconcileRoles(
    KaedeGuild guild, {
    EntityRef? removed,
    KaedeRole? preserve,
  }) {
    final refreshed = [
      for (final role in guild.roles)
        if (role.ref != removed && role.ref != preserve?.ref) role,
      if (preserve != null) preserve,
    ];
    setState(() =>
        _roles = refreshed..sort((a, b) => b.position.compareTo(a.position)));
  }
}

final class _MembersTab extends StatefulWidget {
  const _MembersTab(
      {required this.guild,
      required this.actorRef,
      required this.userProfiles,
      required this.repository,
      required this.changed});
  final KaedeGuild guild;
  final EntityRef? actorRef;
  final Map<EntityRef, KaedeUser> userProfiles;
  final KaedeRepository repository;
  final Future<KaedeGuild> Function([String?]) changed;
  @override
  State<_MembersTab> createState() => _MembersTabState();
}

final class _MembersTabState extends State<_MembersTab> {
  final _search = TextEditingController();
  final _scroll = ScrollController();
  List<GuildMember> _members = const [];
  var _loading = true;
  var _loadingMore = false;
  var _hasMore = true;
  var _requestGeneration = 0;
  Timer? _searchDebounce;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_maybeLoadMore);
    _search.addListener(_searchChanged);
    _load(reset: true);
  }

  @override
  void didUpdateWidget(covariant _MembersTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version) {
      unawaited(_load(reset: true));
    }
  }

  @override
  void dispose() {
    _searchDebounce?.cancel();
    _scroll
      ..removeListener(_maybeLoadMore)
      ..dispose();
    _search.removeListener(_searchChanged);
    _search.dispose();
    super.dispose();
  }

  void _searchChanged() {
    _searchDebounce?.cancel();
    _searchDebounce = Timer(
      const Duration(milliseconds: 300),
      () => unawaited(_load(reset: true)),
    );
  }

  void _maybeLoadMore() {
    if (_scroll.position.extentAfter < 480 &&
        !_loading &&
        !_loadingMore &&
        _hasMore) {
      unawaited(_load(reset: false));
    }
  }

  Future<void> _load({required bool reset}) async {
    if (!reset && (_loadingMore || !_hasMore)) return;
    final generation = reset ? ++_requestGeneration : _requestGeneration;
    final query = _search.text.trim();
    if (reset) {
      setState(() {
        _loading = true;
        _hasMore = true;
      });
    } else {
      setState(() => _loadingMore = true);
    }
    try {
      final data = await widget.repository.members(
        widget.guild.ref,
        query: query,
        after: reset || _members.isEmpty ? null : _members.last.user.ref,
      );
      if (!mounted || generation != _requestGeneration) return;
      final known = reset
          ? <EntityRef>{}
          : _members.map((member) => member.user.ref).toSet();
      setState(() {
        _members = <GuildMember>[
          if (!reset) ..._members,
          ...data.where((member) => known.add(member.user.ref)),
        ];
        _hasMore = data.length == 100;
      });
    } on Object catch (error) {
      if (mounted && generation == _requestGeneration) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(userFacingError(
              error,
              summary: 'Could not load the members',
            )),
            backgroundColor: KaedeColors.danger,
          ),
        );
      }
    } finally {
      if (mounted && generation == _requestGeneration) {
        setState(() {
          _loading = false;
          _loadingMore = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => Column(children: [
        Padding(
            padding: const EdgeInsets.all(14),
            child: SearchBar(
                controller: _search,
                hintText: 'Search members',
                leading: const Icon(Icons.search_rounded),
                trailing: [
                  if (_search.text.isNotEmpty)
                    IconButton(
                        tooltip: 'Clear search',
                        onPressed: _search.clear,
                        icon: const Icon(Icons.close_rounded))
                ],
                onSubmitted: (_) => _load(reset: true))),
        Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : RefreshIndicator(
                    onRefresh: () => _load(reset: true),
                    child: ListView.builder(
                      controller: _scroll,
                      physics: const AlwaysScrollableScrollPhysics(),
                      itemCount: _members.length + (_loadingMore ? 1 : 0),
                      itemBuilder: (_, index) {
                        if (index == _members.length) {
                          return const Padding(
                            padding: EdgeInsets.all(20),
                            child: Center(child: CircularProgressIndicator()),
                          );
                        }
                        final member = overlayGuildMemberProfile(
                          _members[index],
                          widget.userProfiles,
                        );
                        final actions = _actionsFor(member);
                        return ListTile(
                          leading: UserAvatar(user: member.user),
                          title: Text(member.nickname ?? member.user.name),
                          subtitle: Text(member.user.profileResolved
                              ? member.user.handle
                              : 'Profile unavailable · refreshes automatically'),
                          trailing: actions.isEmpty
                              ? null
                              : PopupMenuButton<String>(
                                  tooltip: 'Member actions',
                                  onSelected: (value) => _action(member, value),
                                  itemBuilder: (_) => actions,
                                ),
                          onTap: _canAssignRoles(member)
                              ? () => _action(member, 'roles')
                              : null,
                        );
                      },
                    ))),
      ]);

  List<PopupMenuEntry<String>> _actionsFor(GuildMember member) {
    final owner = widget.guild.ownerRef == widget.actorRef;
    final self = member.user.ref == widget.actorRef;
    final targetIsOwner = member.user.ref == widget.guild.ownerRef;
    final canManageTarget = _canManageMember(member);
    final items = <PopupMenuEntry<String>>[];
    if (_canAssignRoles(member)) {
      items.add(
          const PopupMenuItem(value: 'roles', child: Text('Manage roles')));
    }
    if (self || widget.guild.allows(Permission.manageNicknames)) {
      items.add(const PopupMenuItem(
          value: 'nickname', child: Text('Change nickname')));
    }
    if (!self && !targetIsOwner && canManageTarget) {
      if (owner || widget.guild.allows(Permission.moderateMembers)) {
        items
            .add(const PopupMenuItem(value: 'timeout', child: Text('Timeout')));
      }
      if (owner || widget.guild.allows(Permission.kickMembers)) {
        items.add(const PopupMenuItem(value: 'kick', child: Text('Kick')));
      }
      if (owner || widget.guild.allows(Permission.banMembers)) {
        items.add(const PopupMenuItem(
            value: 'ban',
            child: Text('Ban', style: TextStyle(color: KaedeColors.danger))));
      }
    }
    return items;
  }

  bool _canAssignRoles(GuildMember member) {
    final owner = widget.guild.ownerRef == widget.actorRef;
    final self = member.user.ref == widget.actorRef;
    return (owner || widget.guild.allows(Permission.manageRoles)) &&
        (_canManageMember(member) || (self && owner));
  }

  bool _canManageMember(GuildMember member) {
    if (member.user.ref == widget.guild.ownerRef) return false;
    if (widget.guild.ownerRef == widget.actorRef) return true;
    final actorPosition = _actorRolePosition(widget.guild);
    if (actorPosition == null) return false;
    final targetPosition = member.roleIds
        .map((id) => _roleByMemberId(widget.guild, id)?.position ?? 0)
        .fold(0, (highest, value) => value > highest ? value : highest);
    return actorPosition > targetPosition;
  }

  Future<void> _action(GuildMember member, String action) async {
    try {
      switch (action) {
        case 'roles':
          final actorPosition = widget.guild.ownerRef == widget.actorRef
              ? null
              : _actorRolePosition(widget.guild);
          final selected = await showDialog<Set<String>>(
              context: context,
              builder: (_) => _RoleAssignmentDialog(
                  member: member,
                  roles: widget.guild.roles
                      .where((role) =>
                          role.position != 0 &&
                          role.ref != widget.guild.ref &&
                          (actorPosition == null ||
                              role.position < actorPosition))
                      .toList()));
          if (selected == null) return;
          await widget.repository
              .replaceMemberRoles(widget.guild.ref, member.user.ref, selected);
          break;
        case 'nickname':
          final nickname =
              await _prompt(context, 'Change nickname', 'Nickname');
          if (nickname == null) return;
          await widget.repository.updateMember(
              widget.guild.ref,
              member.user.ref,
              {'nickname': nickname.isEmpty ? null : nickname});
          break;
        case 'timeout':
          final minutes = await _prompt(context, 'Timeout ${member.user.name}',
              'Duration in minutes (0 removes timeout)',
              warning:
                  'Timed-out members can read history but cannot interact.');
          if (minutes == null) return;
          final value = int.tryParse(minutes);
          if (value == null || value < 0) {
            throw const UserInputException(
              'Enter a whole number of minutes, or 0 to remove the timeout.',
            );
          }
          await widget.repository
              .updateMember(widget.guild.ref, member.user.ref, {
            'timeout_until': value <= 0
                ? null
                : DateTime.now()
                    .toUtc()
                    .add(Duration(minutes: value))
                    .toIso8601String()
          });
          break;
        case 'kick':
          final reason = await _prompt(
              context, 'Kick ${member.user.name}?', 'Reason (optional)');
          if (reason == null) return;
          await widget.repository
              .kick(widget.guild.ref, member.user.ref, reason: reason);
          break;
        case 'ban':
          final reason = await _prompt(
              context, 'Ban ${member.user.name}?', 'Reason (optional)',
              warning:
                  'The user will be removed and unable to rejoin until unbanned.');
          if (reason == null) return;
          await widget.repository
              .ban(widget.guild.ref, member.user.ref, reason: reason);
          break;
      }
      await _load(reset: true);
      await widget.changed('$action applied');
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not apply the member action',
        )),
        backgroundColor: KaedeColors.danger,
      ));
    }
  }
}

final class _BansTab extends StatefulWidget {
  const _BansTab({
    required this.guild,
    required this.repository,
    required this.canBanMembers,
    required this.canBanInstances,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canBanMembers;
  final bool canBanInstances;
  @override
  State<_BansTab> createState() => _BansTabState();
}

final class _BansTabState extends State<_BansTab> {
  List<Map<String, Object?>> _bans = const [], _instances = const [];
  var _loading = true;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _BansTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.canBanMembers != widget.canBanMembers ||
        oldWidget.canBanInstances != widget.canBanInstances) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final values = await Future.wait([
        if (widget.canBanMembers)
          widget.repository.bans(widget.guild.ref)
        else
          Future.value(const <Map<String, Object?>>[]),
        if (widget.canBanInstances)
          widget.repository.instanceBans(widget.guild.ref)
        else
          Future.value(const <Map<String, Object?>>[]),
      ]);
      if (mounted) {
        setState(() {
          _bans = values[0];
          _instances = values[1];
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      _tabError(context, 'Could not load bans', error);
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => _loading
      ? const Center(child: CircularProgressIndicator())
      : _PageList(children: [
          _Panel(
              title: 'Member bans',
              child: Column(children: [
                if (!widget.canBanMembers)
                  const ListTile(
                    leading: Icon(Icons.lock_outline_rounded),
                    title: Text('You cannot manage member bans'),
                  ),
                if (_bans.isEmpty)
                  if (widget.canBanMembers)
                    const ListTile(title: Text('No banned members')),
                for (final ban in _bans)
                  ListTile(
                      title: Text(_mapName(ban)),
                      subtitle: Text('${ban['reason'] ?? 'No reason'}'),
                      trailing: TextButton(
                          onPressed: widget.canBanMembers
                              ? () async {
                                  try {
                                    await widget.repository.unban(
                                        widget.guild.ref,
                                        _mapRef(ban, widget.guild.ref.domain));
                                    await _load();
                                  } on Object catch (error) {
                                    if (mounted) {
                                      _tabError(this.context,
                                          'Could not remove member ban', error);
                                    }
                                  }
                                }
                              : null,
                          child: const Text('Unban'))),
              ])),
          _Panel(
              title: 'Banned instances',
              subtitle:
                  'This prevents every account hosted by that domain from joining. It may exclude innocent users and does not erase copies already held by a malicious peer.',
              child: Column(children: [
                FilledButton.tonalIcon(
                    onPressed: widget.canBanInstances ? _addInstance : null,
                    icon: const Icon(Icons.public_off_rounded),
                    label: const Text('Ban an instance')),
                for (final ban in _instances)
                  if (guildInstanceBanDomain(ban) case final domain?)
                    ListTile(
                        title: Text(domain.value),
                        subtitle: Text('${ban['reason'] ?? ''}'),
                        trailing: TextButton(
                            onPressed: widget.canBanInstances
                                ? () async {
                                    try {
                                      await widget.repository.unbanInstance(
                                          widget.guild.ref, domain);
                                      await _load();
                                    } on Object catch (error) {
                                      if (mounted) {
                                        _tabError(
                                            this.context,
                                            'Could not remove instance ban',
                                            error);
                                      }
                                    }
                                  }
                                : null,
                            child: const Text('Remove')))
                  else
                    const ListTile(
                      leading: Icon(Icons.warning_amber_rounded),
                      title: Text('Invalid instance-ban record'),
                      subtitle:
                          Text('Refresh or contact the instance operator.'),
                    ),
              ])),
        ]);
  Future<void> _addInstance() async {
    final value = await _prompt(
        context, 'Ban an entire instance?', 'example.net',
        warning:
            'All users from this domain will be unable to join. Use this only for severe, instance-wide abuse.');
    if (value == null) return;
    try {
      late final Domain domain;
      try {
        domain = Domain(value);
      } on FormatException {
        throw const UserInputException(
          'Enter a valid instance hostname, such as chat.example.',
        );
      }
      await widget.repository.banInstance(widget.guild.ref, domain);
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not ban instance', error);
    }
  }
}

final class _InvitesTab extends StatefulWidget {
  const _InvitesTab({
    required this.guild,
    required this.repository,
    required this.canCreate,
    required this.canManage,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canCreate;
  final bool canManage;
  @override
  State<_InvitesTab> createState() => _InvitesTabState();
}

final class _InvitesTabState extends State<_InvitesTab> {
  List<Map<String, Object?>> _items = const [];
  var _loading = true;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _InvitesTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.canCreate != widget.canCreate ||
        oldWidget.canManage != widget.canManage) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final items = widget.canManage
          ? await widget.repository.invites(widget.guild.ref)
          : const <Map<String, Object?>>[];
      if (mounted) {
        setState(() {
          _items = items;
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      _tabError(context, 'Could not load invites', error);
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
      backgroundColor: Colors.transparent,
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(14), children: [
              const Text(
                  'Invite links can be restricted by age and use count. Treat private invites like passwords.'),
              const SizedBox(height: 12),
              if (!widget.canManage)
                const ListTile(
                  leading: Icon(Icons.lock_outline_rounded),
                  title: Text(
                      'Manage Guild is required to list or revoke invites'),
                ),
              for (final item in _items)
                Card(
                    child: ListTile(
                        leading: const Icon(Icons.link_rounded),
                        title: Text('${item['code']}'),
                        subtitle: Text('${item['uses'] ?? 0} uses'),
                        trailing: IconButton(
                            onPressed: widget.canManage
                                ? () async {
                                    try {
                                      await widget.repository
                                          .revokeInvite('${item['code']}');
                                      await _load();
                                    } on Object catch (error) {
                                      if (mounted) {
                                        _tabError(this.context,
                                            'Could not revoke invite', error);
                                      }
                                    }
                                  }
                                : null,
                            icon: const Icon(Icons.delete_outline_rounded)))),
            ]),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: widget.canCreate ? _create : null,
          icon: const Icon(Icons.person_add_alt_1),
          label: const Text('Invite')));
  Future<void> _create() async {
    final channels = guildTextChannelTargets(widget.guild.channels);
    if (channels.isEmpty) {
      _tabError(
        context,
        'Could not create the invite',
        const UserInputException(
          'Create a text or announcement channel before creating an invite.',
        ),
      );
      return;
    }
    final channel = await showGuildTextChannelPicker(
      context,
      channels: channels,
      title: 'Invite people to…',
    );
    if (channel == null || !mounted) return;
    try {
      await widget.repository.createInvite(widget.guild.ref, {
        'channel_id': channel.ref.id.value,
        'max_age_seconds': 604800,
        'max_uses': 100,
      });
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create invite', error);
    }
  }
}

final class _EmojiTab extends StatefulWidget {
  const _EmojiTab({
    required this.guild,
    required this.repository,
    required this.canManage,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canManage;
  @override
  State<_EmojiTab> createState() => _EmojiTabState();
}

final class _EmojiTabState extends State<_EmojiTab> {
  List<Map<String, Object?>> _items = const [];
  var _loading = true;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _EmojiTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.canManage != widget.canManage) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final items = await widget.repository.emojis();
      if (mounted) {
        setState(() {
          _items = items
              .where((item) =>
                  '${item['guild_id']}@${item['guild_domain']}' ==
                  widget.guild.ref.wire)
              .toList();
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      _tabError(context, 'Could not load emoji', error);
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
      backgroundColor: Colors.transparent,
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(14), children: [
              const Text(
                  'Custom emoji can be used across guilds when the user has permission. Federated tokens retain the emoji’s origin.'),
              const SizedBox(height: 12),
              for (final item in _items)
                Card(
                    child: ListTile(
                        leading: const Icon(Icons.emoji_emotions_outlined),
                        title: Text(':${item['name']}:'),
                        subtitle: Text('${item['origin_domain']}'),
                        trailing: IconButton(
                            onPressed: widget.canManage
                                ? () async {
                                    try {
                                      await widget.repository.deleteEmoji(
                                          widget.guild.ref,
                                          _mapRef(
                                              item, widget.guild.ref.domain));
                                      await _load();
                                    } on Object catch (error) {
                                      if (mounted) {
                                        _tabError(this.context,
                                            'Could not delete emoji', error);
                                      }
                                    }
                                  }
                                : null,
                            icon: const Icon(Icons.delete_outline_rounded)))),
            ]),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: widget.canManage ? _upload : null,
          icon: const Icon(Icons.upload_rounded),
          label: const Text('Upload')));
  Future<void> _upload() async {
    final name = await _prompt(context, 'Emoji name', 'lowercase_name');
    if (name == null) return;
    final file = await ImagePicker().pickImage(source: ImageSource.gallery);
    if (file == null) return;
    if (!mounted) return;
    final contentType =
        imageUploadContentType(file.name, reportedType: file.mimeType);
    if (contentType == null) {
      _tabError(context, 'Could not upload emoji',
          'Choose a PNG, JPEG, GIF, or WebP image.');
      return;
    }
    try {
      await widget.repository.uploadEmoji(
          guild: widget.guild.ref,
          name: name,
          filename: file.name,
          contentType: contentType,
          file: File(file.path));
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not upload emoji', error);
    }
  }
}

final class _WebhooksTab extends StatefulWidget {
  const _WebhooksTab({
    required this.guild,
    required this.repository,
    required this.canManage,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canManage;
  @override
  State<_WebhooksTab> createState() => _WebhooksTabState();
}

final class _WebhooksTabState extends State<_WebhooksTab> {
  List<Map<String, Object?>> _items = const [];
  var _loading = true;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _WebhooksTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.canManage != widget.canManage) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final items = widget.canManage
          ? await widget.repository.webhooks(widget.guild.ref)
          : const <Map<String, Object?>>[];
      if (mounted) {
        setState(() {
          _items = items;
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      _tabError(context, 'Could not load webhooks', error);
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
      backgroundColor: Colors.transparent,
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(14), children: [
              const Text(
                  'Webhook tokens are secrets. A rotated token is shown only once.'),
              const SizedBox(height: 12),
              if (!widget.canManage)
                const ListTile(
                  leading: Icon(Icons.lock_outline_rounded),
                  title: Text('Manage Webhooks is required'),
                ),
              for (final item in _items)
                Card(
                    child: ListTile(
                        leading: const Icon(Icons.webhook_rounded),
                        title: Text('${item['name'] ?? 'Webhook'}'),
                        subtitle: Text('${item['channel_id']}'),
                        trailing: PopupMenuButton<String>(
                            enabled: widget.canManage,
                            onSelected: (value) async {
                              try {
                                if (value == 'rotate') {
                                  final rotated = await widget.repository
                                      .rotateWebhook('${item['id']}');
                                  if (context.mounted) {
                                    await showDialog<void>(
                                        context: context,
                                        builder: (dialogContext) => AlertDialog(
                                                title: const Text(
                                                    'New webhook token'),
                                                content: SelectableText(
                                                    '${rotated['token']}'),
                                                actions: [
                                                  TextButton(
                                                      onPressed: () =>
                                                          Navigator.pop(
                                                              dialogContext),
                                                      child: const Text('Done'))
                                                ]));
                                  }
                                } else {
                                  await widget.repository
                                      .deleteWebhook('${item['id']}');
                                }
                                await _load();
                              } on Object catch (error) {
                                if (mounted) {
                                  _tabError(this.context,
                                      'Could not update webhook', error);
                                }
                              }
                            },
                            itemBuilder: (_) => const [
                                  PopupMenuItem(
                                      value: 'rotate',
                                      child: Text('Rotate token')),
                                  PopupMenuItem(
                                      value: 'delete', child: Text('Delete'))
                                ]))),
            ]),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: widget.canManage ? _create : null,
          icon: const Icon(Icons.add_rounded),
          label: const Text('Webhook')));
  Future<void> _create() async {
    final name = await _prompt(context, 'Create webhook', 'Webhook name');
    if (name == null || !mounted) return;
    final channels = guildTextChannelTargets(widget.guild.channels);
    if (channels.isEmpty) {
      _tabError(
        context,
        'Could not create the webhook',
        const UserInputException(
          'Create a text or announcement channel before creating a webhook.',
        ),
      );
      return;
    }
    final channel = await showGuildTextChannelPicker(
      context,
      channels: channels,
      title: 'Post this webhook in…',
    );
    if (channel == null || !mounted) return;
    try {
      final created = await widget.repository
          .createWebhook(widget.guild.ref, channel.ref, name);
      if (mounted) {
        await showDialog<void>(
            context: context,
            builder: (dialogContext) => AlertDialog(
                    title: const Text('Webhook created'),
                    content: SelectableText(
                        '${created['token'] ?? 'Token is available only now.'}'),
                    actions: [
                      TextButton(
                          onPressed: () => Navigator.pop(dialogContext),
                          child: const Text('Done'))
                    ]));
      }
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create webhook', error);
    }
  }
}

final class _AuditTab extends StatefulWidget {
  const _AuditTab({
    required this.guild,
    required this.repository,
    required this.canView,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canView;
  @override
  State<_AuditTab> createState() => _AuditTabState();
}

final class _AuditTabState extends State<_AuditTab> {
  List<Map<String, Object?>> _items = const [];
  var _loading = true;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _AuditTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.canView != widget.canView) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final items = widget.canView
          ? await widget.repository.auditLog(widget.guild.ref)
          : const <Map<String, Object?>>[];
      if (mounted) {
        setState(() {
          _items = items;
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      _tabError(context, 'Could not load audit log', error);
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => !widget.canView
      ? const Center(
          child: ListTile(
            leading: Icon(Icons.lock_outline_rounded),
            title: Text('View Audit Log is required'),
          ),
        )
      : _loading
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _load,
              child: ListView.builder(
                  padding: const EdgeInsets.all(14),
                  itemCount: _items.length,
                  itemBuilder: (_, index) {
                    final item = _items[index];
                    return ListTile(
                        leading: const Icon(Icons.history_rounded),
                        title: Text(guildAuditActionLabel(item)),
                        subtitle: Text(
                            '${item['reason'] ?? 'No reason'}\n${item['created_at'] ?? ''}'),
                        isThreeLine: true);
                  }));
}

final class _PermissionScreen extends StatefulWidget {
  const _PermissionScreen(
      {required this.guild,
      required this.channel,
      required this.members,
      required this.existing,
      required this.repository});
  final KaedeGuild guild;
  final KaedeChannel channel;
  final List<GuildMember> members;
  final List<Map<String, Object?>> existing;
  final KaedeRepository repository;
  @override
  State<_PermissionScreen> createState() => _PermissionScreenState();
}

final class _PermissionScreenState extends State<_PermissionScreen> {
  EntityRef? _target;
  String _type = 'role';
  BigInt _allow = BigInt.zero, _deny = BigInt.zero;
  final _search = TextEditingController();
  final _targetScroll = ScrollController();
  Timer? _searchDebounce;
  List<GuildMember> _members = const [];
  late List<Map<String, Object?>> _overwrites;
  var _loadingMembers = false;
  var _hasMoreMembers = false;
  var _memberRequestGeneration = 0;
  var _hasOverwrite = false;
  var _mutating = false;

  @override
  void initState() {
    super.initState();
    _members = widget.members;
    _overwrites = List<Map<String, Object?>>.of(widget.existing);
    _hasMoreMembers = widget.members.length == 100;
    _search.addListener(_searchChanged);
    _targetScroll.addListener(_maybeLoadMoreMembers);
  }

  @override
  void didUpdateWidget(covariant _PermissionScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    final changedChannel = oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.channel.ref != widget.channel.ref;
    if (changedChannel) {
      _memberRequestGeneration += 1;
      _search.clear();
      _target = null;
      _type = 'role';
      _allow = BigInt.zero;
      _deny = BigInt.zero;
      _hasOverwrite = false;
      _mutating = false;
      _overwrites = List<Map<String, Object?>>.of(widget.existing);
    }
    if (changedChannel || oldWidget.members != widget.members) {
      _members = widget.members;
      _hasMoreMembers = widget.members.length == 100;
    }
    if (!changedChannel && oldWidget.existing != widget.existing) {
      _overwrites = List<Map<String, Object?>>.of(widget.existing);
      if (_target != null) _loadSelectedOverwrite();
    }
  }

  @override
  void dispose() {
    _searchDebounce?.cancel();
    _search
      ..removeListener(_searchChanged)
      ..dispose();
    _targetScroll
      ..removeListener(_maybeLoadMoreMembers)
      ..dispose();
    super.dispose();
  }

  void _searchChanged() {
    _searchDebounce?.cancel();
    _searchDebounce = Timer(const Duration(milliseconds: 250), _findMembers);
    setState(() {});
  }

  Future<void> _findMembers() async {
    final query = _search.text.trim();
    final generation = ++_memberRequestGeneration;
    if (query.isEmpty) {
      if (mounted) {
        setState(() {
          _members = widget.members;
          _hasMoreMembers = widget.members.length == 100;
          _loadingMembers = false;
        });
      }
      return;
    }
    setState(() => _loadingMembers = true);
    try {
      final members =
          await widget.repository.members(widget.guild.ref, query: query);
      if (mounted &&
          generation == _memberRequestGeneration &&
          query == _search.text.trim()) {
        setState(() {
          _members = members;
          _hasMoreMembers = members.length == 100;
        });
      }
    } on Object catch (error) {
      if (mounted &&
          generation == _memberRequestGeneration &&
          query == _search.text.trim()) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(userFacingError(
              error,
              summary: 'Could not search the members',
            )),
            backgroundColor: KaedeColors.danger,
          ),
        );
      }
    } finally {
      if (mounted &&
          generation == _memberRequestGeneration &&
          query == _search.text.trim()) {
        setState(() => _loadingMembers = false);
      }
    }
  }

  void _maybeLoadMoreMembers() {
    if (_targetScroll.hasClients &&
        _targetScroll.position.extentAfter < 360 &&
        _hasMoreMembers &&
        !_loadingMembers) {
      unawaited(_loadMoreMembers());
    }
  }

  Future<void> _loadMoreMembers() async {
    if (_members.isEmpty || !_hasMoreMembers || _loadingMembers) return;
    final query = _search.text.trim();
    final generation = _memberRequestGeneration;
    setState(() => _loadingMembers = true);
    try {
      final page = await widget.repository.members(
        widget.guild.ref,
        query: query,
        after: _members.last.user.ref,
      );
      if (!mounted ||
          generation != _memberRequestGeneration ||
          query != _search.text.trim()) {
        return;
      }
      final known = _members.map((member) => member.user.ref).toSet();
      setState(() {
        _members = <GuildMember>[
          ..._members,
          ...page.where((member) => known.add(member.user.ref)),
        ];
        _hasMoreMembers = page.length == 100;
      });
    } on Object catch (error) {
      if (mounted && generation == _memberRequestGeneration) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(userFacingError(
              error,
              summary: 'Could not load more members',
            )),
            backgroundColor: KaedeColors.danger,
          ),
        );
      }
    } finally {
      if (mounted && generation == _memberRequestGeneration) {
        setState(() => _loadingMembers = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final query = _search.text.trim().toLowerCase();
    final targets = <(String, EntityRef, String, KaedeUser?)>[
      for (final role in widget.guild.roles)
        if (query.isEmpty || role.name.toLowerCase().contains(query))
          (role.name, role.ref, 'role', null),
      for (final member in _members)
        (
          member.nickname ?? member.user.name,
          member.user.ref,
          'member',
          member.user
        ),
    ];
    return Scaffold(
      appBar: AppBar(title: Text('#${widget.channel.name} permissions')),
      body: Column(children: [
        if (widget.channel.parentRef != null &&
            !widget.channel.permissionsSynced)
          MaterialBanner(
            content: const Text(
                'Permissions are independent from the parent category.'),
            actions: [
              TextButton(
                onPressed: _mutating ? null : _sync,
                child: const Text('Sync with category'),
              ),
            ],
          ),
        Expanded(
          child: LayoutBuilder(builder: (context, constraints) {
            final rail = _targetRail(targets);
            final editor = _permissionEditor();
            if (constraints.maxWidth < 720) {
              return Column(children: [
                ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 260),
                  child: rail,
                ),
                const Divider(height: 1),
                Expanded(child: editor),
              ]);
            }
            return Row(children: [
              SizedBox(width: 300, child: rail),
              const VerticalDivider(width: 1),
              Expanded(child: editor),
            ]);
          }),
        ),
      ]),
    );
  }

  Widget _targetRail(List<(String, EntityRef, String, KaedeUser?)> targets) {
    return Column(children: [
      Padding(
        padding: const EdgeInsets.all(12),
        child: SearchBar(
          controller: _search,
          hintText: 'Search roles or members',
          leading: const Icon(Icons.search_rounded),
          trailing: [
            if (_loadingMembers)
              const SizedBox.square(
                  dimension: 18, child: CircularProgressIndicator()),
            if (_search.text.isNotEmpty)
              IconButton(
                  onPressed: _search.clear,
                  icon: const Icon(Icons.close_rounded)),
          ],
        ),
      ),
      Expanded(
        child: ListView.builder(
          controller: _targetScroll,
          itemCount: targets.length + (_loadingMembers ? 1 : 0),
          itemBuilder: (_, index) {
            if (index == targets.length) {
              return const Padding(
                padding: EdgeInsets.all(16),
                child: Center(child: CircularProgressIndicator()),
              );
            }
            final target = targets[index];
            return ListTile(
              selected: _target == target.$2,
              leading: target.$4 == null
                  ? const CircleAvatar(
                      radius: 15, child: Icon(Icons.shield_outlined, size: 17))
                  : UserAvatar(user: target.$4!, radius: 15),
              title: Text(target.$1, overflow: TextOverflow.ellipsis),
              subtitle: Text(target.$3),
              onTap: () => _select(target.$2, target.$3),
            );
          },
        ),
      ),
    ]);
  }

  Widget _permissionEditor() {
    if (_target == null) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.shield_outlined, size: 36, color: KaedeColors.muted),
              SizedBox(height: 10),
              Text('Choose a role or member',
                  style: TextStyle(fontWeight: FontWeight.w800)),
              SizedBox(height: 4),
              Text('Then set channel-specific permissions.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: KaedeColors.muted)),
            ],
          ),
        ),
      );
    }
    final relevant = permissionMetadata
        .where((item) =>
            item.resourceScopes.contains('channel') &&
            (item.channelTypes.isEmpty ||
                item.channelTypes
                    .contains(_channelNumber(widget.channel.type))))
        .toList(growable: false);
    final groups = relevant.map((item) => item.group).toSet();
    return ListView(padding: const EdgeInsets.all(16), children: [
      const Text('Channel override',
          style: TextStyle(fontSize: 19, fontWeight: FontWeight.w800)),
      const SizedBox(height: 4),
      const Text(
        'Deny blocks the role permission here. Inherit keeps the role value. Allow grants it here.',
        style: TextStyle(color: KaedeColors.muted),
      ),
      const SizedBox(height: 14),
      for (final group in groups)
        Card(
          margin: const EdgeInsets.only(bottom: 12),
          clipBehavior: Clip.antiAlias,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 14, 16, 4),
                child:
                    Text(group, style: Theme.of(context).textTheme.titleLarge),
              ),
              for (final permission
                  in relevant.where((item) => item.group == group))
                _PermissionRow(
                    metadata: permission,
                    value: _permissionValue(permission.bit),
                    changed: (value) =>
                        setState(() => _set(permission.bit, value))),
            ],
          ),
        ),
      const SizedBox(height: 4),
      Row(children: [
        Expanded(
          child: FilledButton.icon(
              onPressed: _mutating ? null : _save,
              icon: const Icon(Icons.save_outlined),
              label: Text(_mutating ? 'Saving…' : 'Save overwrite')),
        ),
        if (_hasOverwrite) ...[
          const SizedBox(width: 10),
          OutlinedButton.icon(
            onPressed: _mutating ? null : _delete,
            icon: const Icon(Icons.restart_alt_rounded),
            label: const Text('Reset'),
          ),
        ],
      ]),
      const SizedBox(height: 12),
    ]);
  }

  void _select(EntityRef target, String type) {
    _target = target;
    _type = type;
    _loadSelectedOverwrite();
  }

  void _loadSelectedOverwrite() {
    final target = _target;
    if (target == null) return;
    final found = _overwrites
        .where((item) => channelOverwriteMatches(
              item,
              target: target,
              targetType: _type,
              defaultDomain: widget.guild.ref.domain,
            ))
        .firstOrNull;
    setState(() {
      _hasOverwrite = found != null;
      _allow = BigInt.tryParse('${found?['allow'] ?? 0}') ?? BigInt.zero;
      _deny = BigInt.tryParse('${found?['deny'] ?? 0}') ?? BigInt.zero;
    });
  }

  int _permissionValue(int bit) {
    final mask = BigInt.from(bit);
    if (_deny & mask != BigInt.zero) return -1;
    if (_allow & mask != BigInt.zero) return 1;
    return 0;
  }

  void _set(int bit, int value) {
    final mask = BigInt.from(bit);
    _allow &= ~mask;
    _deny &= ~mask;
    if (value < 0) _deny |= mask;
    if (value > 0) _allow |= mask;
  }

  Future<void> _save() async {
    final target = _target;
    if (target == null || _mutating) return;
    final targetType = _type;
    final allow = _allow;
    final deny = _deny;
    setState(() => _mutating = true);
    try {
      await widget.repository.setOverwrite(
        widget.guild.ref,
        widget.channel.ref,
        channelOverwriteRequest(
          target: target,
          targetType: targetType,
          allow: allow,
          deny: deny,
        ),
      );
      if (mounted) {
        setState(() {
          _overwrites = upsertChannelOverwrite(
            _overwrites,
            target: target,
            targetType: targetType,
            allow: allow,
            deny: deny,
            defaultDomain: widget.guild.ref.domain,
          );
          if (_target == target && _type == targetType) {
            _hasOverwrite = true;
          }
        });
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('Permissions saved')));
      }
    } on Object catch (error) {
      if (mounted) _showMutationError('Could not save permissions', error);
    } finally {
      if (mounted) setState(() => _mutating = false);
    }
  }

  Future<void> _delete() async {
    final target = _target;
    if (target == null || _mutating) return;
    final targetType = _type;
    setState(() => _mutating = true);
    try {
      await widget.repository.deleteOverwrite(
          widget.guild.ref, widget.channel.ref, target, targetType);
      if (!mounted) return;
      setState(() {
        _overwrites = removeChannelOverwrite(
          _overwrites,
          target: target,
          targetType: targetType,
          defaultDomain: widget.guild.ref.domain,
        );
        if (_target == target && _type == targetType) {
          _allow = BigInt.zero;
          _deny = BigInt.zero;
          _hasOverwrite = false;
        }
      });
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Overwrite reset')));
    } on Object catch (error) {
      if (mounted) _showMutationError('Could not reset permissions', error);
    } finally {
      if (mounted) setState(() => _mutating = false);
    }
  }

  Future<void> _sync() async {
    if (_mutating) return;
    final navigator = Navigator.of(context);
    setState(() => _mutating = true);
    try {
      await widget.repository
          .syncChannelPermissions(widget.guild.ref, widget.channel.ref);
      if (mounted) navigator.pop(true);
    } on Object catch (error) {
      if (mounted) _showMutationError('Could not sync permissions', error);
    } finally {
      if (mounted) setState(() => _mutating = false);
    }
  }

  void _showMutationError(String message, Object error) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(userFacingError(error, summary: message)),
        backgroundColor: KaedeColors.danger,
      ),
    );
  }
}

Map<String, Object?> channelOverwriteRequest({
  required EntityRef target,
  required String targetType,
  required BigInt allow,
  required BigInt deny,
}) =>
    <String, Object?>{
      'target_id': target.wire,
      'target_type': targetType,
      'allow': '$allow',
      'deny': '$deny',
    };

bool channelOverwriteMatches(
  Map<String, Object?> overwrite, {
  required EntityRef target,
  required String targetType,
  required Domain defaultDomain,
}) {
  final rawId = '${overwrite['target_id'] ?? ''}'.trim();
  if (rawId.isEmpty) return false;
  EntityRef overwriteTarget;
  try {
    overwriteTarget = rawId.contains('@')
        ? EntityRef.parse(rawId)
        : EntityRef(
            Snowflake(rawId),
            Domain('${overwrite['target_domain'] ?? defaultDomain.value}'),
          );
  } on Object {
    return false;
  }
  return overwriteTarget == target &&
      '${overwrite['target_type'] ?? overwrite['type']}' == targetType;
}

List<Map<String, Object?>> upsertChannelOverwrite(
  List<Map<String, Object?>> overwrites, {
  required EntityRef target,
  required String targetType,
  required BigInt allow,
  required BigInt deny,
  required Domain defaultDomain,
}) =>
    <Map<String, Object?>>[
      for (final overwrite in overwrites)
        if (!channelOverwriteMatches(
          overwrite,
          target: target,
          targetType: targetType,
          defaultDomain: defaultDomain,
        ))
          overwrite,
      <String, Object?>{
        'target_id': target.id.value,
        'target_domain': target.domain.value,
        'target_type': targetType,
        'allow': '$allow',
        'deny': '$deny',
      },
    ];

List<Map<String, Object?>> removeChannelOverwrite(
  List<Map<String, Object?>> overwrites, {
  required EntityRef target,
  required String targetType,
  required Domain defaultDomain,
}) =>
    <Map<String, Object?>>[
      for (final overwrite in overwrites)
        if (!channelOverwriteMatches(
          overwrite,
          target: target,
          targetType: targetType,
          defaultDomain: defaultDomain,
        ))
          overwrite,
    ];

/// Builds the complete local channel ordering expected by the management API.
///
/// Channel position IDs and parents are guild-local snowflakes, not composite
/// entity references. Sending every current parent is also essential: an
/// omitted [parent_id] defaults to null and would otherwise detach category
/// children during an otherwise position-only reorder.
List<Map<String, Object?>> guildChannelPositionRequest(
  List<KaedeChannel> channels,
) =>
    <Map<String, Object?>>[
      for (var index = 0; index < channels.length; index++)
        <String, Object?>{
          'id': channels[index].ref.id.value,
          'position': index,
          'parent_id': channels[index].parentRef?.id.value,
        },
    ];

Set<String> normalizedMemberRoleIds(Iterable<String> roleIds) => roleIds
    .map((roleId) => roleId.contains('@') ? roleId.split('@').first : roleId)
    .toSet();

Domain? guildInstanceBanDomain(Map<String, Object?> item) {
  final value = '${item['instance_domain'] ?? ''}'.trim();
  if (value.isEmpty) return null;
  try {
    return Domain(value);
  } on FormatException {
    return null;
  }
}

String guildAuditActionLabel(Map<String, Object?> item) {
  final value = '${item['action_type'] ?? ''}'.trim();
  return value.isEmpty ? 'Guild action' : value;
}

KaedeRole? _roleByMemberId(KaedeGuild guild, String roleId) {
  final normalized = roleId.contains('@') ? roleId.split('@').first : roleId;
  return guild.roles
      .where(
          (role) => role.ref.wire == roleId || role.ref.id.value == normalized)
      .firstOrNull;
}

int? _actorRolePosition(KaedeGuild guild) {
  final id = guild.actorHighestRoleId;
  if (id == null) return null;
  return _roleByMemberId(guild, id)?.position;
}

final class _PermissionRow extends StatelessWidget {
  const _PermissionRow(
      {required this.metadata, required this.value, required this.changed});
  final PermissionMetadata metadata;
  final int value;
  final ValueChanged<int> changed;

  Widget _selector() => SegmentedButton<int>(
        showSelectedIcon: false,
        segments: const [
          ButtonSegment(
              value: -1,
              icon: Icon(Icons.close, color: KaedeColors.danger),
              tooltip: 'Deny'),
          ButtonSegment(
              value: 0, icon: Icon(Icons.horizontal_rule), tooltip: 'Inherit'),
          ButtonSegment(
              value: 1,
              icon: Icon(Icons.check, color: KaedeColors.mint),
              tooltip: 'Allow')
        ],
        selected: {value},
        onSelectionChanged: (value) => changed(value.first),
      );

  @override
  Widget build(BuildContext context) => LayoutBuilder(
        builder: (context, constraints) {
          final copy = Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(metadata.label,
                  style: const TextStyle(fontWeight: FontWeight.w700)),
              const SizedBox(height: 2),
              Text(metadata.description,
                  style:
                      const TextStyle(color: KaedeColors.muted, fontSize: 13)),
            ],
          );
          return Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
            child: constraints.maxWidth < 480
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      copy,
                      const SizedBox(height: 9),
                      Align(
                          alignment: Alignment.centerRight, child: _selector()),
                    ],
                  )
                : Row(
                    children: [
                      Expanded(child: copy),
                      const SizedBox(width: 16),
                      _selector(),
                    ],
                  ),
          );
        },
      );
}

Future<GuildChannelDraft?> showGuildChannelEditorSheet(
  BuildContext context, {
  KaedeChannel? channel,
  List<KaedeChannel> channels = const <KaedeChannel>[],
  EntityRef? initialParent,
}) =>
    showModalBottomSheet<GuildChannelDraft>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (_) => _ChannelEditorSheet(
        channel: channel,
        channels: channels,
        initialParent: initialParent,
      ),
    );

List<KaedeChannel> guildTextChannelTargets(Iterable<KaedeChannel> channels) =>
    channels
        .where((channel) =>
            channel.type == ChannelType.text ||
            channel.type == ChannelType.announcement)
        .toList(growable: false)
      ..sort((a, b) => a.position.compareTo(b.position));

Future<KaedeChannel?> showGuildTextChannelPicker(
  BuildContext context, {
  required List<KaedeChannel> channels,
  required String title,
}) =>
    showModalBottomSheet<KaedeChannel>(
      context: context,
      useSafeArea: true,
      showDragHandle: true,
      builder: (sheetContext) => Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 10),
            child: Text(title,
                style: Theme.of(context)
                    .textTheme
                    .headlineSmall
                    ?.copyWith(fontWeight: FontWeight.w900)),
          ),
          Flexible(
            child: ListView(
              shrinkWrap: true,
              padding: const EdgeInsets.fromLTRB(10, 0, 10, 16),
              children: [
                for (final channel in channels)
                  ListTile(
                    leading: Icon(channel.type == ChannelType.announcement
                        ? Icons.campaign_rounded
                        : Icons.tag_rounded),
                    title: Text(channel.name ?? 'channel'),
                    subtitle: Text(channel.type == ChannelType.announcement
                        ? 'Announcement channel'
                        : 'Text channel'),
                    trailing: const Icon(Icons.chevron_right_rounded),
                    onTap: () => Navigator.pop(sheetContext, channel),
                  ),
              ],
            ),
          ),
        ],
      ),
    );

final class _ChannelEditorSheet extends StatefulWidget {
  const _ChannelEditorSheet({
    this.channel,
    required this.channels,
    this.initialParent,
  });

  final KaedeChannel? channel;
  final List<KaedeChannel> channels;
  final EntityRef? initialParent;

  @override
  State<_ChannelEditorSheet> createState() => _ChannelEditorSheetState();
}

final class _ChannelEditorSheetState extends State<_ChannelEditorSheet> {
  final _formKey = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.channel?.name ?? '');
  late final _topic = TextEditingController(text: widget.channel?.topic ?? '');
  late ChannelType _type = widget.channel?.type ?? ChannelType.text;
  late int _slow = widget.channel?.slowModeSeconds ?? 0;
  late String _parent =
      (widget.channel?.parentRef ?? widget.initialParent)?.wire ?? '';

  static const _types = <(ChannelType, String, IconData)>[
    (ChannelType.text, 'Text', Icons.tag_rounded),
    (ChannelType.voice, 'Voice', Icons.volume_up_rounded),
    (ChannelType.announcement, 'Announcement', Icons.campaign_rounded),
    (ChannelType.category, 'Category', Icons.folder_outlined),
  ];

  static const _slowModes = <int, String>{
    0: 'Off',
    5: '5 seconds',
    10: '10 seconds',
    30: '30 seconds',
    60: '1 minute',
    300: '5 minutes',
    3600: '1 hour',
  };

  @override
  void dispose() {
    _name.dispose();
    _topic.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final media = MediaQuery.of(context);
    final categories = widget.channels
        .where((channel) =>
            channel.type == ChannelType.category &&
            channel.ref != widget.channel?.ref)
        .toList()
      ..sort((a, b) => a.position.compareTo(b.position));
    final slowModes = <int, String>{
      ..._slowModes,
      if (!_slowModes.containsKey(_slow)) _slow: '$_slow seconds',
    };
    final compactKeyboardLayout =
        media.size.height - media.viewInsets.bottom < 260;
    final saveButton = Padding(
      padding: const EdgeInsets.fromLTRB(20, 10, 20, 18),
      child: SizedBox(
        width: double.infinity,
        child: FilledButton.icon(
          key: const ValueKey('save-channel-button'),
          onPressed: _save,
          icon: Icon(
              widget.channel == null ? Icons.add_rounded : Icons.save_outlined),
          label:
              Text(widget.channel == null ? 'Create channel' : 'Save changes'),
        ),
      ),
    );
    return AnimatedPadding(
      duration: const Duration(milliseconds: 180),
      curve: Curves.easeOut,
      padding: EdgeInsets.only(bottom: media.viewInsets.bottom),
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: media.size.height * .88),
        child: Form(
          key: _formKey,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (!compactKeyboardLayout)
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 0, 12, 12),
                  child: Row(
                    children: [
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              widget.channel == null
                                  ? 'Create a channel'
                                  : 'Edit channel',
                              style: Theme.of(context)
                                  .textTheme
                                  .headlineSmall
                                  ?.copyWith(fontWeight: FontWeight.w900),
                            ),
                            const SizedBox(height: 3),
                            Text(
                              widget.channel == null
                                  ? 'Choose what members can use this space for.'
                                  : 'Update how this channel appears and behaves.',
                              style: const TextStyle(color: KaedeColors.muted),
                            ),
                          ],
                        ),
                      ),
                      IconButton(
                        tooltip: 'Close',
                        onPressed: () => Navigator.pop(context),
                        icon: const Icon(Icons.close_rounded),
                      ),
                    ],
                  ),
                ),
              if (!compactKeyboardLayout) const Divider(height: 1),
              Flexible(
                child: SingleChildScrollView(
                  keyboardDismissBehavior:
                      ScrollViewKeyboardDismissBehavior.onDrag,
                  padding: const EdgeInsets.fromLTRB(20, 18, 20, 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      TextFormField(
                        key: const ValueKey('channel-name-field'),
                        controller: _name,
                        autofocus: widget.channel == null,
                        maxLength: 100,
                        textInputAction: TextInputAction.next,
                        decoration: const InputDecoration(
                          labelText: 'Channel name',
                          hintText: 'new-channel',
                          prefixIcon: Icon(Icons.tag_rounded),
                        ),
                        validator: (value) => value?.trim().isEmpty == true
                            ? 'Enter a channel name'
                            : null,
                      ),
                      const SizedBox(height: 8),
                      Text('Channel type',
                          style: Theme.of(context).textTheme.titleMedium),
                      const SizedBox(height: 9),
                      GridView.count(
                        crossAxisCount: 2,
                        mainAxisSpacing: 8,
                        crossAxisSpacing: 8,
                        childAspectRatio: 2.55,
                        shrinkWrap: true,
                        physics: const NeverScrollableScrollPhysics(),
                        children: [
                          for (final option in _types)
                            _ChannelTypeTile(
                              type: option.$1,
                              label: option.$2,
                              icon: option.$3,
                              selected: _type == option.$1,
                              enabled: widget.channel == null,
                              onTap: () => setState(() {
                                _type = option.$1;
                                if (_type == ChannelType.category) _parent = '';
                              }),
                            ),
                        ],
                      ),
                      if (widget.channel != null) ...[
                        const SizedBox(height: 7),
                        const Text(
                          'Channel type cannot be changed after creation.',
                          style:
                              TextStyle(color: KaedeColors.muted, fontSize: 12),
                        ),
                      ],
                      if (_type != ChannelType.category) ...[
                        const SizedBox(height: 18),
                        DropdownButtonFormField<String>(
                          key: const ValueKey('channel-category-field'),
                          initialValue: categories
                                  .any((channel) => channel.ref.wire == _parent)
                              ? _parent
                              : '',
                          isExpanded: true,
                          decoration: const InputDecoration(
                            labelText: 'Category',
                            prefixIcon: Icon(Icons.folder_outlined),
                          ),
                          items: [
                            const DropdownMenuItem(
                                value: '', child: Text('No category')),
                            for (final category in categories)
                              DropdownMenuItem(
                                value: category.ref.wire,
                                child: Text(category.name ?? 'Category'),
                              ),
                          ],
                          onChanged: (value) =>
                              setState(() => _parent = value ?? ''),
                        ),
                        const SizedBox(height: 14),
                        TextFormField(
                          controller: _topic,
                          maxLength: 1024,
                          minLines: 2,
                          maxLines: 3,
                          textCapitalization: TextCapitalization.sentences,
                          decoration: const InputDecoration(
                            labelText: 'Topic (optional)',
                            alignLabelWithHint: true,
                          ),
                        ),
                        const SizedBox(height: 4),
                        DropdownButtonFormField<int>(
                          initialValue: _slow,
                          isExpanded: true,
                          decoration: const InputDecoration(
                            labelText: 'Slow mode',
                            prefixIcon: Icon(Icons.timer_outlined),
                          ),
                          items: [
                            for (final entry in slowModes.entries)
                              DropdownMenuItem(
                                  value: entry.key, child: Text(entry.value)),
                          ],
                          onChanged: (value) =>
                              setState(() => _slow = value ?? 0),
                        ),
                      ],
                      if (compactKeyboardLayout) saveButton,
                    ],
                  ),
                ),
              ),
              if (!compactKeyboardLayout) saveButton,
            ],
          ),
        ),
      ),
    );
  }

  void _save() {
    if (_formKey.currentState?.validate() != true) return;
    final parent = widget.channels
        .where((channel) => channel.ref.wire == _parent)
        .firstOrNull
        ?.ref;
    Navigator.pop(
      context,
      GuildChannelDraft(
        name: _name.text.trim(),
        topic: _topic.text.trim(),
        type: _type,
        slowModeSeconds: _type == ChannelType.category ? 0 : _slow,
        parentRef: _type == ChannelType.category ? null : parent,
      ),
    );
  }
}

final class _ChannelTypeTile extends StatelessWidget {
  const _ChannelTypeTile({
    required this.type,
    required this.label,
    required this.icon,
    required this.selected,
    required this.enabled,
    required this.onTap,
  });

  final ChannelType type;
  final String label;
  final IconData icon;
  final bool selected;
  final bool enabled;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Material(
        color: selected ? KaedeColors.selected : KaedeColors.raised,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(
            color: selected ? KaedeColors.coral : KaedeColors.border,
            width: selected ? 1.5 : 1,
          ),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: enabled ? onTap : null,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            child: Row(
              children: [
                Icon(icon,
                    size: 20,
                    color: selected ? KaedeColors.coral : KaedeColors.muted),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(label,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontWeight: FontWeight.w800,
                        color: enabled || selected ? null : KaedeColors.muted,
                      )),
                ),
                if (selected) const Icon(Icons.check_rounded, size: 17),
              ],
            ),
          ),
        ),
      );
}

final class GuildChannelDraft {
  const GuildChannelDraft({
    required this.name,
    required this.topic,
    required this.type,
    required this.slowModeSeconds,
    this.parentRef,
  });

  final String name;
  final String topic;
  final ChannelType type;
  final int slowModeSeconds;
  final EntityRef? parentRef;

  Map<String, Object?> get json => {
        'name': name,
        'type': _channelNumber(type),
        'topic': type == ChannelType.category || topic.isEmpty ? null : topic,
        'rate_limit_per_user':
            type == ChannelType.category ? 0 : slowModeSeconds,
        'parent_id': type == ChannelType.category ? null : parentRef?.id.value,
      };
}

/// Base role grants span both guild-wide administration and the default
/// capabilities inherited by text and voice channels. Keeping this unscoped
/// matches the web role editor and prevents channel-only groups from rendering
/// as empty headings.
List<PermissionMetadata> guildRolePermissionMetadata([String query = '']) {
  final normalized = query.trim().toLowerCase();
  return permissionMetadata
      .where((permission) =>
          normalized.isEmpty ||
          '${permission.label} ${permission.description}'
              .toLowerCase()
              .contains(normalized))
      .toList(growable: false);
}

final class _RoleEditor extends StatefulWidget {
  const _RoleEditor({this.role});
  final KaedeRole? role;
  @override
  State<_RoleEditor> createState() => _RoleEditorState();
}

final class _RoleEditorState extends State<_RoleEditor> {
  late final _name = TextEditingController(text: widget.role?.name ?? '');
  final _permissionSearch = TextEditingController();
  late BigInt _permissions = widget.role?.permissions ?? BigInt.zero;
  late int _color = widget.role?.color ?? 0;
  late bool _hoist = widget.role?.hoist ?? false,
      _mentionable = widget.role?.mentionable ?? false;
  static const _colors = [
    0,
    0x55B998,
    0xF4775F,
    0x3498DB,
    0x9B59B6,
    0xF1C40F,
    0xE67E22,
    0xE91E63,
    0x607D8B
  ];

  @override
  void initState() {
    super.initState();
    _permissionSearch.addListener(_permissionsChanged);
  }

  @override
  void dispose() {
    _name.dispose();
    _permissionSearch
      ..removeListener(_permissionsChanged)
      ..dispose();
    super.dispose();
  }

  void _permissionsChanged() => setState(() {});

  @override
  Widget build(BuildContext context) {
    final query = _permissionSearch.text.trim().toLowerCase();
    final visiblePermissions = guildRolePermissionMetadata(query);
    final groups = visiblePermissions.map((item) => item.group).toSet();
    return Scaffold(
        appBar: AppBar(
            title: Text(widget.role == null
                ? 'Create role'
                : 'Edit ${widget.role!.name}'),
            actions: [
              if (widget.role != null)
                IconButton(
                    onPressed: () => Navigator.pop(
                        context, _RoleDraft(const {}, delete: true)),
                    icon: const Icon(Icons.delete_outline_rounded))
            ]),
        body: ListView(padding: const EdgeInsets.all(16), children: [
          TextField(
              controller: _name,
              decoration: const InputDecoration(labelText: 'Role name')),
          const SizedBox(height: 18),
          Text('Role color', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 10),
          Wrap(spacing: 10, runSpacing: 10, children: [
            for (final color in _colors)
              InkWell(
                  onTap: () => setState(() => _color = color),
                  borderRadius: BorderRadius.circular(14),
                  child: Container(
                      width: 48,
                      height: 48,
                      decoration: BoxDecoration(
                          color: color == 0
                              ? KaedeColors.muted
                              : Color(0xFF000000 | color),
                          borderRadius: BorderRadius.circular(14),
                          border: _color == color
                              ? Border.all(color: KaedeColors.text, width: 3)
                              : null),
                      child: _color == color ? const Icon(Icons.check) : null))
          ]),
          const SizedBox(height: 12),
          SwitchListTile(
              title: const Text('Display separately'),
              value: _hoist,
              onChanged: (value) => setState(() => _hoist = value)),
          SwitchListTile(
              title: const Text('Allow anyone to mention this role'),
              value: _mentionable,
              onChanged: (value) => setState(() => _mentionable = value)),
          const Divider(height: 30),
          Text('Permissions', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 5),
          const Text(
            'Guild permissions and default text or voice channel abilities are granted by roles. Channel overrides can refine them later.',
            style: TextStyle(color: KaedeColors.muted),
          ),
          const SizedBox(height: 14),
          SearchBar(
            controller: _permissionSearch,
            hintText: 'Search permissions',
            leading: const Icon(Icons.search_rounded),
            trailing: [
              if (_permissionSearch.text.isNotEmpty)
                IconButton(
                  tooltip: 'Clear search',
                  onPressed: _permissionSearch.clear,
                  icon: const Icon(Icons.close_rounded),
                ),
            ],
          ),
          const SizedBox(height: 14),
          if (_permissions & BigInt.from(Permission.administrator) !=
              BigInt.zero)
            const Card(
              color: Color(0xFF3A2722),
              child: ListTile(
                leading: Icon(Icons.warning_amber_rounded,
                    color: KaedeColors.warning),
                title:
                    Text('Administrator bypasses every channel restriction.'),
                subtitle: Text(
                    'Only grant it to people who should have unrestricted control of this guild.'),
              ),
            ),
          for (final group in groups)
            if (visiblePermissions
                    .where((item) => item.group == group)
                    .toList(growable: false)
                case final permissions when permissions.isNotEmpty)
              Card(
                margin: const EdgeInsets.only(bottom: 12),
                clipBehavior: Clip.antiAlias,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Padding(
                      padding: const EdgeInsets.fromLTRB(16, 14, 16, 5),
                      child: Text(group,
                          style: Theme.of(context).textTheme.titleLarge),
                    ),
                    for (final permission in permissions)
                      SwitchListTile(
                          contentPadding:
                              const EdgeInsets.symmetric(horizontal: 16),
                          title: Text(permission.label),
                          subtitle: Text(permission.description),
                          value: _permissions & BigInt.from(permission.bit) !=
                              BigInt.zero,
                          onChanged: (value) => setState(() {
                                final bit = BigInt.from(permission.bit);
                                value
                                    ? _permissions |= bit
                                    : _permissions &= ~bit;
                              })),
                  ],
                ),
              ),
          FilledButton.icon(
              onPressed: () => Navigator.pop(
                  context,
                  _RoleDraft({
                    'name': _name.text.trim(),
                    'color': _color,
                    'permissions': '$_permissions',
                    'hoist': _hoist,
                    'mentionable': _mentionable
                  })),
              icon: const Icon(Icons.save_outlined),
              label: const Text('Save role')),
          const SizedBox(height: 12),
        ]));
  }
}

final class _RoleDraft {
  const _RoleDraft(this.json, {this.delete = false});
  final Map<String, Object?> json;
  final bool delete;
}

final class _RoleAssignmentDialog extends StatefulWidget {
  const _RoleAssignmentDialog({required this.member, required this.roles});
  final GuildMember member;
  final List<KaedeRole> roles;
  @override
  State<_RoleAssignmentDialog> createState() => _RoleAssignmentDialogState();
}

final class _RoleAssignmentDialogState extends State<_RoleAssignmentDialog> {
  late final selected = normalizedMemberRoleIds(widget.member.roleIds);
  @override
  Widget build(BuildContext context) => AlertDialog(
          title: Text('Roles for ${widget.member.user.name}'),
          content: SizedBox(
              width: 420,
              child: ListView(shrinkWrap: true, children: [
                for (final role in widget.roles)
                  CheckboxListTile(
                      title: Text(role.name),
                      value: selected.contains(role.ref.id.value),
                      onChanged: (value) => setState(() {
                            value == true
                                ? selected.add(role.ref.id.value)
                                : selected.remove(role.ref.id.value);
                          }))
              ])),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Cancel')),
            FilledButton(
                onPressed: () => Navigator.pop(context, selected),
                child: const Text('Save'))
          ]);
}

final class _PageList extends StatelessWidget {
  const _PageList({required this.children});
  final List<Widget> children;
  @override
  Widget build(BuildContext context) =>
      ListView(padding: const EdgeInsets.all(14), children: children);
}

final class _Panel extends StatelessWidget {
  const _Panel({required this.title, required this.child, this.subtitle});
  final String title;
  final String? subtitle;
  final Widget child;
  @override
  Widget build(BuildContext context) => Card(
      margin: const EdgeInsets.only(bottom: 14),
      child: Padding(
          padding: const EdgeInsets.all(16),
          child:
              Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text(title, style: Theme.of(context).textTheme.titleLarge),
            if (subtitle != null) ...[
              const SizedBox(height: 4),
              Text(subtitle!, style: const TextStyle(color: KaedeColors.muted))
            ],
            const SizedBox(height: 14),
            child
          ])));
}

Future<String?> _prompt(BuildContext context, String title, String label,
    {String? warning}) {
  final input = TextEditingController();
  return showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
              title: Text(title),
              content: Column(mainAxisSize: MainAxisSize.min, children: [
                if (warning != null)
                  Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Text(warning,
                          style: const TextStyle(color: KaedeColors.warning))),
                TextField(
                    controller: input,
                    autofocus: true,
                    decoration: InputDecoration(labelText: label))
              ]),
              actions: [
                TextButton(
                    onPressed: () => Navigator.pop(context),
                    child: const Text('Cancel')),
                FilledButton(
                    onPressed: () => Navigator.pop(context, input.text.trim()),
                    child: const Text('Continue'))
              ]));
}

Future<bool> _confirm(BuildContext context, String title, String body,
        {bool destructive = false}) async =>
    await showDialog<bool>(
        context: context,
        builder: (context) =>
            AlertDialog(title: Text(title), content: Text(body), actions: [
              TextButton(
                  onPressed: () => Navigator.pop(context, false),
                  child: const Text('Cancel')),
              FilledButton(
                  onPressed: () => Navigator.pop(context, true),
                  style: destructive
                      ? FilledButton.styleFrom(
                          backgroundColor: KaedeColors.danger)
                      : null,
                  child: Text(destructive ? 'Delete' : 'Confirm'))
            ])) ??
    false;

void _tabError(BuildContext context, String title, Object error) {
  if (!context.mounted) return;
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(
    content: Text(userFacingError(error, summary: title)),
    backgroundColor: KaedeColors.danger,
  ));
}

EntityRef _mapRef(Map<String, Object?> item, Domain fallback) {
  final user = item['user'];
  if (user is Map) {
    return EntityRef(
        Snowflake('${user['id']}'), Domain('${user['origin_domain']}'));
  }
  return EntityRef(
      Snowflake('${item['id'] ?? item['user_id']}'),
      Domain(
          '${item['origin_domain'] ?? item['user_domain'] ?? fallback.value}'));
}

String _mapName(Map<String, Object?> item) {
  final user = item['user'];
  return user is Map
      ? '${user['display_name'] ?? user['username'] ?? user['id']}'
      : '${item['display_name'] ?? item['username'] ?? item['user_id'] ?? item['id']}';
}

int _channelNumber(ChannelType type) => switch (type) {
      ChannelType.text => 0,
      ChannelType.dm => 1,
      ChannelType.voice => 2,
      ChannelType.category => 4,
      ChannelType.announcement => 5,
      ChannelType.unknown => 0
    };
