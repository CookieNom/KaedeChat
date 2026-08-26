import 'dart:async';
import 'dart:io';
import 'dart:math' as math;

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
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
    final sections =
        <({String label, String description, IconData icon, Widget page})>[
      (
        label: 'Overview',
        description: 'Name, icon, banner and guild-wide defaults.',
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
          description: 'Create channels, reorder them and set permissions.',
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
          description: 'Role colours, permissions and ordering.',
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
          description: 'Nicknames, roles, timeouts, kicks and bans.',
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
          description: 'Banned accounts and blocked instances.',
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
          description: 'Active invite links and who created them.',
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
          description: 'Custom emoji available in this guild.',
          icon: Icons.emoji_emotions_outlined,
          page: _EmojiTab(
            guild: _guild,
            repository: _repository,
            canManage: isOwner || _guild.allows(Permission.manageEmojis),
          )
        ),
      if (isOwner || _guild.allows(Permission.manageEmojis))
        (
          label: 'Stickers',
          description: 'Static and animated stickers available in this guild.',
          icon: Icons.sticky_note_2_outlined,
          page: _StickersTab(
            guild: _guild,
            repository: _repository,
            canManage: isOwner || _guild.allows(Permission.manageEmojis),
          )
        ),
      if (isOwner || _guild.allows(Permission.manageWebhooks))
        (
          label: 'Webhooks',
          description: 'Outgoing integrations that post here.',
          icon: Icons.webhook_rounded,
          page: _WebhooksTab(
            guild: _guild,
            repository: _repository,
            canManage: isOwner || _guild.allows(Permission.manageWebhooks),
          )
        ),
      if (canManageGuild)
        (
          label: 'Bots',
          description: 'Installed bots, grants and automation access.',
          icon: Icons.smart_toy_outlined,
          page: _BotIntegrationsTab(
            guild: _guild,
            repository: _repository,
          ),
        ),
      if (isOwner || _guild.allows(Permission.viewAuditLog))
        (
          label: 'Audit',
          description: 'Recent administrative actions.',
          icon: Icons.receipt_long_outlined,
          page: _AuditTab(
            guild: _guild,
            repository: _repository,
            canView: isOwner || _guild.allows(Permission.viewAuditLog),
            userProfiles: mobile.userProfiles,
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
              : ColoredBox(
                  color: kSettingsSurface,
                  child: Row(
                    children: [
                      NavigationRail(
                        extended: constraints.maxWidth >= 1120,
                        backgroundColor: kSettingsSurface,
                        indicatorColor: kSettingsRowHover,
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
                ),
        );
      }
      return Scaffold(
        appBar: AppBar(title: title),
        body: _loading
            ? const Center(child: CircularProgressIndicator())
            : ColoredBox(
                color: kSettingsSurface,
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(14, 12, 14, 30),
                  children: [
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 4),
                      child: Row(
                        children: [
                          GuildIcon(guild: _guild, size: 56, borderRadius: 16),
                          const SizedBox(width: 14),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  _guild.name,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style:
                                      Theme.of(context).textTheme.headlineSmall,
                                ),
                                const SizedBox(height: 2),
                                Text(
                                  _guild.ref.domain.value,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: KaedeColors.muted,
                                    fontSize: 12.5,
                                  ),
                                ),
                                if (_guild.description?.trim().isNotEmpty ==
                                    true) ...[
                                  const SizedBox(height: 6),
                                  Text(
                                    _guild.description!.trim(),
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                      color: KaedeColors.muted,
                                      fontSize: 12.5,
                                      height: 1.35,
                                    ),
                                  ),
                                ],
                              ],
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 22),
                    for (final section in sections)
                      _SectionRow(
                        label: section.label,
                        description: section.description,
                        icon: section.icon,
                        divider: true,
                        onTap: () => Navigator.of(context).push<void>(
                          MaterialPageRoute<void>(
                            builder: (context) => Scaffold(
                              backgroundColor: kSettingsSurface,
                              appBar: AppBar(title: Text(section.label)),
                              body: section.page,
                            ),
                          ),
                        ),
                      ),
                  ],
                ),
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

/// One tappable settings area in the compact guild settings list: a flat
/// row that fills on hover, Discord style, with a hairline between rows.
final class _SectionRow extends StatelessWidget {
  const _SectionRow({
    required this.label,
    required this.description,
    required this.icon,
    required this.onTap,
    this.divider = false,
  });

  final String label;
  final String description;
  final IconData icon;
  final VoidCallback onTap;
  final bool divider;

  @override
  Widget build(BuildContext context) => Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Material(
            color: Colors.transparent,
            child: InkWell(
              onTap: onTap,
              borderRadius: BorderRadius.circular(10),
              child: Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 4, vertical: 12),
                child: Row(
                  children: [
                    Container(
                      width: 34,
                      height: 34,
                      decoration: BoxDecoration(
                        color: KaedeColors.raised,
                        borderRadius: BorderRadius.circular(9),
                      ),
                      child: Icon(icon, size: 18, color: KaedeColors.coralText),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            label,
                            style: const TextStyle(
                              fontWeight: FontWeight.w600,
                              fontSize: 15,
                            ),
                          ),
                          const SizedBox(height: 1),
                          Text(
                            description,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: KaedeColors.muted,
                              fontSize: 12,
                              height: 1.3,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const Icon(Icons.chevron_right_rounded,
                        size: 18, color: KaedeColors.muted),
                  ],
                ),
              ),
            ),
          ),
          if (divider)
            const Padding(
              padding: EdgeInsets.symmetric(horizontal: 44),
              child: SizedBox(
                height: 1,
                child: DecoratedBox(
                  decoration: BoxDecoration(color: kSettingsDividerColor),
                ),
              ),
            ),
        ],
      );
}

/// The emoji image itself, so the list shows what it is looking at.
final class _EmojiThumbnail extends StatelessWidget {
  const _EmojiThumbnail({required this.emoji, required this.fallbackDomain});

  final Map<String, Object?> emoji;
  final Domain fallbackDomain;

  @override
  Widget build(BuildContext context) {
    final id = '${emoji['id'] ?? ''}';
    final domain = '${emoji['origin_domain'] ?? fallbackDomain.value}';
    if (id.isEmpty || domain.isEmpty) {
      return const Icon(Icons.emoji_emotions_outlined,
          size: 19, color: KaedeColors.muted);
    }
    return ClipRRect(
      borderRadius: BorderRadius.circular(6),
      child: CachedNetworkImage(
        imageUrl:
            Uri.https(domain, '/media/emojis/$id/thumbnail_128').toString(),
        width: 26,
        height: 26,
        fit: BoxFit.contain,
        placeholder: (_, __) => const SizedBox.square(dimension: 26),
        errorWidget: (_, __, ___) => const Icon(
          Icons.emoji_emotions_outlined,
          size: 19,
          color: KaedeColors.muted,
        ),
      ),
    );
  }
}

/// What an invite allows, in one line.
String inviteSummaryLine(Map<String, Object?> invite) {
  final uses = invite['uses'] ?? 0;
  final maximum = invite['max_uses'];
  final expires = invite['expires_at'];
  final parts = <String>[
    maximum is num && maximum > 0 ? '$uses of $maximum uses' : '$uses uses',
  ];
  if (expires is String && expires.isNotEmpty) {
    final at = DateTime.tryParse(expires);
    parts.add(at == null ? 'expires' : 'expires ${_shortDate(at.toLocal())}');
  } else {
    parts.add('never expires');
  }
  return parts.join(' · ');
}

String _shortDate(DateTime value) =>
    '${value.year}-${value.month.toString().padLeft(2, '0')}-'
    '${value.day.toString().padLeft(2, '0')}';

/// Empty state inside a management list.
final class _TabEmpty extends StatelessWidget {
  const _TabEmpty({
    required this.icon,
    required this.title,
    required this.body,
  });

  final IconData icon;
  final String title;
  final String body;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 40),
        child: Column(
          children: [
            Icon(icon, size: 30, color: KaedeColors.muted),
            const SizedBox(height: 12),
            Text(
              title,
              style: const TextStyle(
                fontWeight: FontWeight.w700,
                fontSize: 14.5,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              body,
              textAlign: TextAlign.center,
              style: const TextStyle(color: KaedeColors.muted, fontSize: 13),
            ),
          ],
        ),
      );
}

/// Short guidance above a management list.
final class _TabHint extends StatelessWidget {
  const _TabHint(this.message);

  final String message;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(2, 2, 2, 14),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(
              padding: EdgeInsets.only(top: 1, right: 9),
              child: Icon(Icons.info_outline_rounded,
                  size: 15, color: KaedeColors.muted),
            ),
            Expanded(
              child: Text(
                message,
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 12.5,
                  height: 1.35,
                ),
              ),
            ),
          ],
        ),
      );
}

/// Row shared by the reorderable management lists so channels, roles and
/// members line up with each other.
final class _ManagementRow extends StatelessWidget {
  const _ManagementRow({
    required this.leading,
    required this.title,
    required this.subtitle,
    this.trailing,
    this.badge,
    this.onTap,
    this.indented = false,
  });

  final Widget leading;
  final String title;
  final String subtitle;
  final Widget? trailing;
  final Widget? badge;
  final VoidCallback? onTap;
  final bool indented;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(indented ? 18 : 0, 0, 0, 2),
        child: Material(
          color: Colors.transparent,
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(10),
            child: Padding(
              padding: EdgeInsets.symmetric(
                  horizontal: indented ? 4 : 8, vertical: 9),
              child: Row(
                children: [
                  SizedBox.square(dimension: 30, child: Center(child: leading)),
                  const SizedBox(width: 11),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            Flexible(
                              child: Text(
                                title,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  fontWeight: FontWeight.w600,
                                  fontSize: 14.5,
                                ),
                              ),
                            ),
                            if (badge case final indicator?) ...[
                              const SizedBox(width: 6),
                              indicator,
                            ],
                          ],
                        ),
                        Text(
                          subtitle,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: KaedeColors.muted,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                  if (trailing case final actions?) actions,
                ],
              ),
            ),
          ),
        ),
      );
}

/// Human summary of what a role does, instead of its raw position number.
String roleSummaryLine(KaedeRole role) {
  final parts = <String>[];
  if (role.permissions & BigInt.from(Permission.administrator) != BigInt.zero) {
    parts.add('Administrator');
  } else {
    final granted = countGrantedPermissions(role.permissions);
    parts.add(granted == 0
        ? 'No extra permissions'
        : '$granted permission${granted == 1 ? '' : 's'}');
  }
  if (role.hoist) parts.add('shown separately');
  if (role.mentionable) parts.add('mentionable');
  return parts.join(' · ');
}

/// How many permission bits a role grants.
int countGrantedPermissions(BigInt permissions) {
  var granted = 0;
  for (final permission in permissionMetadata) {
    if (permissions & BigInt.from(permission.bit) != BigInt.zero) granted += 1;
  }
  return granted;
}

/// Type and placement of a channel, for management rows.
String channelSummaryLine(KaedeChannel channel, KaedeChannel? parent) {
  final type = switch (channel.type) {
    ChannelType.category => 'Category',
    ChannelType.voice => 'Voice channel',
    ChannelType.announcement => 'Announcement channel',
    ChannelType.forum => 'Forum channel',
    ChannelType.tracker => 'Task tracker',
    ChannelType.announcementThread ||
    ChannelType.publicThread ||
    ChannelType.privateThread =>
      'Thread',
    _ => 'Text channel',
  };
  final placement = channel.type == ChannelType.category
      ? null
      : parent?.name?.trim().isNotEmpty == true
          ? 'in ${parent!.name!.trim()}'
          : 'no category';
  final topic = channel.topic?.trim();
  return <String>[
    type,
    if (placement != null) placement,
    if (topic?.isNotEmpty == true) topic!,
  ].join(' · ');
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

  Future<void> _chooseHistory(String current) async {
    final chosen = await showSettingsChoiceSheet(
      context,
      title: 'Federated message history',
      description:
          'Whether instances that join later may fetch older messages from this guild.',
      choices: const [
        SettingsChoice('disabled', 'Disabled',
            hint:
                'The recommended default. New instances start with a blank history.'),
        SettingsChoice('full_retained', 'Share permitted history',
            hint: 'Later instances may fetch retained older messages.'),
      ],
      selected: current,
    );
    if (chosen != null && chosen != current && mounted) {
      setState(() => _history = chosen);
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
              GuildIcon(guild: _guild, size: 64, borderRadius: 19),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    OutlinedButton.icon(
                      onPressed: _busy || !widget.canManage
                          ? null
                          : () => _asset('icon'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(0, 38),
                        padding: const EdgeInsets.symmetric(horizontal: 12),
                        textStyle: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      icon: const Icon(Icons.image_outlined, size: 16),
                      label: const Text('Change icon'),
                    ),
                    const SizedBox(height: 6),
                    OutlinedButton.icon(
                      onPressed: _busy || !widget.canManage
                          ? null
                          : () => _asset('banner'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(0, 38),
                        padding: const EdgeInsets.symmetric(horizontal: 12),
                        textStyle: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      icon: const Icon(Icons.panorama_outlined, size: 16),
                      label: const Text('Change banner'),
                    ),
                  ],
                ),
              ),
            ]),
            const SizedBox(height: 18),
            SettingsField(
              label: 'GUILD NAME',
              controller: _name,
              enabled: widget.canManage,
            ),
            const SizedBox(height: 16),
            SettingsField(
              label: 'DESCRIPTION',
              controller: _description,
              maxLines: 4,
              maxLength: 500,
              enabled: widget.canManage,
            ),
            const SizedBox(height: 18),
            SettingsChoiceRow(
              title: 'Federated message history',
              subtitle:
                  'Whether instances that join later may fetch older messages.',
              value: _history,
              display: _history == 'full_retained'
                  ? 'Share permitted history'
                  : 'Disabled',
              onSelected:
                  widget.canManage ? (value) => _chooseHistory(value) : (_) {},
            ),
            const SizedBox(height: 18),
            SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                  onPressed: _busy || !widget.canManage ? null : _save,
                  icon: const Icon(Icons.save_outlined),
                  label: const Text('Save changes')),
            ),
          ]),
        ),
        _Panel(
          title: 'Notifications',
          subtitle: 'What this guild is allowed to notify you about.',
          child: Column(children: [
            if (_notificationError case final warning?) ...[
              DecoratedBox(
                decoration: BoxDecoration(
                  color: KaedeColors.warning.withValues(alpha: 0.1),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(
                      color: KaedeColors.warning.withValues(alpha: .4)),
                ),
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                  child: Row(
                    children: [
                      const Icon(Icons.warning_amber_rounded,
                          size: 18, color: KaedeColors.warning),
                      const SizedBox(width: 10),
                      Expanded(
                          child: Text(warning,
                              style: const TextStyle(
                                  color: KaedeColors.textSoft,
                                  fontSize: 12.5,
                                  height: 1.4))),
                      TextButton(
                        onPressed: _loadNotificationSettings,
                        child: const Text('Retry'),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 10),
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
          child: Column(children: [
            SettingsRow.chevron(
              title: 'Transfer ownership',
              leading: const Padding(
                padding: EdgeInsets.all(3),
                child: Icon(Icons.swap_horiz_rounded,
                    size: 20, color: KaedeColors.muted),
              ),
              divider: true,
              onTap: widget.isOwner ? _transfer : null,
            ),
            SettingsRow.chevron(
              title: 'Leave guild',
              leading: const Padding(
                padding: EdgeInsets.all(3),
                child: Icon(Icons.logout_rounded,
                    size: 20, color: KaedeColors.muted),
              ),
              divider: true,
              onTap: _leave,
            ),
            SettingsRow(
              danger: true,
              title: 'Delete guild',
              leading: const Padding(
                padding: EdgeInsets.all(3),
                child: Icon(Icons.delete_forever_outlined,
                    size: 20, color: KaedeColors.danger),
              ),
              onTap: widget.isOwner ? _delete : null,
            ),
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
        backgroundColor: kSettingsSurface,
        body: ReorderableListView.builder(
          buildDefaultDragHandles: false,
          padding: const EdgeInsets.all(14),
          header: const _TabHint(
            'Press and hold a row to reorder it. Use a row’s menu to move a '
            'channel between categories, or the + on a category to create one '
            'inside it.',
          ),
          itemCount: _channels.length,
          onReorder: widget.canManageChannels ? _reorder : (_, __) {},
          itemBuilder: (context, index) {
            final channel = _channels[index];
            final parent = channel.parentRef == null
                ? null
                : _channels
                    .where((item) => item.ref == channel.parentRef)
                    .firstOrNull;
            return ReorderableDelayedDragStartListener(
              key: ValueKey(channel.ref.wire),
              index: index,
              enabled: widget.canManageChannels,
              child: _ManagementRow(
                indented: channel.parentRef != null,
                onTap: widget.canManageChannels ? () => _edit(channel) : null,
                leading: Icon(
                  switch (channel.type) {
                    ChannelType.category => Icons.folder_outlined,
                    ChannelType.voice => Icons.volume_up_rounded,
                    ChannelType.announcement => Icons.campaign_rounded,
                    ChannelType.forum => Icons.forum_outlined,
                    ChannelType.tracker => Icons.view_kanban_outlined,
                    ChannelType.announcementThread ||
                    ChannelType.publicThread ||
                    ChannelType.privateThread =>
                      Icons.forum_outlined,
                    _ => Icons.tag_rounded
                  },
                  size: 20,
                  color: KaedeColors.muted,
                ),
                title: channel.name ?? 'channel',
                subtitle: channelSummaryLine(channel, parent),
                badge: channel.encryptionMode == 'e2ee'
                    ? const Icon(Icons.lock_rounded,
                        size: 13, color: KaedeColors.mint)
                    : null,
                trailing: _channelActions(channel, index),
              ),
            );
          },
        ),
        floatingActionButton: FloatingActionButton.extended(
            onPressed: widget.canManageChannels ? _create : null,
            icon: const Icon(Icons.add_rounded),
            label: const Text('Create channel')),
      );

  Widget _channelActions(KaedeChannel channel, int index) {
    if (!widget.canManageChannels && !widget.canManagePermissions) {
      return const SizedBox.square(
        dimension: 44,
        child: Icon(Icons.lock_outline_rounded,
            size: 18, color: KaedeColors.muted),
      );
    }
    return Row(mainAxisSize: MainAxisSize.min, children: [
      PopupMenuButton<String>(
        onSelected: (action) => action == 'permissions'
            ? _permissions(channel)
            : action == 'encryption'
                ? _encryption(channel)
                : action == 'delete'
                    ? _delete(channel)
                    : action == 'move'
                        ? _moveToCategory(channel)
                        : _edit(channel),
        itemBuilder: (_) => [
          if (widget.canManageChannels)
            const PopupMenuItem(value: 'edit', child: Text('Edit channel')),
          if (widget.canManageChannels && channel.type != ChannelType.category)
            const PopupMenuItem(value: 'move', child: Text('Move to category')),
          if (widget.canManagePermissions)
            const PopupMenuItem(
                value: 'permissions', child: Text('Permissions')),
          if (widget.canManageChannels &&
              {
                ChannelType.text,
                ChannelType.announcement,
                ChannelType.voice,
              }.contains(channel.type) &&
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
      if (widget.canManageChannels) ...[
        if (channel.type == ChannelType.category)
          Tooltip(
            message: 'Add a channel to this category',
            child: InkWell(
              onTap: () => _createChannel(initialParent: channel.ref),
              borderRadius: BorderRadius.circular(10),
              child: const SizedBox.square(
                dimension: 44,
                child: Icon(Icons.add_rounded, color: KaedeColors.muted),
              ),
            ),
          )
        else
          ReorderableDragStartListener(
            index: index,
            child: const Tooltip(
              message: 'Drag to reorder',
              child: SizedBox.square(
                dimension: 44,
                child:
                    Icon(Icons.drag_handle_rounded, color: KaedeColors.muted),
              ),
            ),
          ),
      ],
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

  /// Discord's "Move to category": a radio-style choice sheet, reachable
  /// from every non-category row's menu instead of digging through the
  /// edit form.
  Future<void> _moveToCategory(KaedeChannel channel) async {
    final categories = [
      for (final candidate in _channels)
        if (candidate.type == ChannelType.category &&
            candidate.ref != channel.ref)
          candidate,
    ]..sort((a, b) => a.position.compareTo(b.position));
    final current = channel.parentRef?.wire ?? '';
    final chosen = await showSettingsChoiceSheet(
      context,
      title: 'Move to category',
      description: 'Choose where ${channel.name ?? 'the channel'} lives.',
      selected: categories.any((category) => category.ref.wire == current)
          ? current
          : '',
      choices: [
        const SettingsChoice('', 'No category'),
        for (final category in categories)
          SettingsChoice(category.ref.wire, category.name ?? 'Category'),
      ],
    );
    if (chosen == null) return;
    final target = chosen.isEmpty
        ? null
        : categories.firstWhere((category) => category.ref.wire == chosen).ref;
    if (target == channel.parentRef) return;
    final previous = [..._channels];
    final index = _channels.indexWhere((item) => item.ref == channel.ref);
    if (index < 0) return;
    final moved = _withParent(channel, target);
    final next = [..._channels]..[index] = moved;
    try {
      // The batch reorder endpoint is the channel of record for parents, so
      // it persists the full ordering — including the new parent — atomically.
      await widget.repository.reorderChannels(
        widget.guild.ref,
        guildChannelPositionRequest(next),
      );
      if (!mounted) return;
      setState(() => _channels = next);
      final refreshed = await widget.changed('Channel moved');
      if (mounted) _reconcileChannels(refreshed, preserve: moved);
    } on Object catch (error) {
      if (!mounted) return;
      setState(() => _channels = previous);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not move the channel',
        )),
        backgroundColor: KaedeColors.danger,
      ));
    }
  }

  KaedeChannel _withParent(KaedeChannel channel, EntityRef? parent) =>
      KaedeChannel(
        ref: channel.ref,
        type: channel.type,
        position: channel.position,
        permissions: channel.permissions,
        createdAt: channel.createdAt,
        guildRef: channel.guildRef,
        name: channel.name,
        topic: channel.topic,
        parentRef: parent,
        lastMessageRef: channel.lastMessageRef,
        recipients: channel.recipients,
        conversationType: channel.conversationType,
        ownerRef: channel.ownerRef,
        slowModeSeconds: channel.slowModeSeconds,
        permissionsSynced: channel.permissionsSynced,
        historyTruncated: channel.historyTruncated,
        historyRetention: channel.historyRetention,
        federatedHistoryPolicy: channel.federatedHistoryPolicy,
        historyRemoteAvailable: channel.historyRemoteAvailable,
        oldestAvailableMessageRef: channel.oldestAvailableMessageRef,
        historyDegradedCode: channel.historyDegradedCode,
        encryptionMode: channel.encryptionMode,
        encryptionState: channel.encryptionState,
        encryptionPolicyGeneration: channel.encryptionPolicyGeneration,
        encryptionProtocol: channel.encryptionProtocol,
        encryptionSuite: channel.encryptionSuite,
        encryptionGroupId: channel.encryptionGroupId,
        encryptionEpoch: channel.encryptionEpoch,
        encryptionActivatedAt: channel.encryptionActivatedAt,
        searchAvailable: channel.searchAvailable,
        flags: channel.flags,
        availableTags: channel.availableTags,
        defaultReactionEmoji: channel.defaultReactionEmoji,
        defaultThreadRateLimitPerUser: channel.defaultThreadRateLimitPerUser,
        defaultAutoArchiveDuration: channel.defaultAutoArchiveDuration,
        defaultSortOrder: channel.defaultSortOrder,
        defaultForumLayout: channel.defaultForumLayout,
        e2eeRequired: channel.e2eeRequired,
        version: channel.version,
      );

  Future<void> _create() => _createChannel();

  /// Opens the creation editor, optionally pinned to [initialParent] so a
  /// new channel lands inside a category — the per-category "+" flow.
  Future<void> _createChannel({EntityRef? initialParent}) async {
    final value = await showGuildChannelEditorSheet(
      context,
      channels: _channels,
      initialParent: initialParent,
      e2eeActivationEnabled: _e2eeActivationEnabled,
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
      e2eeActivationEnabled: _e2eeActivationEnabled,
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
        backgroundColor: kSettingsSurface,
        body: ReorderableListView.builder(
          buildDefaultDragHandles: false,
          padding: const EdgeInsets.all(14),
          header: const _TabHint(
            'Roles are ranked. A member can only manage roles below their own '
            'highest role. Press and hold a row to reorder; changes save '
            'immediately.',
          ),
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
            return ReorderableDelayedDragStartListener(
              key: ValueKey(role.ref.wire),
              index: index,
              enabled: _canMove(role),
              child: _ManagementRow(
                onTap: _canMove(role) ? () => _edit(role) : null,
                leading: Container(
                  width: 30,
                  height: 30,
                  decoration: BoxDecoration(
                    color: (role.color == 0
                            ? KaedeColors.muted
                            : Color(0xFF000000 | role.color))
                        .withValues(alpha: .18),
                    shape: BoxShape.circle,
                  ),
                  child: Center(
                    child: Container(
                      width: 12,
                      height: 12,
                      decoration: BoxDecoration(
                        color: role.color == 0
                            ? KaedeColors.muted
                            : Color(0xFF000000 | role.color),
                        shape: BoxShape.circle,
                      ),
                    ),
                  ),
                ),
                title: role.name,
                subtitle: roleSummaryLine(role),
                trailing: _canMove(role)
                    ? ReorderableDragStartListener(
                        index: index,
                        child: const Tooltip(
                          message: 'Drag to reorder',
                          child: SizedBox.square(
                            dimension: 44,
                            child: Icon(Icons.drag_handle_rounded,
                                color: KaedeColors.muted),
                          ),
                        ),
                      )
                    : const Tooltip(
                        message: 'This role is above your role ceiling',
                        child: SizedBox.square(
                          dimension: 44,
                          child: Icon(Icons.lock_outline_rounded,
                              size: 18, color: KaedeColors.muted),
                        ),
                      ),
              ),
            );
          },
        ),
        floatingActionButton: FloatingActionButton.extended(
            onPressed: widget.actorRef == widget.guild.ownerRef ||
                    widget.guild.allows(Permission.manageRoles)
                ? () => _edit(null)
                : null,
            icon: const Icon(Icons.add_rounded),
            label: const Text('Create role')),
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
      final iconFile = draft.iconFile;
      if (saved != null && iconFile != null) {
        final contentType = imageUploadContentType(iconFile.name,
            reportedType: iconFile.mimeType);
        if (contentType == null) {
          throw const FormatException(
              'Choose a PNG, JPEG, GIF, or WebP image.');
        }
        saved = await widget.repository.uploadRoleIcon(
          guild: widget.guild.ref,
          role: saved.ref,
          filename: iconFile.name,
          contentType: contentType,
          file: File(iconFile.path),
        );
        _upsertRole(saved);
      } else if (saved != null && draft.removeIcon && saved.iconHash != null) {
        saved =
            await widget.repository.deleteRoleIcon(widget.guild.ref, saved.ref);
        _upsertRole(saved);
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
  Widget build(BuildContext context) => ColoredBox(
        color: kSettingsSurface,
        child: Column(children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 12),
            child: TextField(
              controller: _search,
              textInputAction: TextInputAction.search,
              onChanged: (_) => setState(() {}),
              onSubmitted: (_) => _load(reset: true),
              style: const TextStyle(fontSize: 14),
              decoration: InputDecoration(
                hintText: 'Search members',
                hintStyle:
                    const TextStyle(color: KaedeColors.muted, fontSize: 13.5),
                prefixIcon: const Icon(Icons.search_rounded,
                    size: 18, color: KaedeColors.muted),
                suffixIcon: _search.text.isEmpty
                    ? null
                    : IconButton(
                        tooltip: 'Clear search',
                        onPressed: () {
                          _search.clear();
                          _load(reset: true);
                        },
                        icon: const Icon(Icons.close_rounded, size: 18),
                      ),
                isDense: true,
                contentPadding: const EdgeInsets.symmetric(vertical: 10),
                filled: true,
                fillColor: KaedeColors.canvas,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: const BorderSide(color: KaedeColors.border),
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: const BorderSide(color: KaedeColors.border),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide:
                      const BorderSide(color: KaedeColors.coral, width: 1.4),
                ),
              ),
            ),
          ),
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
                          final roleCount = member.roleIds.length;
                          return Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 14),
                            child: _ManagementRow(
                              leading:
                                  UserAvatar(user: member.user, radius: 15),
                              title: member.nickname ?? member.user.name,
                              subtitle: member.user.profileResolved
                                  ? <String>[
                                      member.user.handle,
                                      if (roleCount > 0)
                                        '$roleCount role'
                                            '${roleCount == 1 ? '' : 's'}',
                                      if (member.timeoutUntil != null)
                                        'timed out',
                                    ].join(' · ')
                                  : 'Profile unavailable · refreshes '
                                      'automatically',
                              badge: member.user.ref == widget.guild.ownerRef
                                  ? const Icon(Icons.workspace_premium_rounded,
                                      size: 13, color: KaedeColors.warning)
                                  : null,
                              trailing: actions.isEmpty
                                  ? null
                                  : PopupMenuButton<String>(
                                      tooltip: 'Member actions',
                                      position: PopupMenuPosition.under,
                                      onSelected: (value) =>
                                          _action(member, value),
                                      itemBuilder: (_) => actions,
                                    ),
                              onTap: _canAssignRoles(member)
                                  ? () => _action(member, 'roles')
                                  : null,
                            ),
                          );
                        },
                      ))),
        ]),
      );

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
          final choice = await showModerationOptions(
            context,
            title: 'Timeout ${member.user.name}',
            timeout: true,
          );
          if (choice == null) return;
          final indefinite = choice.durationSeconds < 0;
          await widget.repository.updateMember(
            widget.guild.ref,
            member.user.ref,
            <String, Object?>{
              'timeout_until': indefinite
                  ? null
                  : DateTime.now()
                      .toUtc()
                      .add(Duration(seconds: choice.durationSeconds))
                      .toIso8601String(),
              'timeout_indefinite': indefinite,
            },
            reason: choice.reason,
          );
          break;
        case 'kick':
          final reason = await _prompt(
              context, 'Kick ${member.user.name}?', 'Reason (optional)');
          if (reason == null) return;
          await widget.repository
              .kick(widget.guild.ref, member.user.ref, reason: reason);
          break;
        case 'ban':
          final choice = await showModerationOptions(
            context,
            title: 'Ban ${member.user.name}?',
            includeDeleteHistory: true,
          );
          if (choice == null) return;
          await widget.repository.ban(
            widget.guild.ref,
            member.user.ref,
            reason: choice.reason,
            expiresAt: choice.durationSeconds == 0
                ? null
                : DateTime.now()
                    .toUtc()
                    .add(Duration(seconds: choice.durationSeconds)),
            deleteMessageSeconds: choice.deleteMessageSeconds,
          );
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
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 8),
                    child: Row(
                      children: [
                        Icon(Icons.lock_outline_rounded,
                            size: 19, color: KaedeColors.muted),
                        SizedBox(width: 11),
                        Text('You cannot manage member bans',
                            style: TextStyle(
                                color: KaedeColors.muted, fontSize: 13.5)),
                      ],
                    ),
                  ),
                if (_bans.isEmpty)
                  if (widget.canBanMembers)
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 8),
                      child: Text('No banned members',
                          style: TextStyle(
                              color: KaedeColors.muted, fontSize: 13.5)),
                    ),
                for (final ban in _bans)
                  SettingsRow(
                      title: _mapName(ban),
                      subtitle: '${ban['reason'] ?? 'No reason'}',
                      leading: const Padding(
                        padding: EdgeInsets.all(3),
                        child: Icon(Icons.person_outline_rounded,
                            size: 20, color: KaedeColors.muted),
                      ),
                      divider: true,
                      trailing: TextButton(
                          onPressed: widget.canBanMembers
                              ? () async {
                                  try {
                                    final reason = await _prompt(
                                      context,
                                      'Unban ${_mapName(ban)}?',
                                      'Audit reason (optional)',
                                    );
                                    if (reason == null) return;
                                    await widget.repository.unban(
                                      widget.guild.ref,
                                      _mapRef(ban, widget.guild.ref.domain),
                                      reason: reason,
                                    );
                                    await _load();
                                  } on Object catch (error) {
                                    if (mounted) {
                                      _tabError(this.context,
                                          'Could not remove member ban', error);
                                    }
                                  }
                                }
                              : null,
                          style: TextButton.styleFrom(
                            foregroundColor: KaedeColors.danger,
                            minimumSize: const Size(0, 34),
                            padding: const EdgeInsets.symmetric(horizontal: 10),
                          ),
                          child: const Text('Unban'))),
              ])),
          _Panel(
              title: 'Banned instances',
              subtitle:
                  'This prevents every account hosted by that domain from joining. It may exclude innocent users and does not erase copies already held by a malicious peer.',
              child: Column(children: [
                SettingsRow.chevron(
                    title: 'Ban an instance',
                    leading: const Padding(
                      padding: EdgeInsets.all(3),
                      child: Icon(Icons.public_off_rounded,
                          size: 20, color: KaedeColors.muted),
                    ),
                    divider: true,
                    onTap: widget.canBanInstances ? _addInstance : null),
                for (final ban in _instances)
                  if (guildInstanceBanDomain(ban) case final domain?)
                    SettingsRow(
                        title: domain.value,
                        subtitle: '${ban['reason'] ?? 'No reason'}'.isEmpty
                            ? 'Every account on this domain is blocked.'
                            : '${ban['reason'] ?? ''}',
                        leading: const Padding(
                          padding: EdgeInsets.all(3),
                          child: Icon(Icons.public_rounded,
                              size: 20, color: KaedeColors.muted),
                        ),
                        divider: true,
                        trailing: TextButton(
                            onPressed: widget.canBanInstances
                                ? () async {
                                    try {
                                      final reason = await _prompt(
                                        context,
                                        'Remove ${domain.value} ban?',
                                        'Audit reason (optional)',
                                      );
                                      if (reason == null) return;
                                      await widget.repository.unbanInstance(
                                        widget.guild.ref,
                                        domain,
                                        reason: reason,
                                      );
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
                            style: TextButton.styleFrom(
                              foregroundColor: KaedeColors.danger,
                              minimumSize: const Size(0, 34),
                              padding:
                                  const EdgeInsets.symmetric(horizontal: 10),
                            ),
                            child: const Text('Remove')))
                  else
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 8),
                      child: Row(
                        children: [
                          Icon(Icons.warning_amber_rounded,
                              size: 19, color: KaedeColors.warning),
                          SizedBox(width: 11),
                          Expanded(
                            child: Text(
                              'Invalid instance-ban record. Refresh or contact the instance operator.',
                              style: TextStyle(
                                  color: KaedeColors.muted, fontSize: 13.5),
                            ),
                          ),
                        ],
                      ),
                    ),
              ])),
        ]);
  Future<void> _addInstance() async {
    final domainInput = TextEditingController();
    final choice = await showModerationOptions(
      context,
      title: 'Ban an entire instance?',
      leadingField: domainInput,
      leadingLabel: 'Instance domain',
    );
    final value = domainInput.text.trim();
    domainInput.dispose();
    if (choice == null || value.isEmpty) return;
    try {
      late final Domain domain;
      try {
        domain = Domain(value);
      } on FormatException {
        throw const UserInputException(
          'Enter a valid instance hostname, such as chat.example.',
        );
      }
      await widget.repository.banInstance(
        widget.guild.ref,
        domain,
        reason: choice.reason,
        expiresAt: choice.durationSeconds == 0
            ? null
            : DateTime.now()
                .toUtc()
                .add(Duration(seconds: choice.durationSeconds)),
      );
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
      backgroundColor: kSettingsSurface,
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 90),
              children: [
                const _TabHint(
                  'Anyone with an invite link can join. Treat private invites '
                  'like passwords.',
                ),
                if (!widget.canManage)
                  const _TabHint(
                    'Manage Guild is required to list or revoke invites.',
                  ),
                if (widget.canManage && _items.isEmpty)
                  const _TabEmpty(
                    icon: Icons.link_off_rounded,
                    title: 'No active invites',
                    body: 'Create one to bring people in.',
                  ),
                for (final item in _items)
                  _ManagementRow(
                    leading: const Icon(Icons.link_rounded,
                        size: 19, color: KaedeColors.muted),
                    title: '${item['code']}',
                    subtitle: inviteSummaryLine(item),
                    onTap: widget.canManage
                        ? () => _copyInvite('${item['code']}')
                        : null,
                    trailing: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        IconButton(
                          tooltip: 'Copy invite link',
                          onPressed: () => _copyInvite('${item['code']}'),
                          icon: const Icon(Icons.copy_rounded, size: 18),
                        ),
                        IconButton(
                          tooltip: 'Revoke invite',
                          style: IconButton.styleFrom(
                            foregroundColor: KaedeColors.danger,
                          ),
                          onPressed: widget.canManage
                              ? () async {
                                  if (!await _confirm(
                                    context,
                                    'Revoke ${item['code']}?',
                                    'Anyone still holding this link will no '
                                        'longer be able to join.',
                                    destructive: true,
                                  )) {
                                    return;
                                  }
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
                          icon: const Icon(Icons.delete_outline_rounded,
                              size: 18),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: widget.canCreate ? _create : null,
          icon: const Icon(Icons.person_add_alt_1),
          label: const Text('Create invite')));

  Future<void> _copyInvite(String code) async {
    final host = widget.guild.ref.domain.value;
    await Clipboard.setData(ClipboardData(text: 'https://$host/invite/$code'));
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Invite link copied.')),
    );
  }

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
    final restrictions = await showInviteRestrictions(context);
    if (restrictions == null || !mounted) return;
    try {
      await widget.repository.createInvite(widget.guild.ref, {
        'channel_id': channel.ref.id.value,
        'max_age_seconds': restrictions.$1,
        'max_uses': restrictions.$2,
      });
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create invite', error);
    }
  }
}

Future<(int?, int?)?> showInviteRestrictions(BuildContext context) async {
  var age = 604800;
  var uses = 100;
  return showDialog<(int?, int?)>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setDialogState) => AlertDialog(
        title: const Text('Invite limits'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            DropdownButtonFormField<int>(
              initialValue: age,
              decoration: const InputDecoration(labelText: 'Expires after'),
              items: const [
                DropdownMenuItem(value: 1800, child: Text('30 minutes')),
                DropdownMenuItem(value: 21600, child: Text('6 hours')),
                DropdownMenuItem(value: 86400, child: Text('1 day')),
                DropdownMenuItem(value: 604800, child: Text('7 days')),
                DropdownMenuItem(value: 0, child: Text('Never')),
              ],
              onChanged: (value) => setDialogState(() => age = value ?? age),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<int>(
              initialValue: uses,
              decoration: const InputDecoration(labelText: 'Maximum uses'),
              items: const [
                DropdownMenuItem(value: 1, child: Text('1 use')),
                DropdownMenuItem(value: 5, child: Text('5 uses')),
                DropdownMenuItem(value: 10, child: Text('10 uses')),
                DropdownMenuItem(value: 25, child: Text('25 uses')),
                DropdownMenuItem(value: 100, child: Text('100 uses')),
                DropdownMenuItem(value: 0, child: Text('Unlimited')),
              ],
              onChanged: (value) => setDialogState(() => uses = value ?? uses),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(
              dialogContext,
              (age == 0 ? null : age, uses == 0 ? null : uses),
            ),
            child: const Text('Create invite'),
          ),
        ],
      ),
    ),
  );
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
      backgroundColor: kSettingsSurface,
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 90),
              children: [
                const _TabHint(
                  'Custom emoji work in other guilds for members who are '
                  'allowed to use external emoji. Federated tokens keep the '
                  'emoji\u2019s origin.',
                ),
                if (_items.isEmpty)
                  const _TabEmpty(
                    icon: Icons.emoji_emotions_outlined,
                    title: 'No custom emoji yet',
                    body: 'Upload a PNG, GIF or WebP up to 256 KB.',
                  ),
                for (final item in _items)
                  _ManagementRow(
                    leading: _EmojiThumbnail(
                      emoji: item,
                      fallbackDomain: widget.guild.ref.domain,
                    ),
                    title: ':${item['name']}:',
                    subtitle: '${item['origin_domain']}',
                    trailing: IconButton(
                      tooltip: 'Delete emoji',
                      style: IconButton.styleFrom(
                        foregroundColor: KaedeColors.danger,
                      ),
                      onPressed: widget.canManage
                          ? () async {
                              if (!await _confirm(
                                context,
                                'Delete :${item['name']}:?',
                                'Messages that already use it will show the '
                                    'name instead.',
                                destructive: true,
                              )) {
                                return;
                              }
                              try {
                                await widget.repository.deleteEmoji(
                                    widget.guild.ref,
                                    _mapRef(item, widget.guild.ref.domain));
                                await _load();
                              } on Object catch (error) {
                                if (mounted) {
                                  _tabError(this.context,
                                      'Could not delete emoji', error);
                                }
                              }
                            }
                          : null,
                      icon: const Icon(Icons.delete_outline_rounded, size: 18),
                    ),
                  ),
              ],
            ),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: widget.canManage ? _upload : null,
          icon: const Icon(Icons.add_photo_alternate_outlined),
          label: const Text('Upload emoji')));
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

final class _StickersTab extends StatefulWidget {
  const _StickersTab({
    required this.guild,
    required this.repository,
    required this.canManage,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canManage;

  @override
  State<_StickersTab> createState() => _StickersTabState();
}

final class _StickersTabState extends State<_StickersTab> {
  List<ComposerSticker> _items = const [];
  var _loading = true;
  var _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant _StickersTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final response = await widget.repository.stickers();
      final items = response
          .map(ComposerSticker.tryParse)
          .whereType<ComposerSticker>()
          .where((item) => item.guildRef == widget.guild.ref)
          .toList(growable: false);
      if (!mounted) return;
      setState(() {
        _items = items;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      _tabError(context, 'Could not load stickers', error);
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: kSettingsSurface,
        body: _loading
            ? const Center(child: CircularProgressIndicator())
            : ListView(
                padding: const EdgeInsets.fromLTRB(14, 12, 14, 90),
                children: [
                  _TabHint(
                    'Crop an image in the app and optionally remove its '
                    'background. Animated GIF stickers keep their animation. '
                    '${_items.length} of ${widget.guild.stickerLimit} used.',
                  ),
                  if (_items.isEmpty)
                    _TabEmpty(
                      icon: Icons.sticky_note_2_outlined,
                      title: 'No stickers yet',
                      body: 'Upload a PNG, JPEG, GIF or WebP up to '
                          '${(widget.guild.stickerMaxBytes / 1048576).ceil()} MiB.',
                    ),
                  for (final sticker in _items)
                    _ManagementRow(
                      leading: StickerImage(sticker: sticker, size: 46),
                      title: sticker.name,
                      subtitle: sticker.description ??
                          (sticker.animated ? 'Animated sticker' : 'Sticker'),
                      trailing: IconButton(
                        tooltip: 'Delete sticker',
                        style: IconButton.styleFrom(
                          foregroundColor: KaedeColors.danger,
                        ),
                        onPressed: !widget.canManage || _busy
                            ? null
                            : () => _delete(sticker),
                        icon:
                            const Icon(Icons.delete_outline_rounded, size: 18),
                      ),
                    ),
                ],
              ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: widget.canManage &&
                  !_busy &&
                  _items.length < widget.guild.stickerLimit
              ? _upload
              : null,
          icon: const Icon(Icons.add_photo_alternate_outlined),
          label: Text(_busy ? 'Creating…' : 'Create sticker'),
        ),
      );

  Future<void> _delete(ComposerSticker sticker) async {
    if (!await _confirm(
      context,
      'Delete ${sticker.name}?',
      'Messages that already use it will show the sticker name instead.',
      destructive: true,
    )) {
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.repository.deleteSticker(widget.guild.ref, sticker.ref);
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not delete sticker', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _upload() async {
    final selected = await ImagePicker().pickImage(source: ImageSource.gallery);
    if (selected == null || !mounted) return;
    final contentType = imageUploadContentType(
      selected.name,
      reportedType: selected.mimeType,
    );
    if (contentType == null) {
      _tabError(context, 'Could not create sticker',
          'Choose a PNG, JPEG, GIF, or WebP image.');
      return;
    }
    final file = File(selected.path);
    if (await file.length() > widget.guild.stickerMaxBytes) {
      if (mounted) {
        _tabError(context, 'Could not create sticker',
            'Sticker images can be at most ${(widget.guild.stickerMaxBytes / 1048576).ceil()} MiB.');
      }
      return;
    }
    if (!mounted) return;
    final edit = await showStickerEditor(
      context,
      file: file,
      animated: contentType == 'image/gif',
      backgroundRemovalAvailable: widget.guild.stickerBackgroundRemovalEnabled,
    );
    if (edit == null || !mounted) return;
    setState(() => _busy = true);
    try {
      await widget.repository.uploadSticker(
        guild: widget.guild.ref,
        name: edit.name,
        description: edit.description,
        filename: selected.name,
        contentType: contentType,
        file: file,
        cropX: edit.cropX,
        cropY: edit.cropY,
        cropWidth: edit.cropWidth,
        cropHeight: edit.cropHeight,
        removeBackground: edit.removeBackground,
      );
      await _load();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('${edit.name} is ready to use.')),
        );
      }
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create sticker', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }
}

typedef StickerEdit = ({
  String name,
  String? description,
  double cropX,
  double cropY,
  double cropWidth,
  double cropHeight,
  bool removeBackground,
});

Future<StickerEdit?> showStickerEditor(
  BuildContext context, {
  required File file,
  required bool animated,
  required bool backgroundRemovalAvailable,
}) {
  var name = '';
  var description = '';
  var cropWidth = 1.0;
  var cropHeight = 1.0;
  var cropX = 0.0;
  var cropY = 0.0;
  var removeBackground = false;
  return showDialog<StickerEdit>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setDialogState) {
        final validName = RegExp(r'^[A-Za-z0-9_]{2,32}$').hasMatch(name.trim());
        return AlertDialog(
          title: const Text('Create sticker'),
          content: SizedBox(
            width: 430,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  TextField(
                    key: const ValueKey('sticker-name'),
                    maxLength: 32,
                    decoration: const InputDecoration(
                      labelText: 'Name',
                      helperText: '2–32 letters, numbers, or underscores',
                    ),
                    onChanged: (value) => setDialogState(() => name = value),
                  ),
                  TextField(
                    key: const ValueKey('sticker-description'),
                    maxLength: 100,
                    decoration: const InputDecoration(
                      labelText: 'Description (optional)',
                    ),
                    onChanged: (value) => description = value,
                  ),
                  const SizedBox(height: 12),
                  _StickerCropper(
                    file: file,
                    crop: (
                      x: cropX,
                      y: cropY,
                      width: cropWidth,
                      height: cropHeight,
                    ),
                    onChanged: (crop) => setDialogState(() {
                      cropX = crop.x;
                      cropY = crop.y;
                      cropWidth = crop.width;
                      cropHeight = crop.height;
                    }),
                  ),
                  const SizedBox(height: 10),
                  Text(
                    key: const ValueKey('sticker-crop-summary'),
                    'Selection: ${(cropWidth * 100).round()}% × '
                    '${(cropHeight * 100).round()}%',
                    style: const TextStyle(
                      color: KaedeColors.muted,
                      fontSize: 12,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          'Drag the box to move it. Drag a corner to resize.',
                          style: const TextStyle(
                            color: KaedeColors.muted,
                            fontSize: 12,
                          ),
                        ),
                      ),
                      TextButton(
                        key: const ValueKey('sticker-crop-reset'),
                        onPressed: () => setDialogState(() {
                          cropX = 0;
                          cropY = 0;
                          cropWidth = 1;
                          cropHeight = 1;
                        }),
                        child: const Text('Reset'),
                      ),
                    ],
                  ),
                  SwitchListTile(
                    key: const ValueKey('sticker-remove-background'),
                    contentPadding: EdgeInsets.zero,
                    title: const Text('Remove background'),
                    subtitle: Text(animated
                        ? 'Background removal is unavailable for animated GIFs.'
                        : backgroundRemovalAvailable
                            ? 'Creates a transparent cutout on the server.'
                            : 'This server has not enabled background removal.'),
                    value: removeBackground,
                    onChanged: animated || !backgroundRemovalAvailable
                        ? null
                        : (value) =>
                            setDialogState(() => removeBackground = value),
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('Cancel'),
            ),
            FilledButton.icon(
              onPressed: validName
                  ? () => Navigator.pop(dialogContext, (
                        name: name.trim(),
                        description: description.trim().isEmpty
                            ? null
                            : description.trim(),
                        cropX: cropX,
                        cropY: cropY,
                        cropWidth: cropWidth,
                        cropHeight: cropHeight,
                        removeBackground: removeBackground,
                      ))
                  : null,
              icon: const Icon(Icons.add_photo_alternate_outlined),
              label: const Text('Create'),
            ),
          ],
        );
      },
    ),
  );
}

typedef _NormalizedStickerCrop = ({
  double x,
  double y,
  double width,
  double height,
});

enum _StickerCropGesture { move, northwest, northeast, southwest, southeast }

final class _StickerCropper extends StatefulWidget {
  const _StickerCropper({
    required this.file,
    required this.crop,
    required this.onChanged,
  });

  final File file;
  final _NormalizedStickerCrop crop;
  final ValueChanged<_NormalizedStickerCrop> onChanged;

  @override
  State<_StickerCropper> createState() => _StickerCropperState();
}

final class _StickerCropperState extends State<_StickerCropper> {
  static const _minimumSize = .1;
  late FileImage _provider;
  ImageStream? _stream;
  ImageStreamListener? _listener;
  var _aspectRatio = 1.0;
  int? _activePointer;
  _StickerCropGesture? _gesture;

  @override
  void initState() {
    super.initState();
    _provider = FileImage(widget.file);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _resolveImage();
  }

  @override
  void didUpdateWidget(covariant _StickerCropper oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.file.path != widget.file.path) {
      _stream?.removeListener(_listener!);
      _stream = null;
      _provider = FileImage(widget.file);
      _resolveImage();
    }
  }

  void _resolveImage() {
    if (_stream != null) return;
    final listener = ImageStreamListener((info, _) {
      final width = info.image.width;
      final height = info.image.height;
      if (!mounted || width <= 0 || height <= 0) return;
      setState(() => _aspectRatio = width / height);
    });
    _listener = listener;
    _stream = _provider.resolve(createLocalImageConfiguration(context))
      ..addListener(listener);
  }

  @override
  void dispose() {
    if (_listener case final listener?) _stream?.removeListener(listener);
    super.dispose();
  }

  double _clamp(double value, double minimum, double maximum) =>
      value.clamp(minimum, maximum).toDouble();

  void _beginGesture(PointerDownEvent event, _StickerCropGesture gesture) {
    _activePointer = event.pointer;
    _gesture = gesture;
  }

  void _updateGesture(PointerMoveEvent event, Size size) {
    if (_activePointer != event.pointer) return;
    if (_gesture == _StickerCropGesture.move) {
      _move(event.delta, size);
    } else if (_gesture case final gesture?) {
      _resize(gesture, event.delta, size);
    }
  }

  void _endGesture(PointerEvent event) {
    if (_activePointer != event.pointer) return;
    _activePointer = null;
    _gesture = null;
  }

  void _move(Offset delta, Size size) {
    final crop = widget.crop;
    widget.onChanged((
      x: _clamp(crop.x + delta.dx / size.width, 0, 1 - crop.width),
      y: _clamp(crop.y + delta.dy / size.height, 0, 1 - crop.height),
      width: crop.width,
      height: crop.height,
    ));
  }

  void _resize(
    _StickerCropGesture corner,
    Offset delta,
    Size size,
  ) {
    final crop = widget.crop;
    final dx = delta.dx / size.width;
    final dy = delta.dy / size.height;
    var x = crop.x;
    var y = crop.y;
    var width = crop.width;
    var height = crop.height;
    if (corner == _StickerCropGesture.northwest ||
        corner == _StickerCropGesture.southwest) {
      final right = x + width;
      x = _clamp(x + dx, 0, right - _minimumSize);
      width = right - x;
    } else {
      width = _clamp(width + dx, _minimumSize, 1 - x);
    }
    if (corner == _StickerCropGesture.northwest ||
        corner == _StickerCropGesture.northeast) {
      final bottom = y + height;
      y = _clamp(y + dy, 0, bottom - _minimumSize);
      height = bottom - y;
    } else {
      height = _clamp(height + dy, _minimumSize, 1 - y);
    }
    widget.onChanged((x: x, y: y, width: width, height: height));
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
        builder: (context, constraints) {
          final availableWidth =
              constraints.maxWidth.isFinite ? constraints.maxWidth : 320.0;
          final maxWidth = math.min(320.0, availableWidth);
          final height = math.min(320.0, maxWidth / _aspectRatio);
          final size = Size(height * _aspectRatio, height);
          final crop = widget.crop;
          return Center(
            child: ClipRRect(
              borderRadius: BorderRadius.circular(14),
              child: SizedBox(
                key: const ValueKey('sticker-crop-preview'),
                width: size.width,
                height: size.height,
                child: GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onHorizontalDragUpdate: (_) {},
                  onVerticalDragUpdate: (_) {},
                  child: Listener(
                    onPointerMove: (event) => _updateGesture(event, size),
                    onPointerUp: _endGesture,
                    onPointerCancel: _endGesture,
                    child: Stack(
                      children: [
                        Positioned.fill(
                          child: ColoredBox(
                            color: KaedeColors.rail,
                            child: Image(image: _provider, fit: BoxFit.fill),
                          ),
                        ),
                        _cropShade(
                          imageSize: size,
                          left: 0,
                          top: 0,
                          right: 0,
                          height: crop.y,
                        ),
                        _cropShade(
                          imageSize: size,
                          left: 0,
                          top: crop.y + crop.height,
                          right: 0,
                          bottom: 0,
                        ),
                        _cropShade(
                          imageSize: size,
                          left: 0,
                          top: crop.y,
                          width: crop.x,
                          height: crop.height,
                        ),
                        _cropShade(
                          imageSize: size,
                          left: crop.x + crop.width,
                          top: crop.y,
                          right: 0,
                          height: crop.height,
                        ),
                        Positioned(
                          left: crop.x * size.width,
                          top: crop.y * size.height,
                          width: crop.width * size.width,
                          height: crop.height * size.height,
                          child: Semantics(
                            label: 'Crop selection. Drag to move.',
                            child: Stack(
                              clipBehavior: Clip.none,
                              children: [
                                Positioned.fill(
                                  child: Listener(
                                    key: const ValueKey(
                                        'sticker-crop-selection'),
                                    behavior: HitTestBehavior.translucent,
                                    onPointerDown: (event) => _beginGesture(
                                      event,
                                      _StickerCropGesture.move,
                                    ),
                                    child: DecoratedBox(
                                      decoration: BoxDecoration(
                                        border: Border.all(
                                          color: Colors.white,
                                          width: 2,
                                        ),
                                        boxShadow: const [
                                          BoxShadow(
                                            color: Colors.black54,
                                            blurRadius: 2,
                                          ),
                                        ],
                                      ),
                                      child: const _CropGrid(),
                                    ),
                                  ),
                                ),
                                _cropHandle(
                                  _StickerCropGesture.northwest,
                                  size,
                                ),
                                _cropHandle(
                                  _StickerCropGesture.northeast,
                                  size,
                                ),
                                _cropHandle(
                                  _StickerCropGesture.southwest,
                                  size,
                                ),
                                _cropHandle(
                                  _StickerCropGesture.southeast,
                                  size,
                                ),
                              ],
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          );
        },
      );

  Widget _cropShade({
    required Size imageSize,
    required double left,
    required double top,
    double? right,
    double? bottom,
    double? width,
    double? height,
  }) =>
      Positioned(
        left: left == 0 ? 0 : left * imageSize.width,
        top: top == 0 ? 0 : top * imageSize.height,
        right: right,
        bottom: bottom,
        width: width == null ? null : width * imageSize.width,
        height: height == null ? null : height * imageSize.height,
        child: const IgnorePointer(
          child: ColoredBox(color: Color(0x99000000)),
        ),
      );

  Widget _cropHandle(
    _StickerCropGesture corner,
    Size imageSize,
  ) {
    final north = corner == _StickerCropGesture.northwest ||
        corner == _StickerCropGesture.northeast;
    final west = corner == _StickerCropGesture.northwest ||
        corner == _StickerCropGesture.southwest;
    return Positioned(
      top: north ? 0 : null,
      bottom: north ? null : 0,
      left: west ? 0 : null,
      right: west ? null : 0,
      width: 40,
      height: 40,
      child: Semantics(
        button: true,
        label: 'Resize crop from ${corner.name} corner',
        child: Listener(
          key: ValueKey('sticker-crop-handle-${corner.name}'),
          behavior: HitTestBehavior.opaque,
          onPointerDown: (event) => _beginGesture(event, corner),
          child: Center(
            child: Container(
              width: 15,
              height: 15,
              decoration: BoxDecoration(
                color: KaedeColors.coral,
                border: Border.all(color: Colors.white, width: 2),
                borderRadius: BorderRadius.circular(3),
                boxShadow: const [
                  BoxShadow(color: Colors.black54, blurRadius: 3),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

final class _CropGrid extends StatelessWidget {
  const _CropGrid();

  @override
  Widget build(BuildContext context) => Stack(
        children: [
          for (final alignment in const [-1 / 3, 1 / 3]) ...[
            Align(
              alignment: Alignment(alignment, 0),
              child: const VerticalDivider(
                width: 1,
                thickness: 1,
                color: Color(0x66FFFFFF),
              ),
            ),
            Align(
              alignment: Alignment(0, alignment),
              child: const Divider(
                height: 1,
                thickness: 1,
                color: Color(0x66FFFFFF),
              ),
            ),
          ],
        ],
      );
}

final class _BotIntegrationsTab extends StatefulWidget {
  const _BotIntegrationsTab({required this.guild, required this.repository});

  final KaedeGuild guild;
  final KaedeRepository repository;

  @override
  State<_BotIntegrationsTab> createState() => _BotIntegrationsTabState();
}

final class _BotIntegrationsTabState extends State<_BotIntegrationsTab> {
  List<Map<String, Object?>> _items = const [];
  var _loading = true;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant _BotIntegrationsTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final items = await widget.repository.botIntegrations(widget.guild.ref);
      if (mounted) {
        setState(() {
          _items = items;
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      setState(() => _loading = false);
      _tabError(context, 'Could not load bot integrations', error);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: kSettingsSurface,
        body: _loading
            ? const Center(child: CircularProgressIndicator())
            : RefreshIndicator(
                onRefresh: _load,
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(14, 12, 14, 24),
                  children: [
                    const _TabHint(
                      'Bots keep only the scopes, live-event intents and guild permissions approved during installation. Removing one immediately revokes future access.',
                    ),
                    if (_items.isEmpty)
                      const _TabEmpty(
                        icon: Icons.smart_toy_outlined,
                        title: 'No bots installed',
                        body:
                            'Open a bot invite link to review and install an automation.',
                      ),
                    for (final item in _items) _integrationCard(item),
                  ],
                ),
              ),
      );

  Widget _integrationCard(Map<String, Object?> item) {
    final application = item['application'] is Map
        ? Map<String, Object?>.from(item['application']! as Map)
        : const <String, Object?>{};
    final name = '${application['name'] ?? 'Bot'}';
    final bot = application['bot_user'] is Map
        ? Map<String, Object?>.from(application['bot_user']! as Map)
        : const <String, Object?>{};
    final scopes =
        (item['scopes'] as List? ?? const []).map((e) => '$e').toList();
    final intents =
        (item['intents'] as List? ?? const []).map((e) => '$e').toList();
    return Card(
      margin: const EdgeInsets.only(top: 10),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              CircleAvatar(
                backgroundColor: KaedeColors.coralSoft,
                foregroundColor: KaedeColors.coralText,
                child: Text(name.characters.first.toUpperCase()),
              ),
              const SizedBox(width: 12),
              Expanded(
                  child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(name,
                      style: const TextStyle(fontWeight: FontWeight.w800)),
                  Text('${bot['handle'] ?? application['origin_domain'] ?? ''}',
                      style: const TextStyle(
                          color: KaedeColors.muted, fontSize: 12)),
                ],
              )),
              IconButton(
                tooltip: 'Remove bot',
                color: KaedeColors.danger,
                onPressed: () => _remove(item, application, name),
                icon: const Icon(Icons.delete_outline_rounded),
              ),
            ]),
            if ('${application['description'] ?? ''}'.trim().isNotEmpty) ...[
              const SizedBox(height: 8),
              Text('${application['description']}',
                  style: const TextStyle(color: KaedeColors.textSoft)),
            ],
            const SizedBox(height: 10),
            Wrap(spacing: 6, runSpacing: 6, children: [
              _Tag('${item['status'] ?? 'unknown'}'),
              _Tag('${item['e2ee_mode'] ?? 'disabled'} E2EE'),
              _Tag('${scopes.length} scopes'),
              _Tag('${intents.length} intents'),
            ]),
            if (scopes.isNotEmpty || intents.isNotEmpty)
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                title: const Text('Approved access'),
                children: [
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      [...scopes, ...intents].join('\n'),
                      style: const TextStyle(
                          color: KaedeColors.muted, height: 1.45),
                    ),
                  ),
                ],
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _remove(Map<String, Object?> item,
      Map<String, Object?> application, String name) async {
    if (!await _confirm(
      context,
      'Remove $name?',
      'Future API and realtime access for this guild will be revoked.',
      destructive: true,
    )) {
      return;
    }
    if (!mounted) return;
    final reason =
        await _prompt(context, 'Removal audit reason', 'Reason (optional)');
    if (reason == null) return;
    try {
      final rawRef = '${application['ref'] ?? ''}';
      final applicationRef = rawRef.isNotEmpty
          ? EntityRef.parse(rawRef)
          : EntityRef(
              Snowflake('${application['id']}'),
              Domain('${application['origin_domain']}'),
            );
      await widget.repository.removeBotIntegration(
        widget.guild.ref,
        applicationRef,
        reason: reason,
      );
      if (mounted) {
        setState(() => _items = _items.where((e) => e != item).toList());
      }
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not remove the bot', error);
    }
  }
}

final class _Tag extends StatelessWidget {
  const _Tag(this.label);
  final String label;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: KaedeColors.raised,
          borderRadius: BorderRadius.circular(99),
          border: Border.all(color: KaedeColors.border),
        ),
        child: Text(label.replaceAll('_', ' '),
            style: const TextStyle(color: KaedeColors.muted, fontSize: 11)),
      );
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
      backgroundColor: kSettingsSurface,
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(14), children: [
              const _TabHint(
                  'Webhook tokens are secrets. A rotated token is shown only once.'),
              if (!widget.canManage)
                const Padding(
                  padding: EdgeInsets.only(top: 8),
                  child: Row(
                    children: [
                      Icon(Icons.lock_outline_rounded,
                          size: 19, color: KaedeColors.muted),
                      SizedBox(width: 11),
                      Text('Manage Webhooks is required',
                          style: TextStyle(
                              color: KaedeColors.muted, fontSize: 13.5)),
                    ],
                  ),
                ),
              for (final item in _items)
                _ManagementRow(
                    leading: const Icon(Icons.webhook_rounded,
                        size: 20, color: KaedeColors.muted),
                    title: '${item['name'] ?? 'Webhook'}',
                    subtitle: '${item['channel_id']}',
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
                                            title:
                                                const Text('New webhook token'),
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
                                  value: 'rotate', child: Text('Rotate token')),
                              PopupMenuItem(
                                  value: 'delete', child: Text('Delete'))
                            ])),
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
    required this.userProfiles,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canView;
  final Map<EntityRef, KaedeUser> userProfiles;
  @override
  State<_AuditTab> createState() => _AuditTabState();
}

final class _AuditTabState extends State<_AuditTab> {
  List<Map<String, Object?>> _items = const [];
  List<GuildMember> _members = const [];
  var _loading = true;
  var _loadingOlder = false;
  var _hasMore = false;
  String? _error;
  String? _actorFilter;
  String? _actionFilter;

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
      setState(() {
        _loading = true;
        _actorFilter = null;
        _actionFilter = null;
      });
      unawaited(_load());
    }
  }

  Future<void> _load({bool refresh = false}) async {
    if (!widget.canView) {
      if (mounted) setState(() => _loading = false);
      return;
    }
    if (refresh && mounted) setState(() => _error = null);
    try {
      final items = await widget.repository.auditLog(widget.guild.ref);
      var members = _members;
      try {
        members = await widget.repository.members(widget.guild.ref);
      } on Object {
        // Actor IDs and targets still render if roster resolution is denied.
      }
      if (mounted) {
        setState(() {
          _items = items;
          _members = members;
          _hasMore = items.length == 50;
          _loading = false;
          _error = null;
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(
          error,
          summary: 'Could not load the audit log',
        );
        _loading = false;
      });
    }
  }

  Future<void> _loadOlder() async {
    if (_loadingOlder || !_hasMore || _items.isEmpty) return;
    setState(() => _loadingOlder = true);
    try {
      final older = await widget.repository.auditLog(
        widget.guild.ref,
        before: '${_items.last['id']}',
      );
      if (!mounted) return;
      setState(() {
        final known = _items.map((item) => '${item['id']}').toSet();
        _items = [
          ..._items,
          ...older.where((item) => !known.contains('${item['id']}')),
        ];
        _hasMore = older.length == 50;
        _loadingOlder = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      _tabError(context, 'Could not load older audit events', error);
      setState(() => _loadingOlder = false);
    }
  }

  Map<EntityRef, KaedeUser> get _users => <EntityRef, KaedeUser>{
        ...widget.userProfiles,
        for (final member in _members) member.user.ref: member.user,
      };

  List<Map<String, Object?>> get _visibleItems => _items.where((item) {
        final actorMatches = _actorFilter == null ||
            guildAuditActorKey(item, widget.guild.ref.domain) == _actorFilter;
        final actionMatches = _actionFilter == null ||
            guildAuditActionFilterKey(item) == _actionFilter;
        return actorMatches && actionMatches;
      }).toList();

  void _clearFilters() => setState(() {
        _actorFilter = null;
        _actionFilter = null;
      });

  @override
  Widget build(BuildContext context) => !widget.canView
      ? const ColoredBox(
          color: kSettingsSurface,
          child: Center(
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.lock_outline_rounded,
                    size: 19, color: KaedeColors.muted),
                SizedBox(width: 11),
                Text('View Audit Log is required',
                    style: TextStyle(color: KaedeColors.muted, fontSize: 13.5)),
              ],
            ),
          ),
        )
      : _loading
          ? const Center(child: CircularProgressIndicator())
          : ColoredBox(
              color: kSettingsSurface,
              child: _error != null && _items.isEmpty
                  ? _AuditErrorState(message: _error!, retry: _load)
                  : RefreshIndicator(
                      onRefresh: () => _load(refresh: true),
                      child: _buildList(),
                    ),
            );

  Widget _buildList() {
    final users = _users;
    final visible = _visibleItems;
    final actorKeys = _items
        .map((item) => guildAuditActorKey(item, widget.guild.ref.domain))
        .whereType<String>()
        .toSet()
        .toList()
      ..sort((left, right) => guildAuditActorNameFromKey(left, users)
          .compareTo(guildAuditActorNameFromKey(right, users)));
    final actions = <String, String>{
      for (final item in _items)
        guildAuditActionFilterKey(item): guildAuditActionLabel(item),
    };
    final actionKeys = actions.keys.toList()
      ..sort((left, right) => actions[left]!.compareTo(actions[right]!));

    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 32),
      children: [
        const SettingsSectionHeader(
          'Audit log',
          top: 0,
          subheading:
              'Review moderation and configuration changes made in this guild.',
        ),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              _AuditFilterButton<String>(
                icon: Icons.person_outline_rounded,
                label: _actorFilter == null
                    ? 'All members'
                    : guildAuditActorNameFromKey(_actorFilter!, users),
                value: _actorFilter,
                allLabel: 'All members',
                values: actorKeys,
                itemLabel: (value) => guildAuditActorNameFromKey(value, users),
                changed: (value) => setState(() => _actorFilter = value),
              ),
              const SizedBox(width: 8),
              _AuditFilterButton<String>(
                icon: Icons.tune_rounded,
                label: _actionFilter == null
                    ? 'All actions'
                    : actions[_actionFilter] ?? 'All actions',
                value: _actionFilter,
                allLabel: 'All actions',
                values: actionKeys,
                itemLabel: (value) => actions[value]!,
                changed: (value) => setState(() => _actionFilter = value),
              ),
              if (_actorFilter != null || _actionFilter != null) ...[
                const SizedBox(width: 4),
                TextButton(
                  onPressed: _clearFilters,
                  child: const Text('Clear'),
                ),
              ],
            ],
          ),
        ),
        const SizedBox(height: 14),
        if (_items.isEmpty)
          const _AuditEmptyState(
            title: 'No audit events yet',
            message: 'Administrative actions will show up here.',
          )
        else if (visible.isEmpty)
          _AuditEmptyState(
            title: 'No matching events',
            message: 'Try another member or action filter.',
            action: TextButton(
              onPressed: _clearFilters,
              child: const Text('Clear filters'),
            ),
          )
        else
          for (final item in visible)
            _AuditEventCard(
              item: item,
              guild: widget.guild,
              users: users,
            ),
        if (_hasMore && visible.isNotEmpty) ...[
          const SizedBox(height: 8),
          Center(
            child: OutlinedButton.icon(
              onPressed: _loadingOlder ? null : _loadOlder,
              icon: _loadingOlder
                  ? const SizedBox.square(
                      dimension: 15,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.expand_more_rounded),
              label: Text(_loadingOlder ? 'Loading…' : 'Load older events'),
            ),
          ),
        ],
      ],
    );
  }
}

final class _AuditFilterButton<T> extends StatelessWidget {
  const _AuditFilterButton({
    required this.icon,
    required this.label,
    required this.value,
    required this.allLabel,
    required this.values,
    required this.itemLabel,
    required this.changed,
  });

  final IconData icon;
  final String label;
  final T? value;
  final String allLabel;
  final List<T> values;
  final String Function(T value) itemLabel;
  final ValueChanged<T?> changed;

  @override
  Widget build(BuildContext context) => PopupMenuButton<T>(
        initialValue: value,
        onSelected: (value) => changed(value),
        itemBuilder: (context) => [
          PopupMenuItem<T>(onTap: () => changed(null), child: Text(allLabel)),
          for (final item in values)
            PopupMenuItem<T>(value: item, child: Text(itemLabel(item))),
        ],
        child: Container(
          constraints: const BoxConstraints(maxWidth: 210),
          padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
          decoration: BoxDecoration(
            color: KaedeColors.raised,
            borderRadius: BorderRadius.circular(KaedeRadius.small),
            border: Border.all(color: KaedeColors.border),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 16, color: KaedeColors.textSoft),
              const SizedBox(width: 7),
              Flexible(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: KaedeColors.textSoft,
                    fontSize: 12.5,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              const SizedBox(width: 4),
              const Icon(Icons.arrow_drop_down_rounded,
                  size: 18, color: KaedeColors.muted),
            ],
          ),
        ),
      );
}

final class _AuditEventCard extends StatelessWidget {
  const _AuditEventCard({
    required this.item,
    required this.guild,
    required this.users,
  });

  final Map<String, Object?> item;
  final KaedeGuild guild;
  final Map<EntityRef, KaedeUser> users;

  @override
  Widget build(BuildContext context) {
    final actorRef = guildAuditActorRef(item, guild.ref.domain);
    final actor = actorRef == null ? null : users[actorRef];
    final actorName =
        actor?.name ?? guildAuditActorName(item, users, guild.ref.domain);
    final target = guildAuditTargetName(item, guild, users);
    final summary =
        guildAuditSummary(item, actorName: actorName, targetName: target);
    final createdAt =
        DateTime.tryParse('${item['created_at'] ?? ''}')?.toLocal();
    final changes = guildAuditChanges(item);
    final reason = '${item['reason'] ?? ''}'.trim();
    final metadata = <String>[
      if (createdAt != null) guildAuditRelativeTime(createdAt),
      guildAuditActionLabel(item),
    ].join(' • ');

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
        color: KaedeColors.panel,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        clipBehavior: Clip.antiAlias,
        child: ExpansionTile(
          tilePadding: const EdgeInsets.fromLTRB(12, 8, 8, 8),
          childrenPadding: const EdgeInsets.fromLTRB(14, 0, 14, 14),
          shape: const Border(),
          collapsedShape: const Border(),
          leading: actor == null
              ? CircleAvatar(
                  radius: 18,
                  backgroundColor: guildAuditActionColor(item),
                  child: Icon(guildAuditActionIcon(item),
                      size: 18, color: KaedeColors.text),
                )
              : UserAvatar(user: actor, radius: 18),
          title: Text(
            summary,
            style: const TextStyle(
              fontSize: 14,
              height: 1.3,
              fontWeight: FontWeight.w600,
            ),
          ),
          subtitle: Padding(
            padding: const EdgeInsets.only(top: 3),
            child: Text(
              metadata,
              style: const TextStyle(
                color: KaedeColors.muted,
                fontSize: 11.5,
              ),
            ),
          ),
          children: [
            const Divider(height: 1),
            const SizedBox(height: 12),
            if (reason.isNotEmpty) ...[
              _AuditDetailLabel(label: 'Reason', value: reason),
              const SizedBox(height: 10),
            ],
            _AuditDetailLabel(
              label: 'Target',
              value: guildAuditTargetDetail(item, guild, users),
            ),
            if (createdAt != null) ...[
              const SizedBox(height: 10),
              _AuditDetailLabel(
                label: 'When',
                value: DateFormat('MMM d, y • h:mm:ss a').format(createdAt),
              ),
            ],
            if (reason.isEmpty) ...[
              const SizedBox(height: 10),
              const _AuditDetailLabel(
                  label: 'Reason', value: 'No reason provided'),
            ],
            if (changes.isNotEmpty) ...[
              const SizedBox(height: 14),
              const Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  'CHANGES',
                  style: TextStyle(
                    color: KaedeColors.muted,
                    fontSize: 10.5,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1,
                  ),
                ),
              ),
              const SizedBox(height: 7),
              for (final change in changes) _AuditChangeRow(change: change),
            ],
          ],
        ),
      ),
    );
  }
}

final class _AuditDetailLabel extends StatelessWidget {
  const _AuditDetailLabel({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Align(
        alignment: Alignment.centerLeft,
        child: Text.rich(
          TextSpan(
            style: const TextStyle(
              color: KaedeColors.textSoft,
              fontSize: 12.5,
              height: 1.4,
            ),
            children: [
              TextSpan(
                text: '$label  ',
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontWeight: FontWeight.w600,
                ),
              ),
              TextSpan(text: value),
            ],
          ),
        ),
      );
}

final class _AuditChangeRow extends StatelessWidget {
  const _AuditChangeRow({required this.change});

  final Map<String, Object?> change;

  @override
  Widget build(BuildContext context) {
    final key = guildAuditFieldLabel('${change['key'] ?? 'value'}');
    final value = guildAuditChangeDescription(change);
    return Padding(
      padding: const EdgeInsets.only(bottom: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Padding(
            padding: EdgeInsets.only(top: 5),
            child: Icon(Icons.circle, size: 5, color: KaedeColors.coralText),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text.rich(
              TextSpan(
                style: const TextStyle(
                  color: KaedeColors.textSoft,
                  fontSize: 12,
                  height: 1.4,
                ),
                children: [
                  TextSpan(
                    text: '$key: ',
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                  TextSpan(text: value),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

final class _AuditEmptyState extends StatelessWidget {
  const _AuditEmptyState({
    required this.title,
    required this.message,
    this.action,
  });

  final String title;
  final String message;
  final Widget? action;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 52, horizontal: 20),
        child: Column(
          children: [
            const Icon(Icons.manage_search_rounded,
                size: 42, color: KaedeColors.muted),
            const SizedBox(height: 12),
            Text(title,
                style:
                    const TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
            const SizedBox(height: 5),
            Text(message,
                textAlign: TextAlign.center,
                style: const TextStyle(color: KaedeColors.muted, fontSize: 13)),
            if (action case final button?) ...[
              const SizedBox(height: 8),
              button,
            ],
          ],
        ),
      );
}

final class _AuditErrorState extends StatelessWidget {
  const _AuditErrorState({required this.message, required this.retry});

  final String message;
  final Future<void> Function() retry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.error_outline_rounded,
                  size: 38, color: KaedeColors.danger),
              const SizedBox(height: 10),
              Text(message, textAlign: TextAlign.center),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                onPressed: retry,
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Try again'),
              ),
            ],
          ),
        ),
      );
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
  final code = guildAuditActionCode(item);
  final targetType = '${item['target_type'] ?? ''}';
  return switch (code) {
    1 => 'Guild updated',
    10 => 'Channel created',
    11 => targetType == 'channel_order'
        ? 'Channel order updated'
        : 'Channel updated',
    12 => 'Channel deleted',
    15 => 'Channel permissions updated',
    16 => 'Channel permissions removed',
    17 => 'Channel permissions synced',
    20 => 'Member kicked',
    22 => 'Member banned',
    23 => 'Member unbanned',
    24 => 'Member updated',
    25 => targetType == 'instance' ? 'Instance banned' : 'Member roles updated',
    26 => targetType == 'instance' ? 'Instance unbanned' : 'Member moved',
    27 =>
      targetType == 'user' ? 'Ownership transferred' : 'Member disconnected',
    30 => 'Role created',
    31 => 'Role updated',
    32 => 'Role deleted',
    33 => 'Roles reordered',
    40 => 'Invite created',
    42 => 'Invite deleted',
    50 => 'Webhook created',
    51 => 'Webhook updated',
    52 => 'Webhook deleted',
    60 => 'Emoji created',
    61 => 'Emoji updated',
    62 => 'Emoji deleted',
    final value? => 'Unknown action ($value)',
    null => _humanizeAuditAction('${item['action_type'] ?? ''}'),
  };
}

int? guildAuditActionCode(Map<String, Object?> item) {
  final value = item['action_type'];
  return value is num ? value.toInt() : int.tryParse('$value');
}

String guildAuditActionFilterKey(Map<String, Object?> item) =>
    '${item['action_type'] ?? 'unknown'}|${item['target_type'] ?? ''}';

String _humanizeAuditAction(String value) {
  final normalized = value.trim();
  if (normalized.isEmpty) return 'Guild action';
  const known = <String, String>{
    'guild.update': 'Guild updated',
    'guild.channel.create': 'Channel created',
    'guild.channel.update': 'Channel updated',
    'guild.channel.delete': 'Channel deleted',
    'guild.role.create': 'Role created',
    'guild.role.update': 'Role updated',
    'guild.role.delete': 'Role deleted',
    'guild.member.update': 'Member updated',
    'guild.member.kick': 'Member kicked',
    'guild.member.ban': 'Member banned',
    'guild.member.unban': 'Member unbanned',
    'guild.invite.create': 'Invite created',
    'guild.invite.delete': 'Invite deleted',
  };
  if (known[normalized] case final label?) return label;
  final words = normalized
      .split(RegExp(r'[._\-\s]+'))
      .where((part) => part.isNotEmpty)
      .toList();
  if (words.isEmpty) return 'Guild action';
  final useful = words.first == 'guild' ? words.skip(1).toList() : words;
  final label = useful.join(' ');
  return '${label[0].toUpperCase()}${label.substring(1)}';
}

EntityRef? guildAuditActorRef(
  Map<String, Object?> item,
  Domain defaultDomain,
) {
  final id = '${item['actor_id'] ?? ''}'.trim();
  final domain = '${item['actor_domain'] ?? defaultDomain.value}'.trim();
  if (id.isEmpty || domain.isEmpty) return null;
  try {
    return EntityRef(Snowflake(id), Domain(domain));
  } on FormatException {
    return null;
  }
}

String? guildAuditActorKey(
  Map<String, Object?> item,
  Domain defaultDomain,
) =>
    guildAuditActorRef(item, defaultDomain)?.wire;

String guildAuditActorName(
  Map<String, Object?> item,
  Map<EntityRef, KaedeUser> users,
  Domain defaultDomain,
) {
  final ref = guildAuditActorRef(item, defaultDomain);
  if (ref == null) return 'Unknown moderator';
  return users[ref]?.name ?? '@${ref.id.value}';
}

String guildAuditActorNameFromKey(
  String key,
  Map<EntityRef, KaedeUser> users,
) {
  try {
    final ref = EntityRef.parse(key);
    return users[ref]?.name ?? '@${ref.id.value}';
  } on FormatException {
    return 'Unknown moderator';
  }
}

Map<String, Object?> _guildAuditTarget(Map<String, Object?> item) {
  final value = item['target_ref'];
  if (value is! Map) return const <String, Object?>{};
  return value.map((key, value) => MapEntry('$key', value));
}

EntityRef? _guildAuditTargetRef(
  Map<String, Object?> item,
  Domain defaultDomain,
) {
  final target = _guildAuditTarget(item);
  final id = '${target['id'] ?? ''}'.trim();
  final domain =
      '${target['origin_domain'] ?? target['domain'] ?? defaultDomain.value}'
          .trim();
  if (id.isEmpty || domain.isEmpty) return null;
  try {
    return EntityRef(Snowflake(id), Domain(domain));
  } on FormatException {
    return null;
  }
}

String guildAuditTargetName(
  Map<String, Object?> item,
  KaedeGuild guild,
  Map<EntityRef, KaedeUser> users,
) {
  final type = '${item['target_type'] ?? ''}';
  final target = _guildAuditTarget(item);
  final ref = _guildAuditTargetRef(item, guild.ref.domain);
  if (type == 'guild') return guild.name;
  if (type == 'channel_order') return 'the channel list';
  if (type == 'channel') {
    final channel =
        guild.channels.where((value) => value.ref == ref).firstOrNull;
    final name = channel?.name ?? '${target['name'] ?? ''}'.trim();
    return name.isEmpty ? 'a channel' : '#$name';
  }
  if (type == 'role') {
    final role = guild.roles.where((value) => value.ref == ref).firstOrNull;
    final name = role?.name ?? '${target['name'] ?? ''}'.trim();
    if (name.isNotEmpty) return '@$name';
    final ids = target['ids'];
    if (ids is List) return '${ids.length} roles';
    return 'a role';
  }
  if (type == 'member' || type == 'user') {
    final user = ref == null ? null : users[ref];
    return user?.name ?? (ref == null ? 'a member' : '@${ref.id.value}');
  }
  if (type == 'instance') {
    final domain = '${target['domain'] ?? ''}'.trim();
    return domain.isEmpty ? 'an instance' : domain;
  }
  if (type == 'invite') {
    final code = '${target['code'] ?? ''}'.trim();
    return code.isEmpty ? 'an invite' : 'invite $code';
  }
  return type.isEmpty ? 'the guild' : 'a $type';
}

String guildAuditTargetDetail(
  Map<String, Object?> item,
  KaedeGuild guild,
  Map<EntityRef, KaedeUser> users,
) {
  final name = guildAuditTargetName(item, guild, users);
  final ref = _guildAuditTargetRef(item, guild.ref.domain);
  if (ref == null) return name;
  final user = users[ref];
  if (user != null) return '$name • ${user.handle}';
  return '$name • ${ref.wire}';
}

String guildAuditSummary(
  Map<String, Object?> item, {
  required String actorName,
  required String targetName,
}) {
  final code = guildAuditActionCode(item);
  final targetType = '${item['target_type'] ?? ''}';
  final verb = switch (code) {
    1 => 'updated',
    10 => 'created',
    11 when targetType == 'channel_order' => 'reordered',
    11 => 'updated',
    12 => 'deleted',
    15 => 'updated permissions for',
    16 => 'removed a permission override from',
    17 => 'synced permissions for',
    20 => 'kicked',
    22 => 'banned',
    23 => 'unbanned',
    24 => 'updated',
    25 when targetType == 'instance' => 'banned',
    25 => 'updated roles for',
    26 when targetType == 'instance' => 'unbanned',
    26 => 'moved',
    27 when targetType == 'user' => 'transferred ownership to',
    27 => 'disconnected',
    30 => 'created',
    31 => 'updated',
    32 => 'deleted',
    33 => 'reordered',
    40 => 'created',
    42 => 'deleted',
    50 => 'created',
    51 => 'updated',
    52 => 'deleted',
    60 => 'created',
    61 => 'updated',
    62 => 'deleted',
    _ => 'performed an action on',
  };
  return '$actorName $verb $targetName';
}

IconData guildAuditActionIcon(Map<String, Object?> item) {
  final code = guildAuditActionCode(item);
  final targetType = '${item['target_type'] ?? ''}';
  if (targetType == 'instance') return Icons.public_off_outlined;
  if (code == 1) return Icons.settings_outlined;
  if (code != null && code >= 10 && code < 20) return Icons.tag_rounded;
  if (code != null && code >= 20 && code < 30) {
    return code == 22 || code == 20
        ? Icons.person_remove_outlined
        : Icons.manage_accounts_outlined;
  }
  if (code != null && code >= 30 && code < 40) return Icons.badge_outlined;
  if (code != null && code >= 40 && code < 50) {
    return Icons.person_add_alt_1_rounded;
  }
  if (code != null && code >= 50 && code < 60) return Icons.webhook_rounded;
  if (code != null && code >= 60 && code < 70) {
    return Icons.emoji_emotions_outlined;
  }
  return Icons.receipt_long_outlined;
}

Color guildAuditActionColor(Map<String, Object?> item) {
  final code = guildAuditActionCode(item);
  if (code == 12 || code == 20 || code == 22 || code == 32 || code == 52) {
    return KaedeColors.dangerSoft;
  }
  if (code == 10 || code == 23 || code == 30 || code == 40) {
    return KaedeColors.mintSoft;
  }
  return KaedeColors.coralSoft;
}

String guildAuditRelativeTime(DateTime value, {DateTime? now}) {
  final current = now ?? DateTime.now();
  final difference = current.difference(value);
  if (difference.isNegative || difference.inSeconds < 45) return 'Just now';
  if (difference.inMinutes < 60) {
    final count = difference.inMinutes;
    return '$count minute${count == 1 ? '' : 's'} ago';
  }
  if (difference.inHours < 24) {
    final count = difference.inHours;
    return '$count hour${count == 1 ? '' : 's'} ago';
  }
  if (difference.inDays < 7) {
    final count = difference.inDays;
    return '$count day${count == 1 ? '' : 's'} ago';
  }
  return DateFormat('MMM d, y').format(value);
}

List<Map<String, Object?>> guildAuditChanges(Map<String, Object?> item) {
  final values = item['changes'];
  if (values is! List) return const [];
  return values
      .whereType<Map<Object?, Object?>>()
      .map((value) => value.map((key, value) => MapEntry('$key', value)))
      .toList();
}

String guildAuditFieldLabel(String value) {
  final words = value
      .trim()
      .split(RegExp(r'[_\-\s]+'))
      .where((word) => word.isNotEmpty)
      .toList();
  if (words.isEmpty) return 'Value';
  final label = words.join(' ');
  return '${label[0].toUpperCase()}${label.substring(1)}';
}

String guildAuditChangeDescription(Map<String, Object?> change) {
  String display(Object? value) {
    if (value == null || '$value' == 'null') return 'None';
    if (value is List) return value.isEmpty ? 'None' : value.join(', ');
    if (value is Map) {
      final id = value['id'];
      final domain = value['origin_domain'];
      if (id != null && domain != null) return '$id@$domain';
    }
    return '$value';
  }

  if (change.containsKey('added') || change.containsKey('removed')) {
    final parts = <String>[];
    if (change['added'] != null) parts.add('Added ${display(change['added'])}');
    if (change['removed'] != null) {
      parts.add('Removed ${display(change['removed'])}');
    }
    return parts.join(' • ');
  }
  return '${display(change['old_value'])} → ${display(change['new_value'])}';
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
  bool e2eeActivationEnabled = false,
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
        e2eeActivationEnabled: e2eeActivationEnabled,
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
    required this.e2eeActivationEnabled,
  });

  final KaedeChannel? channel;
  final List<KaedeChannel> channels;
  final EntityRef? initialParent;
  final bool e2eeActivationEnabled;

  @override
  State<_ChannelEditorSheet> createState() => _ChannelEditorSheetState();
}

final class _ChannelEditorSheetState extends State<_ChannelEditorSheet> {
  final _formKey = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.channel?.name ?? '');
  late final _topic = TextEditingController(text: widget.channel?.topic ?? '');
  late final _defaultReaction = TextEditingController(
    text: '${widget.channel?.defaultReactionEmoji?['emoji_name'] ?? ''}',
  );
  late final _trackerPrefix = TextEditingController(text: 'TASK');
  late final String? _defaultReactionId =
      widget.channel?.defaultReactionEmoji?['emoji_id'] as String?;
  var _defaultReactionEdited = false;
  late ChannelType _type = widget.channel?.type ?? ChannelType.text;
  late int _slow = widget.channel?.slowModeSeconds ?? 0;
  late int _threadSlow = widget.channel?.defaultThreadRateLimitPerUser ?? 0;
  late int _defaultAutoArchive =
      widget.channel?.defaultAutoArchiveDuration ?? 1440;
  late int _forumSort = widget.channel?.defaultSortOrder ?? 0;
  late int _forumLayout = widget.channel?.defaultForumLayout ?? 0;
  late bool _requireTag = (widget.channel?.flags ?? 0) & 16 != 0;
  late bool _e2eeRequired = widget.channel?.e2eeRequired ?? false;
  late final List<ForumTag> _forumTags = [
    ...?widget.channel?.availableTags,
  ];
  late String _history = widget.channel?.federatedHistoryPolicy ?? 'inherit';
  late String _parent =
      (widget.channel?.parentRef ?? widget.initialParent)?.wire ?? '';

  static const _types = <(ChannelType, String, IconData)>[
    (ChannelType.text, 'Text', Icons.tag_rounded),
    (ChannelType.voice, 'Voice', Icons.volume_up_rounded),
    (ChannelType.announcement, 'Announcement', Icons.campaign_rounded),
    (ChannelType.forum, 'Forum', Icons.forum_outlined),
    (ChannelType.tracker, 'Task tracker', Icons.view_kanban_outlined),
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
    _defaultReaction.dispose();
    _trackerPrefix.dispose();
    super.dispose();
  }

  Future<void> _setE2eeRequired(bool value) async {
    if (!value) {
      setState(() => _e2eeRequired = false);
      return;
    }
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Require end-to-end encrypted replies?'),
        content: const Text(
          'Only new posts will use this policy. Their starter message remains '
          'plaintext, then all replies and files are end-to-end encrypted. '
          'Once this forum is saved, the requirement cannot be turned off.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('Cancel'),
          ),
          FilledButton.icon(
            onPressed: () => Navigator.pop(dialogContext, true),
            icon: const Icon(Icons.lock_rounded),
            label: const Text('Require encryption'),
          ),
        ],
      ),
    );
    if (confirmed == true && mounted) {
      setState(() => _e2eeRequired = true);
    }
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
                        decoration: InputDecoration(
                          labelText: 'Channel name',
                          hintText: _type == ChannelType.category
                              ? 'New category'
                              : 'new-channel',
                          prefixIcon: Icon(switch (_type) {
                            ChannelType.category => Icons.folder_outlined,
                            ChannelType.voice => Icons.volume_up_rounded,
                            ChannelType.announcement => Icons.campaign_rounded,
                            ChannelType.forum => Icons.forum_outlined,
                            ChannelType.tracker => Icons.view_kanban_outlined,
                            _ => Icons.tag_rounded,
                          }),
                        ),
                        validator: (value) => value?.trim().isEmpty == true
                            ? 'Enter a channel name'
                            : null,
                      ),
                      if (widget.channel == null) ...[
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
                                enabled: true,
                                onTap: () => setState(() {
                                  _type = option.$1;
                                  if (_type == ChannelType.category) {
                                    _parent = '';
                                  }
                                }),
                              ),
                          ],
                        ),
                      ] else ...[
                        const SizedBox(height: 4),
                        Row(
                          children: [
                            Icon(
                              switch (_type) {
                                ChannelType.category => Icons.folder_outlined,
                                ChannelType.voice => Icons.volume_up_rounded,
                                ChannelType.announcement =>
                                  Icons.campaign_rounded,
                                ChannelType.forum => Icons.forum_outlined,
                                ChannelType.tracker =>
                                  Icons.view_kanban_outlined,
                                _ => Icons.tag_rounded,
                              },
                              size: 15,
                              color: KaedeColors.muted,
                            ),
                            const SizedBox(width: 7),
                            Expanded(
                              child: Text(
                                '${switch (_type) {
                                  ChannelType.category => 'Category',
                                  ChannelType.voice => 'Voice channel',
                                  ChannelType.announcement =>
                                    'Announcement channel',
                                  ChannelType.forum => 'Forum channel',
                                  ChannelType.tracker => 'Task tracker',
                                  _ => 'Text channel',
                                }} · the type cannot change after creation',
                                style: const TextStyle(
                                  color: KaedeColors.muted,
                                  fontSize: 12,
                                ),
                              ),
                            ),
                          ],
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
                          decoration: InputDecoration(
                            labelText: _type == ChannelType.voice
                                ? 'Description (optional)'
                                : _type == ChannelType.forum
                                    ? 'Post Guidelines (optional)'
                                    : 'Topic (optional)',
                            alignLabelWithHint: true,
                          ),
                        ),
                        if (_type != ChannelType.voice &&
                            _type != ChannelType.tracker) ...[
                          const SizedBox(height: 4),
                          DropdownButtonFormField<int>(
                            initialValue: _slow,
                            isExpanded: true,
                            decoration: InputDecoration(
                              labelText: _type == ChannelType.forum
                                  ? 'Post slow mode'
                                  : 'Slow mode',
                              helperText: _type == ChannelType.forum
                                  ? 'How long members wait between posts.'
                                  : 'How long members wait between messages.',
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
                        if (_type == ChannelType.tracker &&
                            widget.channel == null) ...[
                          const SizedBox(height: 14),
                          TextFormField(
                            key: const ValueKey('tracker-key-prefix-field'),
                            controller: _trackerPrefix,
                            maxLength: 10,
                            textCapitalization: TextCapitalization.characters,
                            decoration: const InputDecoration(
                              labelText: 'Task key prefix',
                              helperText:
                                  'Used for task IDs, for example TASK-24.',
                              prefixIcon: Icon(Icons.tag_rounded),
                            ),
                            validator: (value) => RegExp(
                              r'^[A-Za-z][A-Za-z0-9]{1,9}$',
                            ).hasMatch(value?.trim() ?? '')
                                ? null
                                : 'Use 2–10 letters or digits; start with a letter',
                          ),
                        ],
                        if (_type == ChannelType.forum) ...[
                          const SizedBox(height: 16),
                          DropdownButtonFormField<int>(
                            initialValue: _defaultAutoArchive,
                            decoration: const InputDecoration(
                              labelText: 'Hide posts after inactivity',
                              prefixIcon: Icon(Icons.archive_outlined),
                            ),
                            items: const [
                              DropdownMenuItem(
                                  value: 60, child: Text('1 hour')),
                              DropdownMenuItem(
                                  value: 1440, child: Text('24 hours')),
                              DropdownMenuItem(
                                  value: 4320, child: Text('3 days')),
                              DropdownMenuItem(
                                  value: 10080, child: Text('1 week')),
                            ],
                            onChanged: (value) => setState(() =>
                                _defaultAutoArchive =
                                    value ?? _defaultAutoArchive),
                          ),
                          const SizedBox(height: 14),
                          DropdownButtonFormField<int>(
                            initialValue: _threadSlow,
                            decoration: const InputDecoration(
                              labelText: 'Default reply slow mode',
                              helperText:
                                  'How long members wait between replies.',
                              prefixIcon: Icon(Icons.timer_outlined),
                            ),
                            items: [
                              for (final entry in slowModes.entries)
                                DropdownMenuItem(
                                  value: entry.key,
                                  child: Text(entry.value),
                                ),
                            ],
                            onChanged: (value) =>
                                setState(() => _threadSlow = value ?? 0),
                          ),
                          const SizedBox(height: 14),
                          DropdownButtonFormField<int>(
                            initialValue: _forumSort,
                            decoration: const InputDecoration(
                              labelText: 'Default sort order',
                              prefixIcon: Icon(Icons.swap_vert_rounded),
                            ),
                            items: const [
                              DropdownMenuItem(
                                  value: 0, child: Text('Recently Active')),
                              DropdownMenuItem(
                                  value: 1, child: Text('Date Posted')),
                            ],
                            onChanged: (value) =>
                                setState(() => _forumSort = value ?? 0),
                          ),
                          const SizedBox(height: 14),
                          DropdownButtonFormField<int>(
                            initialValue: _forumLayout,
                            decoration: const InputDecoration(
                              labelText: 'Default layout',
                              prefixIcon: Icon(Icons.view_agenda_outlined),
                            ),
                            items: const [
                              DropdownMenuItem(
                                  value: 0, child: Text('Not set')),
                              DropdownMenuItem(value: 1, child: Text('List')),
                              DropdownMenuItem(
                                  value: 2, child: Text('Gallery')),
                            ],
                            onChanged: (value) =>
                                setState(() => _forumLayout = value ?? 0),
                          ),
                          const SizedBox(height: 14),
                          TextFormField(
                            controller: _defaultReaction,
                            maxLength: 64,
                            onChanged: (_) =>
                                setState(() => _defaultReactionEdited = true),
                            decoration: InputDecoration(
                              labelText: 'Default reaction emoji (optional)',
                              counterText: '',
                              prefixIcon:
                                  const Icon(Icons.add_reaction_outlined),
                              helperText: !_defaultReactionEdited &&
                                      _defaultReactionId?.isNotEmpty == true
                                  ? 'A custom emoji is selected.'
                                  : null,
                              suffixIcon: !_defaultReactionEdited &&
                                      _defaultReactionId?.isNotEmpty == true
                                  ? IconButton(
                                      tooltip: 'Clear default reaction',
                                      onPressed: () => setState(() {
                                        _defaultReactionEdited = true;
                                        _defaultReaction.clear();
                                      }),
                                      icon: const Icon(Icons.close_rounded),
                                    )
                                  : null,
                            ),
                          ),
                          SwitchListTile(
                            contentPadding: EdgeInsets.zero,
                            value: _requireTag,
                            onChanged: (value) =>
                                setState(() => _requireTag = value),
                            title: const Text('Require a tag'),
                            subtitle: const Text(
                                'Members must select a tag before posting.'),
                          ),
                          if (widget.e2eeActivationEnabled || _e2eeRequired)
                            SwitchListTile(
                              contentPadding: EdgeInsets.zero,
                              value: _e2eeRequired,
                              onChanged: widget.channel?.e2eeRequired == true
                                  ? null
                                  : (value) =>
                                      unawaited(_setE2eeRequired(value)),
                              title: const Text(
                                  'Require end-to-end encrypted replies'),
                              subtitle: Text(_e2eeRequired
                                  ? 'Future posts activate encryption after their plaintext starter.'
                                  : 'New posts use plaintext replies.'),
                            ),
                          const SizedBox(height: 8),
                          Row(
                            children: [
                              Expanded(
                                child: Text('Tags (${_forumTags.length}/20)',
                                    style: Theme.of(context)
                                        .textTheme
                                        .titleMedium),
                              ),
                              TextButton.icon(
                                onPressed: _forumTags.length >= 20
                                    ? null
                                    : () => _editForumTag(),
                                icon: const Icon(Icons.add_rounded, size: 17),
                                label: const Text('Create Tag'),
                              ),
                            ],
                          ),
                          for (var index = 0;
                              index < _forumTags.length;
                              index += 1)
                            ListTile(
                              contentPadding: EdgeInsets.zero,
                              leading: CircleAvatar(
                                radius: 16,
                                backgroundColor: KaedeColors.raised,
                                child: Text(_forumTags[index].emoji ?? '#'),
                              ),
                              title: Text(_forumTags[index].name),
                              subtitle: _forumTags[index].moderated
                                  ? const Text('Moderated')
                                  : null,
                              trailing: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  IconButton(
                                    tooltip: 'Edit tag',
                                    onPressed: () => _editForumTag(index),
                                    icon: const Icon(Icons.edit_outlined),
                                  ),
                                  IconButton(
                                    tooltip: 'Delete tag',
                                    onPressed: () => setState(
                                        () => _forumTags.removeAt(index)),
                                    icon: const Icon(
                                        Icons.delete_outline_rounded),
                                  ),
                                ],
                              ),
                            ),
                        ],
                        if (_type == ChannelType.text ||
                            _type == ChannelType.announcement) ...[
                          const SizedBox(height: 14),
                          DropdownButtonFormField<String>(
                            key: const ValueKey('channel-history-policy-field'),
                            initialValue: _history,
                            isExpanded: true,
                            decoration: const InputDecoration(
                              labelText: 'Federated history',
                              helperText:
                                  'Override how remote instances retain this channel.',
                              prefixIcon: Icon(Icons.history_rounded),
                            ),
                            items: const [
                              DropdownMenuItem(
                                  value: 'inherit',
                                  child: Text('Use guild setting')),
                              DropdownMenuItem(
                                  value: 'disabled',
                                  child: Text('Recent messages only')),
                              DropdownMenuItem(
                                  value: 'full_retained',
                                  child: Text('Retain full history')),
                            ],
                            onChanged: (value) =>
                                setState(() => _history = value ?? 'inherit'),
                          ),
                        ],
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

  Future<void> _editForumTag([int? index]) async {
    final existing = index == null ? null : _forumTags[index];
    final name = TextEditingController(text: existing?.name ?? '');
    final emoji = TextEditingController(text: existing?.emojiName ?? '');
    var moderated = existing?.moderated ?? false;
    var emojiEdited = false;
    final result = await showDialog<ForumTag>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(existing == null ? 'Create Tag' : 'Edit Tag'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: name,
                autofocus: true,
                maxLength: 20,
                decoration: const InputDecoration(
                  labelText: 'Tag name',
                  counterText: '',
                ),
              ),
              const SizedBox(height: 10),
              TextField(
                controller: emoji,
                maxLength: 64,
                onChanged: (_) => setDialogState(() => emojiEdited = true),
                decoration: InputDecoration(
                  labelText: 'Emoji (optional)',
                  counterText: '',
                  helperText:
                      !emojiEdited && existing?.emojiId?.isNotEmpty == true
                          ? 'A custom emoji is selected.'
                          : null,
                  suffixIcon:
                      !emojiEdited && existing?.emojiId?.isNotEmpty == true
                          ? IconButton(
                              tooltip: 'Clear tag emoji',
                              onPressed: () => setDialogState(() {
                                emojiEdited = true;
                                emoji.clear();
                              }),
                              icon: const Icon(Icons.close_rounded),
                            )
                          : null,
                ),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                value: moderated,
                onChanged: (value) => setDialogState(() => moderated = value),
                title: const Text('Moderated'),
                subtitle: const Text(
                    'Only members who can manage threads can use this tag.'),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('Cancel'),
            ),
            ValueListenableBuilder<TextEditingValue>(
              valueListenable: name,
              builder: (_, value, __) => FilledButton(
                onPressed: value.text.trim().isEmpty
                    ? null
                    : () => Navigator.pop(
                          dialogContext,
                          ForumTag(
                            id: existing?.id ??
                                'new-${DateTime.now().microsecondsSinceEpoch}',
                            name: value.text.trim(),
                            moderated: moderated,
                            emojiName: emoji.text.trim().isEmpty
                                ? null
                                : emoji.text.trim(),
                            emojiId: emojiEdited ? null : existing?.emojiId,
                          ),
                        ),
                child: Text(existing == null ? 'Create' : 'Save'),
              ),
            ),
          ],
        ),
      ),
    );
    name.dispose();
    emoji.dispose();
    if (result == null || !mounted) return;
    setState(() {
      if (index == null) {
        _forumTags.add(result);
      } else {
        _forumTags[index] = result;
      }
    });
  }

  Map<String, Object?>? _defaultReactionPayload() {
    if (!_defaultReactionEdited && _defaultReactionId?.isNotEmpty == true) {
      return <String, Object?>{'emoji_id': _defaultReactionId};
    }
    final name = _defaultReaction.text.trim();
    return name.isEmpty ? null : <String, Object?>{'emoji_name': name};
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
        slowModeSeconds: _type == ChannelType.category ||
                _type == ChannelType.voice ||
                _type == ChannelType.tracker
            ? 0
            : _slow,
        parentRef: _type == ChannelType.category ? null : parent,
        federatedHistoryPolicy:
            _type == ChannelType.text || _type == ChannelType.announcement
                ? _history
                : null,
        flags: _type == ChannelType.forum
            ? _requireTag
                ? (widget.channel?.flags ?? 0) | 16
                : (widget.channel?.flags ?? 0) & ~16
            : null,
        availableTags:
            _type == ChannelType.forum ? List.unmodifiable(_forumTags) : null,
        defaultReactionEmoji:
            _type == ChannelType.forum ? _defaultReactionPayload() : null,
        defaultThreadRateLimitPerUser:
            _type == ChannelType.forum ? _threadSlow : null,
        defaultAutoArchiveDuration:
            _type == ChannelType.forum ? _defaultAutoArchive : null,
        defaultSortOrder: _type == ChannelType.forum ? _forumSort : null,
        defaultForumLayout: _type == ChannelType.forum ? _forumLayout : null,
        e2eeRequired: _type == ChannelType.forum ? _e2eeRequired : null,
        trackerKeyPrefix: _type == ChannelType.tracker && widget.channel == null
            ? _trackerPrefix.text.trim().toUpperCase()
            : null,
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
    this.federatedHistoryPolicy,
    this.flags,
    this.availableTags,
    this.defaultReactionEmoji,
    this.defaultThreadRateLimitPerUser,
    this.defaultAutoArchiveDuration,
    this.defaultSortOrder,
    this.defaultForumLayout,
    this.e2eeRequired,
    this.trackerKeyPrefix,
  });

  final String name;
  final String topic;
  final ChannelType type;
  final int slowModeSeconds;
  final EntityRef? parentRef;
  final String? federatedHistoryPolicy;
  final int? flags;
  final List<ForumTag>? availableTags;
  final Map<String, Object?>? defaultReactionEmoji;
  final int? defaultThreadRateLimitPerUser;
  final int? defaultAutoArchiveDuration;
  final int? defaultSortOrder;
  final int? defaultForumLayout;
  final bool? e2eeRequired;
  final String? trackerKeyPrefix;

  Map<String, Object?> get json => {
        'name': name,
        'type': _channelNumber(type),
        'topic': type == ChannelType.category || topic.isEmpty ? null : topic,
        'rate_limit_per_user':
            type == ChannelType.category || type == ChannelType.tracker
                ? 0
                : slowModeSeconds,
        'parent_id': type == ChannelType.category ? null : parentRef?.id.value,
        if (federatedHistoryPolicy != null)
          'federated_history_policy': federatedHistoryPolicy,
        if (flags != null) 'flags': flags,
        if (availableTags != null)
          'available_tags': [
            for (final tag in availableTags!)
              <String, Object?>{
                if (!tag.id.startsWith('new-')) 'id': tag.id,
                'name': tag.name,
                'moderated': tag.moderated,
                if (tag.emojiId?.isNotEmpty == true)
                  'emoji_id': tag.emojiId
                else if (tag.emojiName?.isNotEmpty == true)
                  'emoji_name': tag.emojiName,
              },
          ],
        if (type == ChannelType.forum)
          'default_reaction_emoji': _forumEmojiJson(defaultReactionEmoji),
        if (defaultThreadRateLimitPerUser != null)
          'default_thread_rate_limit_per_user': defaultThreadRateLimitPerUser,
        if (defaultAutoArchiveDuration != null)
          'default_auto_archive_duration': defaultAutoArchiveDuration,
        if (defaultSortOrder != null) 'default_sort_order': defaultSortOrder,
        if (defaultForumLayout != null)
          'default_forum_layout': defaultForumLayout,
        if (e2eeRequired != null) 'e2ee_required': e2eeRequired,
        if (trackerKeyPrefix != null) 'tracker_key_prefix': trackerKeyPrefix,
      };
}

Map<String, Object?>? _forumEmojiJson(Map<String, Object?>? emoji) {
  final id = '${emoji?['emoji_id'] ?? ''}'.trim();
  if (id.isNotEmpty) return <String, Object?>{'emoji_id': id};
  final name = '${emoji?['emoji_name'] ?? ''}'.trim();
  if (name.isNotEmpty) return <String, Object?>{'emoji_name': name};
  return null;
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
  XFile? _iconFile;
  var _removeIcon = false;
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
    // The save action is disabled until the role has a name, so the field has
    // to drive rebuilds.
    _name.addListener(_permissionsChanged);
    _permissionSearch.addListener(_permissionsChanged);
  }

  @override
  void dispose() {
    _name
      ..removeListener(_permissionsChanged)
      ..dispose();
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
                    tooltip: 'Delete role',
                    style: IconButton.styleFrom(
                      foregroundColor: KaedeColors.danger,
                    ),
                    onPressed: _confirmDelete,
                    icon: const Icon(Icons.delete_outline_rounded))
            ]),
        body: ListView(padding: const EdgeInsets.all(16), children: [
          TextField(
              controller: _name,
              decoration: const InputDecoration(labelText: 'Role name')),
          const SizedBox(height: 18),
          Text('Role colour', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 4),
          const Text(
            'Members show their highest coloured role.',
            style: TextStyle(color: KaedeColors.muted, fontSize: 12.5),
          ),
          const SizedBox(height: 12),
          Wrap(spacing: 10, runSpacing: 10, children: [
            for (final color in _colors)
              Tooltip(
                message: color == 0 ? 'No colour' : 'Custom colour',
                child: InkWell(
                  onTap: () => setState(() => _color = color),
                  borderRadius: BorderRadius.circular(KaedeRadius.medium),
                  child: Container(
                    width: 42,
                    height: 42,
                    decoration: BoxDecoration(
                      color: color == 0
                          ? KaedeColors.raised
                          : Color(0xFF000000 | color),
                      borderRadius: BorderRadius.circular(KaedeRadius.medium),
                      border: Border.all(
                        color: _color == color
                            ? KaedeColors.text
                            : KaedeColors.border,
                        width: _color == color ? 2.5 : 1,
                      ),
                    ),
                    child: color == 0
                        ? const Icon(Icons.format_color_reset_rounded,
                            size: 17, color: KaedeColors.muted)
                        : _color == color
                            ? const Icon(Icons.check_rounded,
                                size: 20, color: Colors.black87)
                            : null,
                  ),
                ),
              ),
          ]),
          const SizedBox(height: 18),
          Text('Role icon', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 4),
          const Text(
            'Shown beside names in chat. A member uses their highest role icon.',
            style: TextStyle(color: KaedeColors.muted, fontSize: 12.5),
          ),
          const SizedBox(height: 12),
          Row(children: [
            if (_iconFile case final file?)
              Image.file(File(file.path),
                  width: 44, height: 44, fit: BoxFit.contain)
            else if (!_removeIcon && widget.role?.iconHash != null)
              CachedNetworkImage(
                imageUrl: publicAssetUri(
                        widget.role!.ref.domain, widget.role!.iconHash,
                        variant: 'thumbnail_128')!
                    .toString(),
                width: 44,
                height: 44,
                fit: BoxFit.contain,
              )
            else
              const SizedBox.square(
                dimension: 44,
                child: DecoratedBox(
                  decoration: BoxDecoration(
                    color: KaedeColors.raised,
                    borderRadius: BorderRadius.all(Radius.circular(12)),
                  ),
                  child: Icon(Icons.shield_outlined, color: KaedeColors.muted),
                ),
              ),
            const SizedBox(width: 12),
            OutlinedButton.icon(
              onPressed: _pickRoleIcon,
              icon: const Icon(Icons.image_outlined),
              label: Text(_iconFile == null && widget.role?.iconHash == null
                  ? 'Choose icon'
                  : 'Change icon'),
            ),
            if (_iconFile != null ||
                (!_removeIcon && widget.role?.iconHash != null)) ...[
              const SizedBox(width: 8),
              TextButton(
                onPressed: () => setState(() {
                  _iconFile = null;
                  _removeIcon = true;
                }),
                child: const Text('Remove'),
              ),
            ],
          ]),
          const SizedBox(height: 18),
          _Panel(
            title: 'Display',
            child: Column(
              children: [
                SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: const Text('Show separately in the member list'),
                    subtitle: const Text(
                      'Members with this role get their own section.',
                    ),
                    value: _hoist,
                    onChanged: (value) => setState(() => _hoist = value)),
                SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: const Text('Allow anyone to mention this role'),
                    value: _mentionable,
                    onChanged: (value) => setState(() => _mentionable = value)),
              ],
            ),
          ),
          Text('Permissions', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 4),
          const Text(
            'Roles grant guild-wide abilities and channel defaults. Channel '
            'overrides can refine them later.',
            style: TextStyle(color: KaedeColors.muted, fontSize: 12.5),
          ),
          const SizedBox(height: 14),
          TextField(
            controller: _permissionSearch,
            decoration: InputDecoration(
              hintText: 'Search permissions',
              isDense: true,
              prefixIcon: const Icon(Icons.search_rounded, size: 19),
              suffixIcon: _permissionSearch.text.isEmpty
                  ? null
                  : IconButton(
                      tooltip: 'Clear search',
                      onPressed: _permissionSearch.clear,
                      icon: const Icon(Icons.close_rounded, size: 18),
                    ),
            ),
          ),
          const SizedBox(height: 14),
          if (_permissions & BigInt.from(Permission.administrator) !=
              BigInt.zero)
            const Card(
              color: KaedeColors.warningSoft,
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
                      padding: const EdgeInsets.fromLTRB(16, 13, 16, 2),
                      child: Text(group,
                          style: Theme.of(context).textTheme.titleMedium),
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
          const SizedBox(height: 90),
        ]),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: _name.text.trim().isEmpty
              ? null
              : () => Navigator.pop(
                    context,
                    _RoleDraft({
                      'name': _name.text.trim(),
                      'color': _color,
                      'permissions': '$_permissions',
                      'hoist': _hoist,
                      'mentionable': _mentionable
                    }, iconFile: _iconFile, removeIcon: _removeIcon),
                  ),
          icon: Icon(
              widget.role == null ? Icons.add_rounded : Icons.save_outlined),
          label: Text(widget.role == null ? 'Create role' : 'Save role'),
        ));
  }

  Future<void> _pickRoleIcon() async {
    final file = await ImagePicker().pickImage(source: ImageSource.gallery);
    if (file == null || !mounted) return;
    final contentType =
        imageUploadContentType(file.name, reportedType: file.mimeType);
    if (contentType == null) {
      _tabError(context, 'Could not choose role icon',
          'Choose a PNG, JPEG, GIF, or WebP image.');
      return;
    }
    setState(() {
      _iconFile = file;
      _removeIcon = false;
    });
  }

  Future<void> _confirmDelete() async {
    final role = widget.role;
    if (role == null) return;
    if (!await _confirm(
      context,
      'Delete ${role.name}?',
      'Members keep their other roles. Permissions this role granted are '
          'removed everywhere in the guild.',
      destructive: true,
    )) {
      return;
    }
    if (!mounted) return;
    Navigator.pop(context, _RoleDraft(const {}, delete: true));
  }
}

final class _RoleDraft {
  const _RoleDraft(this.json,
      {this.delete = false, this.iconFile, this.removeIcon = false});
  final Map<String, Object?> json;
  final bool delete;
  final XFile? iconFile;
  final bool removeIcon;
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
  Widget build(BuildContext context) => ColoredBox(
        color: kSettingsSurface,
        child: ListView(padding: const EdgeInsets.all(14), children: children),
      );
}

/// Flat settings panel: an uppercase heading over its content, no card
/// border, the way Discord groups each settings page.
final class _Panel extends StatelessWidget {
  const _Panel({required this.title, required this.child, this.subtitle});
  final String title;
  final String? subtitle;
  final Widget child;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SettingsSectionHeader(title, top: 10),
            if (subtitle != null)
              SettingsInfo(subtitle!,
                  padding: const EdgeInsets.fromLTRB(4, 0, 4, 12))
            else
              const SizedBox(height: 12),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4),
              child: child,
            ),
          ],
        ),
      );
}

final class ModerationOptions {
  const ModerationOptions({
    required this.reason,
    required this.durationSeconds,
    required this.deleteMessageSeconds,
  });

  final String reason;
  final int durationSeconds;
  final int deleteMessageSeconds;
}

Future<ModerationOptions?> showModerationOptions(
  BuildContext context, {
  required String title,
  bool timeout = false,
  bool includeDeleteHistory = false,
  TextEditingController? leadingField,
  String? leadingLabel,
}) async {
  final reason = TextEditingController();
  var duration = timeout ? 3600 : 0;
  var deleteSeconds = 0;
  try {
    return await showDialog<ModerationOptions>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(title),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (leadingField != null) ...[
                  TextField(
                    controller: leadingField,
                    autofocus: true,
                    keyboardType: TextInputType.url,
                    autocorrect: false,
                    decoration: InputDecoration(labelText: leadingLabel),
                  ),
                  const SizedBox(height: 12),
                ],
                DropdownButtonFormField<int>(
                  initialValue: duration,
                  decoration: InputDecoration(
                    labelText: timeout ? 'Timeout duration' : 'Ban duration',
                  ),
                  items: timeout
                      ? const [
                          DropdownMenuItem(
                              value: 600, child: Text('10 minutes')),
                          DropdownMenuItem(value: 3600, child: Text('1 hour')),
                          DropdownMenuItem(value: 86400, child: Text('1 day')),
                          DropdownMenuItem(
                              value: 604800, child: Text('7 days')),
                          DropdownMenuItem(
                              value: -1, child: Text('Indefinite')),
                        ]
                      : const [
                          DropdownMenuItem(
                              value: 0, child: Text('Until removed')),
                          DropdownMenuItem(value: 86400, child: Text('1 day')),
                          DropdownMenuItem(
                              value: 604800, child: Text('7 days')),
                          DropdownMenuItem(
                              value: 2592000, child: Text('30 days')),
                        ],
                  onChanged: (value) =>
                      setDialogState(() => duration = value ?? duration),
                ),
                if (includeDeleteHistory) ...[
                  const SizedBox(height: 12),
                  DropdownButtonFormField<int>(
                    initialValue: deleteSeconds,
                    decoration: const InputDecoration(
                        labelText: 'Delete message history'),
                    items: const [
                      DropdownMenuItem(value: 0, child: Text('Do not delete')),
                      DropdownMenuItem(value: 3600, child: Text('Past hour')),
                      DropdownMenuItem(value: 86400, child: Text('Past day')),
                      DropdownMenuItem(
                          value: 604800, child: Text('Past 7 days')),
                    ],
                    onChanged: (value) => setDialogState(
                        () => deleteSeconds = value ?? deleteSeconds),
                  ),
                ],
                const SizedBox(height: 12),
                TextField(
                  controller: reason,
                  maxLength: 512,
                  maxLines: 3,
                  decoration: const InputDecoration(
                    labelText: 'Audit reason (optional)',
                    alignLabelWithHint: true,
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () {
                if (leadingField != null && leadingField.text.trim().isEmpty) {
                  ScaffoldMessenger.of(dialogContext).showSnackBar(
                    SnackBar(
                        content: Text('Enter ${leadingLabel ?? 'a value'}.')),
                  );
                  return;
                }
                Navigator.pop(
                  dialogContext,
                  ModerationOptions(
                    reason: reason.text.trim(),
                    durationSeconds: duration,
                    deleteMessageSeconds: deleteSeconds,
                  ),
                );
              },
              child: Text(timeout ? 'Apply timeout' : 'Ban'),
            ),
          ],
        ),
      ),
    );
  } finally {
    reason.dispose();
  }
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
      ChannelType.announcementThread => 10,
      ChannelType.publicThread => 11,
      ChannelType.privateThread => 12,
      ChannelType.forum => 15,
      ChannelType.tracker => 17,
      ChannelType.unknown => 0
    };
