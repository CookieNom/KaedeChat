import 'dart:async';
import 'dart:io';
import 'dart:math' as math;
import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/foundation.dart' show listEquals;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/guild_admin_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/api/scheduled_events_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_admin.dart';
import 'package:kaede_mobile/src/domain/guild_hierarchy.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/permission_selection.dart';
import 'package:kaede_mobile/src/domain/scheduled_events.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/features/guild/announcement_management_tab.dart';
import 'package:kaede_mobile/src/features/guild/application_command_permissions_screen.dart';
import 'package:kaede_mobile/src/features/guild/bot_e2ee_participation_screen.dart';
import 'package:kaede_mobile/src/features/guild/guild_admin_advanced.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

BigInt get allPermissionBits => permissionMetadata.fold(
      BigInt.zero,
      (mask, permission) => mask | BigInt.from(permission.bit),
    );

BigInt effectiveChannelPermissionCeiling(
  KaedeChannel channel, {
  required bool isOwner,
}) {
  if (!isOwner && !channel.allows(Permission.administrator)) {
    return channel.permissions;
  }
  return allPermissionBits;
}

BigInt effectiveGuildPermissionCeiling(
  KaedeGuild guild, {
  required EntityRef? actorRef,
}) =>
    actorRef == guild.ownerRef || guild.allows(Permission.administrator)
        ? allPermissionBits
        : guild.permissions;

bool rolePermissionCanChange(BigInt heldPermissions, int bit) =>
    heldPermissions & BigInt.from(bit) != BigInt.zero;

bool rolePermissionChangesWithinCeiling(
  BigInt original,
  BigInt updated,
  BigInt heldPermissions,
) =>
    (original ^ updated) & ~heldPermissions == BigInt.zero;

bool canManageEffectiveChannel(
  KaedeChannel channel,
  int permission, {
  required bool isOwner,
}) =>
    effectiveChannelPermissionCeiling(channel, isOwner: isOwner) &
        BigInt.from(permission) !=
    BigInt.zero;

bool channelCategoryTargetEligible(
  KaedeChannel channel, {
  required bool isOwner,
}) =>
    canManageEffectiveChannel(
      channel,
      Permission.viewChannel,
      isOwner: isOwner,
    ) &&
    canManageEffectiveChannel(
      channel,
      Permission.manageChannels,
      isOwner: isOwner,
    );

bool channelPositionReorderAllowed(
  KaedeChannel channel,
  Iterable<KaedeChannel> channels, {
  required bool canManageGuildChannels,
  required bool isOwner,
}) {
  if (!canManageGuildChannels) return false;
  final parentRef = channel.parentRef;
  if (channel.type == ChannelType.category || parentRef == null) return true;
  final parent =
      channels.where((candidate) => candidate.ref == parentRef).firstOrNull;
  return parent != null &&
      canManageEffectiveChannel(
        parent,
        Permission.manageChannels,
        isOwner: isOwner,
      );
}

bool guildHasEffectiveChannelPermission(
  KaedeGuild guild,
  int permission, {
  required bool isOwner,
}) =>
    isOwner ||
    guild.channels.any((channel) => canManageEffectiveChannel(
          channel,
          permission,
          isOwner: false,
        ));

List<String> channelPermissionDependencyLabels(PermissionMetadata metadata) =>
    metadata.dependencies
        .map((bit) => permissionMetadata
            .where((candidate) => candidate.bit == bit)
            .map((candidate) => candidate.label)
            .firstOrNull)
        .whereType<String>()
        .toList(growable: false);

bool channelOverwritePermissionCanChange(BigInt heldPermissions, int bit) =>
    heldPermissions & BigInt.from(bit) != BigInt.zero;

bool channelOverwriteCanReset(
  BigInt allow,
  BigInt deny,
  BigInt heldPermissions,
) =>
    (allow | deny) & ~heldPermissions == BigInt.zero;

Set<EntityRef> removedGuildManagementMemberRefs(
  Iterable<GuildMember> previous,
  Iterable<GuildMember> current,
) {
  final currentRefs = current.map((member) => member.user.ref).toSet();
  return previous
      .map((member) => member.user.ref)
      .where((ref) => !currentRefs.contains(ref))
      .toSet();
}

List<GuildMember> reconcileGuildManagementMembers({
  required Iterable<GuildMember> members,
  required Iterable<GuildMember> liveMembers,
  required Set<EntityRef> removedRefs,
  String query = '',
}) {
  final normalizedQuery = query.trim().toLowerCase();
  bool matches(GuildMember member) =>
      normalizedQuery.isEmpty ||
      <String?>[
        member.nickname,
        member.user.name,
        member.user.username,
        member.user.handle,
      ].any((value) => value?.toLowerCase().contains(normalizedQuery) == true);

  final reconciled = <EntityRef, GuildMember>{};
  for (final member in members) {
    if (!removedRefs.contains(member.user.ref) && matches(member)) {
      reconciled[member.user.ref] = member;
    }
  }
  for (final member in liveMembers) {
    if (!removedRefs.contains(member.user.ref) && matches(member)) {
      reconciled[member.user.ref] = member;
    } else {
      reconciled.remove(member.user.ref);
    }
  }
  return List.unmodifiable(reconciled.values);
}

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
  var _reloadGeneration = 0;
  var _sawLiveGuild = false;

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
    final generation = ++_reloadGeneration;
    final live = ref
        .read(mobileControllerProvider)
        .guilds
        .where((guild) => guild.ref == widget.guild.ref)
        .firstOrNull;
    if (live != null) {
      _sawLiveGuild = true;
      if (mounted && generation == _reloadGeneration) {
        setState(() {
          _guild = live;
          _loading = false;
        });
      }
      return;
    }
    try {
      final guild = await _repository.guild(widget.guild.ref);
      final current = ref
          .read(mobileControllerProvider)
          .guilds
          .where((item) => item.ref == widget.guild.ref)
          .firstOrNull;
      if (mounted && generation == _reloadGeneration) {
        setState(() {
          _guild = current ?? guild;
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
    final live = mobile.guilds
        .where((guild) => guild.ref == widget.guild.ref)
        .firstOrNull;
    if (live != null) {
      _sawLiveGuild = true;
      if (!identical(live, _guild)) {
        _reloadGeneration += 1;
        _guild = live;
        _loading = false;
      }
    } else if (_sawLiveGuild) {
      _reloadGeneration += 1;
      return Scaffold(
        appBar:
            AppBar(title: Text(L10n.of(context).ui_guild_settings_33e2baed)),
        body: Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Text(
              L10n.of(context).ui_this_guild_is_no_longer_available_d15ae27d,
              textAlign: TextAlign.center,
            ),
          ),
        ),
      );
    }
    final actorRef = mobile.user?.ref;
    final isOwner = actorRef != null && actorRef == _guild.ownerRef;
    final canManageGuild = isOwner || _guild.allows(Permission.manageGuild);
    final canCreateExpressions =
        isOwner || _guild.allows(Permission.createGuildExpressions);
    final canManageExpressions =
        isOwner || _guild.allows(Permission.manageGuildExpressions);
    final canManageChannels =
        isOwner || _guild.allows(Permission.manageChannels);
    final canListGuildInvites = canManageGuild;
    final managedInviteChannels = _guild.channels
        .where((channel) =>
            channel.type != ChannelType.category &&
            (isOwner ||
                channel.permissions & BigInt.from(Permission.manageChannels) !=
                    BigInt.zero))
        .toList(growable: false);
    final canManageRoles = isOwner || _guild.allows(Permission.manageRoles);
    final hasChannelManagement = guildHasEffectiveChannelPermission(
      _guild,
      Permission.manageChannels,
      isOwner: isOwner,
    );
    final hasChannelPermissionManagement = guildHasEffectiveChannelPermission(
      _guild,
      Permission.manageRoles,
      isOwner: isOwner,
    );
    final canManageWebhooks =
        isOwner || _guild.allows(Permission.manageWebhooks);
    final managedWebhookChannels = guildWebhookManagementTargets(
      _guild.channels,
      isOwner: isOwner,
    );
    final canManageMembers = isOwner ||
        _guild.allows(Permission.kickMembers) ||
        _guild.allows(Permission.banMembers) ||
        _guild.allows(Permission.moderateMembers) ||
        _guild.allows(Permission.manageNicknames) ||
        canManageRoles;
    final sections =
        <({String label, String description, IconData icon, Widget page})>[
      (
        label: L10n.of(context).ui_overview_6a494fba,
        description: L10n.of(context)
            .ui_name_icon_banner_and_guild_wide_defaults_fc9218fd,
        icon: Icons.home_outlined,
        page: _OverviewTab(
          guild: _guild,
          repository: _repository,
          changed: _changed,
          canManage: canManageGuild,
          canManageAssets: canManageGuild,
          isOwner: isOwner,
        ),
      ),
      if (canManageChannels ||
          hasChannelManagement ||
          hasChannelPermissionManagement)
        (
          label: L10n.of(context).ui_channels_c9409495,
          description: L10n.of(context)
              .ui_create_channels_reorder_them_and_set_permissi_f7c8fc39,
          icon: Icons.tag_rounded,
          page: _ChannelsTab(
            guild: _guild,
            actorRef: actorRef,
            repository: _repository,
            changed: _changed,
            canManageChannels: canManageChannels,
            isOwner: isOwner,
            e2eeClient: () =>
                ref.read(mobileControllerProvider.notifier).e2eeClient(),
          ),
        ),
      if (canManageRoles)
        (
          label: L10n.of(context).ui_roles_3e77ce7e,
          description: L10n.of(context)
              .ui_role_colours_permissions_and_ordering_e73ab61f,
          icon: Icons.badge_outlined,
          page: _RolesTab(
              guild: _guild,
              actorRef: actorRef,
              repository: _repository,
              changed: _changed)
        ),
      if (canManageMembers)
        (
          label: L10n.of(context).ui_members_ed3c96b0,
          description: L10n.of(context)
              .ui_nicknames_roles_timeouts_kicks_and_bans_f3a1e17f,
          icon: Icons.people_outline_rounded,
          page: _MembersTab(
              guild: _guild,
              actorRef: actorRef,
              liveMembers:
                  mobile.guildMembers[_guild.ref] ?? const <GuildMember>[],
              userProfiles: mobile.userProfiles,
              repository: _repository,
              changed: _changed)
        ),
      if (isOwner ||
          _guild.allows(Permission.banMembers) ||
          _guild.allows(Permission.banInstances))
        (
          label: L10n.of(context).ui_bans_aa74d24f,
          description: L10n.of(context)
              .ui_banned_accounts_and_blocked_instances_03a7b02c,
          icon: Icons.gavel_outlined,
          page: _BansTab(
            guild: _guild,
            repository: _repository,
            canBanMembers: isOwner || _guild.allows(Permission.banMembers),
            canBanInstances: isOwner || _guild.allows(Permission.banInstances),
          )
        ),
      if (isOwner || _guild.allows(Permission.manageAutoModeration))
        (
          label: L10n.of(context).ui_automod_d65d27ea,
          description: L10n.of(context)
              .ui_filter_messages_and_member_profiles_automatic_e3c9eae5,
          icon: Icons.shield_outlined,
          page: GuildAutoModTab(
            guild: _guild,
            repository: _repository,
          ),
        ),
      if (isOwner ||
          (_guild.allows(Permission.manageGuild) &&
              (_guild.allows(Permission.kickMembers) ||
                  _guild.allows(Permission.banMembers))))
        (
          label: L10n.of(context).ui_bulk_moderation_08152917,
          description: L10n.of(context)
              .ui_prune_inactive_members_and_perform_reviewed_b_25827fe4,
          icon: Icons.cleaning_services_outlined,
          page: GuildBulkModerationTab(
            guild: _guild,
            repository: _repository,
            canPrune: isOwner ||
                (_guild.allows(Permission.manageGuild) &&
                    _guild.allows(Permission.kickMembers)),
            canBulkBan: isOwner ||
                (_guild.allows(Permission.manageGuild) &&
                    _guild.allows(Permission.banMembers)),
          ),
        ),
      if (isOwner ||
          _guild.allows(Permission.createInvite) ||
          canListGuildInvites ||
          managedInviteChannels.isNotEmpty)
        (
          label: L10n.of(context).ui_invites_4f9d0a79,
          description: L10n.of(context)
              .ui_active_invite_links_and_who_created_them_57a72920,
          icon: Icons.person_add_alt_1_rounded,
          page: _InvitesTab(
            guild: _guild,
            repository: _repository,
            actorRef: actorRef,
            canCreate: isOwner || _guild.allows(Permission.createInvite),
            canManage: canManageGuild,
            canListGuild: canListGuildInvites,
            managedChannels: managedInviteChannels,
            canManageRoles: canManageRoles,
            currentGuild: () => ref
                .read(mobileControllerProvider)
                .guilds
                .where((guild) => guild.ref == _guild.ref)
                .firstOrNull,
          )
        ),
      if (canCreateExpressions || canManageExpressions)
        (
          label: L10n.of(context).ui_emoji_033ebcdd,
          description:
              L10n.of(context).ui_custom_emoji_available_in_this_guild_e89c2155,
          icon: Icons.emoji_emotions_outlined,
          page: _EmojiTab(
            guild: _guild,
            repository: _repository,
            currentUserRef: actorRef,
            canCreate: canCreateExpressions,
            canManage: canManageExpressions,
          )
        ),
      if (canCreateExpressions || canManageExpressions)
        (
          label: L10n.of(context).ui_soundboard_5a91f77c,
          description: L10n.of(context)
              .ui_manage_and_play_short_guild_audio_clips_66e75aab,
          icon: Icons.music_note_outlined,
          page: GuildSoundboardTab(
            guild: _guild,
            repository: _repository,
            currentUserRef: actorRef,
            canCreate: canCreateExpressions,
            canManage: canManageExpressions,
            canUse: isOwner || _guild.allows(Permission.useSoundboard),
          ),
        ),
      if (canCreateExpressions || canManageExpressions)
        (
          label: L10n.of(context).ui_stickers_1609e2dd,
          description: L10n.of(context)
              .ui_static_and_animated_stickers_available_in_thi_a7e6e49e,
          icon: Icons.sticky_note_2_outlined,
          page: _StickersTab(
            guild: _guild,
            repository: _repository,
            currentUserRef: actorRef,
            canCreate: canCreateExpressions,
            canManage: canManageExpressions,
          )
        ),
      if (canManageWebhooks || managedWebhookChannels.isNotEmpty)
        (
          label: L10n.of(context).ui_integrations_webhooks_e59e195d,
          description:
              L10n.of(context).ui_outgoing_integrations_that_post_here_c2f0636b,
          icon: Icons.webhook_rounded,
          page: _WebhooksTab(
            guild: _guild,
            repository: _repository,
            canManageGuild: canManageWebhooks,
            managedChannels: managedWebhookChannels,
          )
        ),
      if (_guild.channels.any(
        (channel) => channel.type == ChannelType.announcement,
      ))
        (
          label: L10n.of(context).ui_integrations_channels_followed_8fcd2555,
          description: L10n.of(context)
              .ui_follower_channels_and_published_announcement__9dd4254b,
          icon: Icons.campaign_outlined,
          page: AnnouncementManagementTab(
            guild: _guild,
            guilds: mobile.guilds,
            currentUser: mobile.user,
            repository: _repository,
            liveController: ref.read(mobileControllerProvider.notifier),
          ),
        ),
      if (canManageGuild)
        (
          label: L10n.of(context).ui_integrations_bots_apps_f16dc645,
          description: L10n.of(context)
              .ui_installed_bots_and_apps_grants_and_automation_021af7b3,
          icon: Icons.smart_toy_outlined,
          page: _BotIntegrationsTab(
            guild: _guild,
            repository: _repository,
            canManageE2ee: canManageGuild,
            canManageCommandPermissions: isOwner ||
                (_guild.allows(Permission.manageGuild) &&
                    _guild.allows(Permission.manageRoles)),
          ),
        ),
      if (isOwner || _guild.allows(Permission.viewAuditLog))
        (
          label: L10n.of(context).ui_audit_9d973548,
          description:
              L10n.of(context).ui_recent_administrative_actions_ce26f577,
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
        Text(L10n.of(context).ui_guild_settings_33e2baed,
            style: TextStyle(fontSize: 12, color: context.kaede.muted)),
      ],
    );

    return LayoutBuilder(builder: (context, constraints) {
      if (constraints.maxWidth >= 900) {
        return Scaffold(
          appBar: AppBar(title: title),
          body: _loading
              ? Center(child: CircularProgressIndicator())
              : ColoredBox(
                  color: settingsSurface(context),
                  child: Row(
                    children: [
                      NavigationRail(
                        extended: constraints.maxWidth >= 1120,
                        backgroundColor: settingsSurface(context),
                        indicatorColor: settingsRowHover(context),
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
                      VerticalDivider(width: 1),
                      Expanded(child: sections[_selectedSection].page),
                    ],
                  ),
                ),
        );
      }
      return Scaffold(
        appBar: AppBar(title: title),
        body: _loading
            ? Center(child: CircularProgressIndicator())
            : ColoredBox(
                color: settingsSurface(context),
                child: ListView(
                  padding: EdgeInsets.fromLTRB(14, 12, 14, 30),
                  children: [
                    Padding(
                      padding: EdgeInsets.symmetric(horizontal: 4),
                      child: Row(
                        children: [
                          GuildIcon(guild: _guild, size: 56, borderRadius: 16),
                          SizedBox(width: 14),
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
                                SizedBox(height: 2),
                                Text(
                                  _guild.ref.domain.value,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    color: context.kaede.muted,
                                    fontSize: 12.5,
                                  ),
                                ),
                                if (_guild.description?.trim().isNotEmpty ==
                                    true) ...[
                                  SizedBox(height: 6),
                                  Text(
                                    _guild.description!.trim(),
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                    style: TextStyle(
                                      color: context.kaede.muted,
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
                    SizedBox(height: 22),
                    for (final section in sections)
                      _SectionRow(
                        label: section.label,
                        description: section.description,
                        icon: section.icon,
                        divider: true,
                        onTap: () => Navigator.of(context).push<void>(
                          MaterialPageRoute<void>(
                            builder: (context) => Scaffold(
                              backgroundColor: settingsSurface(context),
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
            summary:
                L10n.of(context).ui_could_not_load_the_guild_settings_b6e86f92,
          )),
          backgroundColor: context.kaede.danger));
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
  Widget build(BuildContext context) => SettingsRow.chevron(
        title: label,
        subtitle: description,
        leading: Icon(icon, color: context.kaede.coralText),
        onTap: onTap,
        divider: divider,
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
      return Icon(Icons.emoji_emotions_outlined,
          size: 19, color: context.kaede.muted);
    }
    return ClipRRect(
      borderRadius: BorderRadius.circular(6),
      child: CachedNetworkImage(
        imageUrl:
            Uri.https(domain, '/media/emojis/$id/thumbnail_128').toString(),
        width: 26,
        height: 26,
        fit: BoxFit.contain,
        placeholder: (_, __) => SizedBox.square(dimension: 26),
        errorWidget: (_, __, ___) => Icon(
          Icons.emoji_emotions_outlined,
          size: 19,
          color: context.kaede.muted,
        ),
      ),
    );
  }
}

/// What an invite allows, in one line.
String inviteSummaryLine(Map<String, Object?> invite) {
  final uses = invite['uses'];
  final maximum = invite['max_uses'];
  final expires = invite['expires_at'];
  final parts = <String>[];
  if (uses is num) {
    parts.add(maximum is num && maximum > 0
        ? '$uses of $maximum uses'
        : '$uses uses');
  }
  if (expires is String && expires.isNotEmpty) {
    final at = DateTime.tryParse(expires);
    parts.add(at == null ? 'expires' : 'expires ${_shortDate(at.toLocal())}');
  } else {
    parts.add('never expires');
  }
  final roles = invite['role_ids'];
  if (roles is List && roles.isNotEmpty) {
    parts.add('grants ${roles.length} role${roles.length == 1 ? '' : 's'}');
  }
  final targetUserCount = invite['target_user_count'];
  if (targetUserCount is num && targetUserCount > 0) {
    parts.add(
        'limited to $targetUserCount user${targetUserCount == 1 ? '' : 's'}');
  }
  if (invite['scheduled_event_id'] != null) parts.add('scheduled event');
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
        padding: EdgeInsets.symmetric(vertical: 40),
        child: Column(
          children: [
            Icon(icon, size: 30, color: context.kaede.muted),
            SizedBox(height: 12),
            Text(
              title,
              style: TextStyle(
                fontWeight: FontWeight.w700,
                fontSize: 14.5,
              ),
            ),
            SizedBox(height: 4),
            Text(
              body,
              textAlign: TextAlign.center,
              style: TextStyle(color: context.kaede.muted, fontSize: 13),
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
        padding: EdgeInsets.fromLTRB(2, 2, 2, 14),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: EdgeInsets.only(top: 1, right: 9),
              child: Icon(Icons.info_outline_rounded,
                  size: 15, color: context.kaede.muted),
            ),
            Expanded(
              child: Text(
                message,
                style: TextStyle(
                  color: context.kaede.muted,
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
                  SizedBox(width: 11),
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
                                style: TextStyle(
                                  fontWeight: FontWeight.w600,
                                  fontSize: 14.5,
                                ),
                              ),
                            ),
                            if (badge case final indicator?) ...[
                              SizedBox(width: 6),
                              indicator,
                            ],
                          ],
                        ),
                        Text(
                          subtitle,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: context.kaede.muted,
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
    ChannelType.stage => 'Stage channel',
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
    required this.canManageAssets,
    required this.isOwner,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final Future<KaedeGuild> Function([String?]) changed;
  final bool canManage;
  final bool canManageAssets;
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
          summary: L10n.of(context)
              .ui_this_guild_s_notification_preference_could_no_a756f155,
        );
      });
    }
  }

  Future<void> _chooseHistory(String current) async {
    final chosen = await showSettingsChoiceSheet(
      context,
      title: L10n.of(context).ui_federated_message_history_298c1048,
      description: L10n.of(context)
          .ui_whether_instances_that_join_later_may_fetch_o_91bad957,
      choices: [
        SettingsChoice('disabled', L10n.of(context).ui_disabled_bfce9925,
            hint:
                'The recommended default. New instances start with a blank history.'),
        SettingsChoice('full_retained',
            L10n.of(context).ui_share_permitted_history_21a3c13c,
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
          title: L10n.of(context).ui_guild_profile_ade7f5a5,
          subtitle: L10n.of(context)
              .ui_identity_and_presentation_are_federated_to_ev_1b8fac78,
          child: Column(children: [
            Row(children: [
              GuildIcon(guild: _guild, size: 64, borderRadius: 19),
              SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    OutlinedButton.icon(
                      onPressed: _busy || !widget.canManageAssets
                          ? null
                          : () => _asset('icon'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: Size(0, 38),
                        padding: EdgeInsets.symmetric(horizontal: 12),
                        textStyle: TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      icon: Icon(Icons.image_outlined, size: 16),
                      label: Text(L10n.of(context).ui_change_icon_ca1b1c46),
                    ),
                    if (_guild.iconHash != null) ...[
                      SizedBox(height: 6),
                      OutlinedButton.icon(
                        onPressed: _busy || !widget.canManageAssets
                            ? null
                            : () => _removeAsset('icon'),
                        style: OutlinedButton.styleFrom(
                          foregroundColor: context.kaede.danger,
                          minimumSize: Size(0, 38),
                          padding: EdgeInsets.symmetric(horizontal: 12),
                        ),
                        icon: Icon(Icons.delete_outline_rounded, size: 16),
                        label: Text(L10n.of(context).ui_remove_icon_bca65d06),
                      ),
                    ],
                    SizedBox(height: 6),
                    OutlinedButton.icon(
                      onPressed: _busy || !widget.canManageAssets
                          ? null
                          : () => _asset('banner'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: Size(0, 38),
                        padding: EdgeInsets.symmetric(horizontal: 12),
                        textStyle: TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      icon: Icon(Icons.panorama_outlined, size: 16),
                      label: Text(L10n.of(context).ui_change_banner_ae718a61),
                    ),
                    if (_guild.bannerHash != null) ...[
                      SizedBox(height: 6),
                      OutlinedButton.icon(
                        onPressed: _busy || !widget.canManageAssets
                            ? null
                            : () => _removeAsset('banner'),
                        style: OutlinedButton.styleFrom(
                          foregroundColor: context.kaede.danger,
                          minimumSize: Size(0, 38),
                          padding: EdgeInsets.symmetric(horizontal: 12),
                        ),
                        icon: Icon(Icons.delete_outline_rounded, size: 16),
                        label: Text(L10n.of(context).ui_remove_banner_74d33021),
                      ),
                    ],
                  ],
                ),
              ),
            ]),
            SizedBox(height: 18),
            SettingsField(
              label: L10n.of(context).ui_guild_name_da252e43,
              controller: _name,
              enabled: widget.canManage,
            ),
            SizedBox(height: 16),
            SettingsField(
              label: L10n.of(context).ui_description_41c75cc9,
              controller: _description,
              maxLines: 4,
              maxLength: 500,
              enabled: widget.canManage,
            ),
            SizedBox(height: 18),
            SettingsChoiceRow(
              title: L10n.of(context).ui_federated_message_history_298c1048,
              subtitle: L10n.of(context)
                  .ui_whether_instances_that_join_later_may_fetch_o_dfbef346,
              value: _history,
              display: _history == 'full_retained'
                  ? 'Share permitted history'
                  : 'Disabled',
              onSelected:
                  widget.canManage ? (value) => _chooseHistory(value) : (_) {},
            ),
            SizedBox(height: 18),
            SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                  onPressed: _busy || !widget.canManage ? null : _save,
                  icon: Icon(Icons.save_outlined),
                  label: Text(L10n.of(context).ui_save_changes_61efb279)),
            ),
          ]),
        ),
        _Panel(
          title: L10n.of(context).ui_notifications_381d190f,
          subtitle: L10n.of(context)
              .ui_what_this_guild_is_allowed_to_notify_you_abou_67dbc438,
          child: Column(children: [
            if (_notificationError case final warning?) ...[
              DecoratedBox(
                decoration: BoxDecoration(
                  color: context.kaede.warning.withValues(alpha: 0.1),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(
                      color: context.kaede.warning.withValues(alpha: .4)),
                ),
                child: Padding(
                  padding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                  child: Row(
                    children: [
                      Icon(Icons.warning_amber_rounded,
                          size: 18, color: context.kaede.warning),
                      SizedBox(width: 10),
                      Expanded(
                          child: Text(warning,
                              style: TextStyle(
                                  color: context.kaede.textSoft,
                                  fontSize: 12.5,
                                  height: 1.4))),
                      TextButton(
                        onPressed: _loadNotificationSettings,
                        child: Text(L10n.of(context).ui_retry_8036af59),
                      ),
                    ],
                  ),
                ),
              ),
              SizedBox(height: 10),
            ],
            SegmentedButton<String>(
              segments: [
                ButtonSegment(
                    value: 'all',
                    label: Text(L10n.of(context).ui_all_messages_a6bb8868)),
                ButtonSegment(
                    value: 'mentions',
                    label: Text(L10n.of(context).ui_mentions_43e4fe52)),
                ButtonSegment(
                    value: 'none',
                    label: Text(L10n.of(context).ui_nothing_2cea492a)),
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
          title: L10n.of(context).ui_ownership_5bfe229e,
          subtitle: L10n.of(context)
              .ui_remote_instances_keep_untrusted_replicas_dele_c3cd0afd,
          child: Column(children: [
            SettingsRow.chevron(
              title: L10n.of(context).ui_transfer_ownership_0b7722a7,
              leading: Padding(
                padding: EdgeInsets.all(3),
                child: Icon(Icons.swap_horiz_rounded,
                    size: 20, color: context.kaede.muted),
              ),
              divider: true,
              onTap: widget.isOwner ? _transfer : null,
            ),
            SettingsRow.chevron(
              title: L10n.of(context).ui_leave_guild_a37329a3,
              leading: Padding(
                padding: EdgeInsets.all(3),
                child: Icon(Icons.logout_rounded,
                    size: 20, color: context.kaede.muted),
              ),
              divider: true,
              onTap: _leave,
            ),
            SettingsRow(
              danger: true,
              title: L10n.of(context).ui_delete_guild_260ee9b1,
              leading: Padding(
                padding: EdgeInsets.all(3),
                child: Icon(Icons.delete_forever_outlined,
                    size: 20, color: context.kaede.danger),
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

  Future<void> _removeAsset(String kind) async {
    final label = kind == 'icon' ? 'guild icon' : 'guild banner';
    if (!await _confirm(
      context,
      'Remove the $label?',
      'You can upload a new $label at any time.',
      destructive: true,
    )) {
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.repository.removeGuildAsset(
        guild: _guild.ref,
        kind: kind,
      );
      final updated = await widget.changed(
        '${kind == 'icon' ? 'Guild icon' : 'Guild banner'} removed',
      );
      if (mounted) setState(() => _guild = updated);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not remove the $label', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _transfer() async {
    final value = await _prompt(
        context, 'Transfer ownership', 'Member reference (ID@instance)',
        warning:
            'Choose an eligible human member. Ownership moves immediately and you remain a member.');
    if (value == null || !mounted) return;
    try {
      late final EntityRef member;
      try {
        member = EntityRef.parse(value, localDomain: _guild.ref.domain);
      } on FormatException {
        throw UserInputException(
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
    required this.actorRef,
    required this.repository,
    required this.changed,
    required this.canManageChannels,
    required this.isOwner,
    required this.e2eeClient,
  });
  final KaedeGuild guild;
  final EntityRef? actorRef;
  final KaedeRepository repository;
  final Future<KaedeGuild> Function([String?]) changed;
  final bool canManageChannels;
  final bool isOwner;
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
        backgroundColor: settingsSurface(context),
        body: ReorderableListView.builder(
          buildDefaultDragHandles: false,
          padding: EdgeInsets.all(14),
          header: const _TabHint(
            'Press and hold a row to reorder it. Use a row’s menu to move a '
            'channel between categories, or the + on a category to create one '
            'inside it.',
          ),
          itemCount: _channels.length,
          onReorder: _reorder,
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
              enabled: _canReorderChannel(channel),
              child: _ManagementRow(
                indented: channel.parentRef != null,
                onTap: _canManageChannel(channel) ? () => _edit(channel) : null,
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
                  color: context.kaede.muted,
                ),
                title: channel.name ?? 'channel',
                subtitle: channelSummaryLine(channel, parent),
                badge: channel.encryptionMode == 'e2ee'
                    ? Icon(Icons.lock_rounded,
                        size: 13, color: context.kaede.mint)
                    : null,
                trailing: _channelActions(channel, index),
              ),
            );
          },
        ),
        floatingActionButton: FloatingActionButton.extended(
            onPressed: widget.canManageChannels ? _create : null,
            icon: Icon(Icons.add_rounded),
            label: Text(L10n.of(context).ui_create_channel_fd2226d6)),
      );

  Widget _channelActions(KaedeChannel channel, int index) {
    final canManageChannel = _canManageChannel(channel);
    final canManagePermissions = _canManagePermissions(channel);
    if (!canManageChannel && !canManagePermissions) {
      return SizedBox.square(
        dimension: 44,
        child: Icon(Icons.lock_outline_rounded,
            size: 18, color: context.kaede.muted),
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
          if (canManageChannel)
            PopupMenuItem(
                value: 'edit',
                child: Text(L10n.of(context).ui_edit_channel_dfb3900a)),
          if (widget.canManageChannels &&
              canManageChannel &&
              channel.type != ChannelType.category)
            PopupMenuItem(
                value: 'move',
                child: Text(L10n.of(context).ui_move_to_category_dac98463)),
          if (canManagePermissions)
            PopupMenuItem(
                value: 'permissions',
                child: Text(L10n.of(context).ui_permissions_8700fd83)),
          if (canManageChannel &&
              {
                ChannelType.text,
                ChannelType.announcement,
                ChannelType.voice,
              }.contains(channel.type) &&
              (channel.encryptionMode == 'e2ee' || _e2eeActivationEnabled))
            PopupMenuItem(
              value: 'encryption',
              child: Text(channel.encryptionMode == 'e2ee'
                  ? L10n.of(context).ui_encryption_settings_233c72d9
                  : L10n.of(context).ui_enable_encryption_a0595627),
            ),
          if (canManageChannel)
            PopupMenuItem(
                value: 'delete',
                child: Text(L10n.of(context).ui_delete_5797ea6a,
                    style: TextStyle(color: context.kaede.danger))),
        ],
      ),
      if (_canReorderChannel(channel)) ...[
        if (channel.type == ChannelType.category)
          Tooltip(
            message:
                L10n.of(context).ui_add_a_channel_to_this_category_d12cb039,
            child: InkWell(
              onTap: () => _createChannel(initialParent: channel.ref),
              borderRadius: BorderRadius.circular(10),
              child: SizedBox.square(
                dimension: 44,
                child: Icon(Icons.add_rounded, color: context.kaede.muted),
              ),
            ),
          )
        else
          ReorderableDragStartListener(
            index: index,
            child: Tooltip(
              message: L10n.of(context).ui_drag_to_reorder_fce503d3,
              child: SizedBox.square(
                dimension: 44,
                child:
                    Icon(Icons.drag_handle_rounded, color: context.kaede.muted),
              ),
            ),
          ),
      ],
    ]);
  }

  bool _canManageChannel(KaedeChannel channel) => canManageEffectiveChannel(
        channel,
        Permission.manageChannels,
        isOwner: widget.isOwner,
      );

  bool _canManagePermissions(KaedeChannel channel) => canManageEffectiveChannel(
        channel,
        Permission.manageRoles,
        isOwner: widget.isOwner,
      );

  bool _canUseCategory(KaedeChannel channel) =>
      channelCategoryTargetEligible(channel, isOwner: widget.isOwner);

  bool _canReorderChannel(KaedeChannel channel) {
    return channelPositionReorderAllowed(
      channel,
      _channels,
      canManageGuildChannels: widget.canManageChannels,
      isOwner: widget.isOwner,
    );
  }

  Future<void> _reorder(int oldIndex, int newIndex) async {
    if (oldIndex < 0 ||
        oldIndex >= _channels.length ||
        !_canReorderChannel(_channels[oldIndex])) {
      return;
    }
    final movedRef = _channels[oldIndex].ref;
    if (newIndex > oldIndex) newIndex--;
    final previous = [..._channels];
    setState(() {
      final item = _channels.removeAt(oldIndex);
      _channels.insert(newIndex, item);
    });
    try {
      final request = guildChannelPositionRequest(
        previous,
        _channels,
        movedRef: movedRef,
      );
      final requestRefs = request.map((item) => '${item['id']}').toSet();
      if (_channels.any((channel) =>
          requestRefs.contains(channel.ref.id.value) &&
          !_canReorderChannel(channel))) {
        throw UserInputException('Channel permissions changed. Try again.');
      }
      await widget.repository.reorderChannels(
        widget.guild.ref,
        request,
      );
      await widget.changed();
    } on Object catch (error) {
      if (!mounted) return;
      setState(() => _channels = previous);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: L10n.of(context).ui_could_not_reorder_the_channels_419a2fc9,
        )),
        backgroundColor: context.kaede.danger,
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
            candidate.ref != channel.ref &&
            _canUseCategory(candidate))
          candidate,
    ]..sort((a, b) => a.position.compareTo(b.position));
    final current = channel.parentRef?.wire ?? '';
    final chosen = await showSettingsChoiceSheet(
      context,
      title: L10n.of(context).ui_move_to_category_dac98463,
      description: L10n.of(context).ui_choose_where_value0_lives_855fa127(
          (channel.name ?? 'the channel').toString()),
      selected: categories.any((category) => category.ref.wire == current)
          ? current
          : '',
      choices: [
        SettingsChoice('', L10n.of(context).ui_no_category_44662d06),
        for (final category in categories)
          SettingsChoice(category.ref.wire, category.name ?? 'Category'),
      ],
    );
    if (chosen == null) return;
    final currentCategories = _channels.where((candidate) =>
        candidate.type == ChannelType.category && _canUseCategory(candidate));
    final selected = chosen.isEmpty
        ? null
        : currentCategories
            .where((category) => category.ref.wire == chosen)
            .firstOrNull;
    if (!_canUseCategory(channel) || (chosen.isNotEmpty && selected == null)) {
      if (mounted) {
        _tabError(
          context,
          'Could not move the channel',
          UserInputException('Channel permissions changed. Try again.'),
        );
      }
      return;
    }
    final target = selected?.ref;
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
        guildChannelPositionRequest(
          previous,
          next,
          movedRef: channel.ref,
        ),
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
          summary: L10n.of(context).ui_could_not_move_the_channel_da939b2c,
        )),
        backgroundColor: context.kaede.danger,
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
        nsfw: channel.nsfw,
        parentRef: parent,
        lastMessageRef: channel.lastMessageRef,
        recipients: channel.recipients,
        conversationType: channel.conversationType,
        ownerRef: channel.ownerRef,
        slowModeSeconds: channel.slowModeSeconds,
        bitrate: channel.bitrate,
        userLimit: channel.userLimit,
        rtcRegion: channel.rtcRegion,
        videoQualityMode: channel.videoQualityMode,
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
      loadVoiceRegions: () => widget.repository.voiceRegions(widget.guild.ref),
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
    final categories = <KaedeChannel>[
      for (final candidate in _channels)
        if (_canUseCategory(candidate) || candidate.ref == channel.parentRef)
          candidate,
    ];
    final value = await showGuildChannelEditorSheet(
      context,
      channel: channel,
      channels: categories,
      e2eeActivationEnabled: _e2eeActivationEnabled,
      loadVoiceRegions: () => widget.repository.voiceRegions(widget.guild.ref),
    );
    if (value == null || !mounted) return;
    final current = _channels
        .where((candidate) => candidate.ref == channel.ref)
        .firstOrNull;
    final changingParent = value.parentRef != current?.parentRef;
    final parentAllowed = value.parentRef == null ||
        _channels.any((candidate) =>
            candidate.ref == value.parentRef && _canUseCategory(candidate));
    if (current == null ||
        !_canManageChannel(current) ||
        (changingParent && !parentAllowed)) {
      _tabError(
        context,
        'Could not save channel',
        UserInputException('Channel permissions changed. Try again.'),
      );
      return;
    }
    try {
      final updated = await widget.repository.updateChannel(
          widget.guild.ref, current.ref, current.version ?? '*', value.json);
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
                summary: L10n
                    .current.ui_could_not_update_end_to_end_encryption_8cd06684,
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
                ? L10n.of(context).ui_end_to_end_encryption_21f71771
                : L10n.of(context).ui_enable_end_to_end_encryption_5096f759),
            content: SizedBox(
              width: 520,
              child: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(encrypted
                        ? needsRekey
                            ? L10n.of(context)
                                .ui_encrypted_activity_is_paused_until_a_manager__be3437b6
                            : L10n.of(context).ui_encryption_is_value0_b7e2290d(
                                (channel.encryptionState).toString())
                        : media
                            ? L10n.of(context)
                                .ui_voice_video_screen_video_and_screen_audio_wil_3aa150a1
                            : L10n.of(context)
                                .ui_new_messages_and_files_will_be_encrypted_on_p_3b833b3c),
                    SizedBox(height: 12),
                    Text(
                      L10n.of(context)
                          .ui_until_members_compare_the_channel_safety_numb_b0edc22b,
                    ),
                    SizedBox(height: 12),
                    Text(media
                        ? L10n.of(context)
                            .ui_server_recording_transcription_media_moderati_f0e397de
                        : L10n.of(context)
                            .ui_search_link_previews_server_file_previews_and_39ca98fe),
                    if (safetyNumber != null) ...[
                      SizedBox(height: 14),
                      Text(L10n.of(context).ui_channel_safety_number_a5771c0b,
                          style: TextStyle(fontWeight: FontWeight.w800)),
                      SelectableText(safetyNumber!),
                      SizedBox(height: 6),
                      Text(L10n.of(context)
                          .ui_compare_this_with_members_through_a_trusted_c_a53388d7),
                    ],
                    if (error != null) ...[
                      SizedBox(height: 12),
                      Text(error!,
                          style: TextStyle(color: context.kaede.coral)),
                    ],
                  ],
                ),
              ),
            ),
            actions: [
              TextButton(
                onPressed: busy ? null : () => Navigator.pop(dialogContext),
                child: Text(L10n.of(context).ui_done_8dd31791),
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
                  child:
                      Text(L10n.of(context).ui_verify_safety_number_070dce0b),
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
                  label: Text(needsRekey
                      ? L10n.of(context).ui_rotate_keys_014b6162
                      : L10n.of(context).ui_enable_8614a9ee),
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
                  actorRef: widget.actorRef,
                  actorHighestRole: guildActorHighestRole(widget.guild),
                  channel: channel,
                  heldPermissions: effectiveChannelPermissionCeiling(
                    channel,
                    isOwner: widget.isOwner,
                  ),
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
        backgroundColor: settingsSurface(context),
        body: ReorderableListView.builder(
          buildDefaultDragHandles: false,
          padding: EdgeInsets.all(14),
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
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                  content: Text(L10n.of(context)
                      .ui_roles_cannot_be_moved_above_your_role_ceiling_d199e5af),
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
                  summary:
                      L10n.of(context).ui_could_not_reorder_the_roles_105d4f32,
                )),
                backgroundColor: context.kaede.danger,
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
                            ? context.kaede.muted
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
                            ? context.kaede.muted
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
                        child: Tooltip(
                          message: L10n.of(context).ui_drag_to_reorder_fce503d3,
                          child: SizedBox.square(
                            dimension: 44,
                            child: Icon(Icons.drag_handle_rounded,
                                color: context.kaede.muted),
                          ),
                        ),
                      )
                    : Tooltip(
                        message: L10n.of(context)
                            .ui_this_role_is_above_your_role_ceiling_a73f88be,
                        child: SizedBox.square(
                          dimension: 44,
                          child: Icon(Icons.lock_outline_rounded,
                              size: 18, color: context.kaede.muted),
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
            icon: Icon(Icons.add_rounded),
            label: Text(L10n.of(context).ui_create_role_b4d57cd3)),
      );

  bool _canMove(KaedeRole role) {
    if (_isEveryoneRole(role)) return false;
    return widget.guild.allows(Permission.manageRoles) &&
            guildActorCanManageRole(
              guild: widget.guild,
              actorRef: widget.actorRef,
              actorHighestRole: guildActorHighestRole(widget.guild),
              target: role,
            ) ||
        widget.actorRef == widget.guild.ownerRef;
  }

  bool _isEveryoneRole(KaedeRole role) =>
      role.position == 0 || role.ref == widget.guild.ref;

  Future<void> _edit(KaedeRole? role) async {
    final heldPermissions = effectiveGuildPermissionCeiling(
      widget.guild,
      actorRef: widget.actorRef,
    );
    final draft = await Navigator.push<_RoleDraft>(
        context,
        MaterialPageRoute(
            builder: (_) => _RoleEditor(
                  role: role,
                  heldPermissions: heldPermissions,
                )));
    if (draft == null) return;
    if (!draft.delete) {
      final updatedPermissions =
          BigInt.tryParse('${draft.json['permissions'] ?? ''}');
      final currentCeiling = effectiveGuildPermissionCeiling(
        widget.guild,
        actorRef: widget.actorRef,
      );
      if (updatedPermissions == null ||
          !rolePermissionChangesWithinCeiling(
            role?.permissions ?? BigInt.zero,
            updatedPermissions,
            currentCeiling,
          )) {
        if (mounted) {
          _tabError(context, 'Could not update role',
              'You can only change permissions you currently hold.');
        }
        return;
      }
    }
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
          throw FormatException('Choose a PNG, JPEG, GIF, or WebP image.');
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
      required this.liveMembers,
      required this.userProfiles,
      required this.repository,
      required this.changed});
  final KaedeGuild guild;
  final EntityRef? actorRef;
  final List<GuildMember> liveMembers;
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
  final Set<EntityRef> _removedMemberRefs = <EntityRef>{};
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
    final changedGuild = oldWidget.guild.ref != widget.guild.ref;
    if (changedGuild) {
      _removedMemberRefs.clear();
    } else if (!identical(oldWidget.liveMembers, widget.liveMembers)) {
      _removedMemberRefs.addAll(removedGuildManagementMemberRefs(
        oldWidget.liveMembers,
        widget.liveMembers,
      ));
      _removedMemberRefs
          .removeAll(widget.liveMembers.map((member) => member.user.ref));
      _members = reconcileGuildManagementMembers(
        members: _members,
        liveMembers: widget.liveMembers,
        removedRefs: _removedMemberRefs,
        query: _search.text,
      );
    }
    if (changedGuild || oldWidget.guild.version != widget.guild.version) {
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
      Duration(milliseconds: 300),
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
        _members = reconcileGuildManagementMembers(
          members: <GuildMember>[
            if (!reset) ..._members,
            ...data.where((member) => known.add(member.user.ref)),
          ],
          liveMembers: widget.liveMembers,
          removedRefs: _removedMemberRefs,
          query: query,
        );
        _hasMore = data.length == 100;
      });
    } on Object catch (error) {
      if (mounted && generation == _requestGeneration) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(userFacingError(
              error,
              summary: L10n.of(context).ui_could_not_load_the_members_f9e59295,
            )),
            backgroundColor: context.kaede.danger,
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
        color: settingsSurface(context),
        child: Column(children: [
          Padding(
            padding: EdgeInsets.fromLTRB(16, 14, 16, 12),
            child: TextField(
              controller: _search,
              textInputAction: TextInputAction.search,
              onChanged: (_) => setState(() {}),
              onSubmitted: (_) => _load(reset: true),
              style: TextStyle(fontSize: 14),
              decoration: InputDecoration(
                hintText: L10n.of(context).ui_search_members_d6e1fdce,
                hintStyle:
                    TextStyle(color: context.kaede.muted, fontSize: 13.5),
                prefixIcon: Icon(Icons.search_rounded,
                    size: 18, color: context.kaede.muted),
                suffixIcon: _search.text.isEmpty
                    ? null
                    : IconButton(
                        tooltip: L10n.of(context).ui_clear_search_5a13e946,
                        onPressed: () {
                          _search.clear();
                          _load(reset: true);
                        },
                        icon: Icon(Icons.close_rounded, size: 18),
                      ),
                isDense: true,
                contentPadding: EdgeInsets.symmetric(vertical: 10),
                filled: true,
                fillColor: context.kaede.canvas,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: context.kaede.border),
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: context.kaede.border),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide:
                      BorderSide(color: context.kaede.coral, width: 1.4),
                ),
              ),
            ),
          ),
          Expanded(
              child: _loading
                  ? Center(child: CircularProgressIndicator())
                  : RefreshIndicator(
                      onRefresh: () => _load(reset: true),
                      child: ListView.builder(
                        controller: _scroll,
                        physics: AlwaysScrollableScrollPhysics(),
                        itemCount: _members.length + (_loadingMore ? 1 : 0),
                        itemBuilder: (_, index) {
                          if (index == _members.length) {
                            return Padding(
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
                            padding: EdgeInsets.symmetric(horizontal: 14),
                            child: _ManagementRow(
                              leading:
                                  UserAvatar(user: member.user, radius: 15),
                              title: member.nickname ?? member.user.name,
                              subtitle: member.user.profileResolved
                                  ? <String>[
                                      member.user.handle,
                                      if (roleCount > 0)
                                        L10n.of(context)
                                            .ui_value0_role_value1_5b8d3554(
                                                (roleCount).toString(),
                                                (roleCount == 1 ? '' : 's')
                                                    .toString()),
                                      if (member.timeoutUntil != null)
                                        L10n.of(context).ui_timed_out_9a0e1624,
                                    ].join(' · ')
                                  : L10n.of(context)
                                      .ui_profile_unavailable_refreshes_automatically_9ef31289,
                              badge: member.user.ref == widget.guild.ownerRef ||
                                      member.user.isApplication
                                  ? Row(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        if (member.user.ref ==
                                            widget.guild.ownerRef)
                                          Icon(
                                            Icons.workspace_premium_rounded,
                                            size: 13,
                                            color: context.kaede.warning,
                                          ),
                                        if (member.user.isApplication) ...[
                                          const SizedBox(width: 5),
                                          const ApplicationTag(compact: true),
                                        ],
                                      ],
                                    )
                                  : null,
                              trailing: actions.isEmpty
                                  ? null
                                  : PopupMenuButton<String>(
                                      tooltip: L10n.of(context)
                                          .ui_member_actions_8d33b0d8,
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
      items.add(PopupMenuItem(
          value: 'roles',
          child: Text(L10n.of(context).ui_manage_roles_de7a2fe3)));
    }
    if (_canChangeNickname(member)) {
      items.add(PopupMenuItem(
          value: 'nickname',
          child: Text(L10n.of(context).ui_change_nickname_123a63b7)));
    }
    if (!self && !targetIsOwner && canManageTarget) {
      if (owner || widget.guild.allows(Permission.moderateMembers)) {
        items.add(PopupMenuItem(
            value: 'timeout',
            child: Text(L10n.of(context).ui_timeout_82d97e28)));
      }
      if (owner || widget.guild.allows(Permission.kickMembers)) {
        items.add(PopupMenuItem(
            value: 'kick', child: Text(L10n.of(context).ui_kick_2cfbaa3f)));
      }
      if (owner || widget.guild.allows(Permission.banMembers)) {
        items.add(PopupMenuItem(
            value: 'ban',
            child: Text(L10n.of(context).ui_ban_bfbc51a6,
                style: TextStyle(color: context.kaede.danger))));
      }
    }
    return items;
  }

  bool _canAssignRoles(GuildMember member) {
    final owner = widget.guild.ownerRef == widget.actorRef;
    final self = member.user.ref == widget.actorRef;
    return (owner || widget.guild.allows(Permission.manageRoles)) &&
        (self || _canManageMember(member));
  }

  bool _canManageMember(GuildMember member) => guildActorCanManageMember(
        guild: widget.guild,
        actorRef: widget.actorRef,
        actorHighestRole: guildActorHighestRole(widget.guild),
        target: member,
      );

  bool _canChangeNickname(GuildMember member) => canChangeGuildMemberNickname(
        guild: widget.guild,
        actorRef: widget.actorRef,
        actorHighestRole: guildActorHighestRole(widget.guild),
        target: member,
      );

  GuildMember? _currentMember(EntityRef ref) => guildMemberByRef(_members, ref);

  Future<void> _action(GuildMember member, String action) async {
    try {
      final currentAtOpen = _currentMember(member.user.ref);
      if (currentAtOpen == null) return;
      member = currentAtOpen;
      switch (action) {
        case 'roles':
          if (!_canAssignRoles(member)) return;
          final actorHighestRole = guildActorHighestRole(widget.guild);
          final selected = await showDialog<Set<String>>(
              context: context,
              builder: (_) => _RoleAssignmentDialog(
                  member: member,
                  roles: widget.guild.roles
                      .where((role) =>
                          role.position != 0 &&
                          role.ref != widget.guild.ref &&
                          guildActorCanManageRole(
                            guild: widget.guild,
                            actorRef: widget.actorRef,
                            actorHighestRole: actorHighestRole,
                            target: role,
                          ))
                      .toList()));
          if (selected == null) return;
          final current = _currentMember(member.user.ref);
          if (current == null || !_canAssignRoles(current)) return;
          await widget.repository
              .replaceMemberRoles(widget.guild.ref, current.user.ref, selected);
          break;
        case 'nickname':
          if (!_canChangeNickname(member)) return;
          final nickname =
              await _prompt(context, 'Change nickname', 'Nickname');
          if (nickname == null) return;
          final current = _currentMember(member.user.ref);
          if (current == null || !_canChangeNickname(current)) return;
          await widget.repository.updateMember(
              widget.guild.ref,
              current.user.ref,
              {'nickname': nickname.isEmpty ? null : nickname});
          break;
        case 'timeout':
          if (!_canManageMember(member) ||
              (widget.guild.ownerRef != widget.actorRef &&
                  !widget.guild.allows(Permission.moderateMembers))) {
            return;
          }
          final choice = await showModerationOptions(
            context,
            title: L10n.of(context)
                .ui_timeout_value0_11b9b0cd((member.user.name).toString()),
            timeout: true,
          );
          if (choice == null) return;
          final current = _currentMember(member.user.ref);
          if (current == null ||
              !_canManageMember(current) ||
              (widget.guild.ownerRef != widget.actorRef &&
                  !widget.guild.allows(Permission.moderateMembers))) {
            return;
          }
          final indefinite = choice.durationSeconds < 0;
          await widget.repository.updateMember(
            widget.guild.ref,
            current.user.ref,
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
          if (!_canManageMember(member) ||
              (widget.guild.ownerRef != widget.actorRef &&
                  !widget.guild.allows(Permission.kickMembers))) {
            return;
          }
          final reason = await _prompt(
              context, 'Kick ${member.user.name}?', 'Reason (optional)');
          if (reason == null) return;
          final current = _currentMember(member.user.ref);
          if (current == null ||
              !_canManageMember(current) ||
              (widget.guild.ownerRef != widget.actorRef &&
                  !widget.guild.allows(Permission.kickMembers))) {
            return;
          }
          await widget.repository
              .kick(widget.guild.ref, current.user.ref, reason: reason);
          break;
        case 'ban':
          if (!_canManageMember(member) ||
              (widget.guild.ownerRef != widget.actorRef &&
                  !widget.guild.allows(Permission.banMembers))) {
            return;
          }
          final choice = await showModerationOptions(
            context,
            title: L10n.of(context)
                .ui_ban_value0_b94c36f0((member.user.name).toString()),
            includeDeleteHistory: true,
          );
          if (choice == null) return;
          final current = _currentMember(member.user.ref);
          if (current == null ||
              !_canManageMember(current) ||
              (widget.guild.ownerRef != widget.actorRef &&
                  !widget.guild.allows(Permission.banMembers))) {
            return;
          }
          await widget.repository.ban(
            widget.guild.ref,
            current.user.ref,
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
          summary:
              L10n.of(context).ui_could_not_apply_the_member_action_f0ce665c,
        )),
        backgroundColor: context.kaede.danger,
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
      ? Center(child: CircularProgressIndicator())
      : _PageList(children: [
          _Panel(
              title: L10n.of(context).ui_member_bans_4e7de23b,
              child: Column(children: [
                if (!widget.canBanMembers)
                  Padding(
                    padding: EdgeInsets.symmetric(vertical: 8),
                    child: Row(
                      children: [
                        Icon(Icons.lock_outline_rounded,
                            size: 19, color: context.kaede.muted),
                        SizedBox(width: 11),
                        Text(
                            L10n.of(context)
                                .ui_you_cannot_manage_member_bans_17cba37e,
                            style: TextStyle(
                                color: context.kaede.muted, fontSize: 13.5)),
                      ],
                    ),
                  ),
                if (_bans.isEmpty)
                  if (widget.canBanMembers)
                    Padding(
                      padding: EdgeInsets.symmetric(vertical: 8),
                      child: Text(
                          L10n.of(context).ui_no_banned_members_31edfedd,
                          style: TextStyle(
                              color: context.kaede.muted, fontSize: 13.5)),
                    ),
                for (final ban in _bans)
                  SettingsRow(
                      title: _mapName(ban),
                      subtitle: L10n.of(context).ui_value0_26e9163c(
                          (ban['reason'] ?? 'No reason').toString()),
                      leading: Padding(
                        padding: EdgeInsets.all(3),
                        child: Icon(Icons.person_outline_rounded,
                            size: 20, color: context.kaede.muted),
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
                            foregroundColor: context.kaede.danger,
                            minimumSize: Size(0, 34),
                            padding: EdgeInsets.symmetric(horizontal: 10),
                          ),
                          child: Text(L10n.of(context).ui_unban_84c561ff))),
              ])),
          _Panel(
              title: L10n.of(context).ui_banned_instances_803d2b81,
              subtitle: L10n.of(context)
                  .ui_this_prevents_every_account_hosted_by_that_do_c00cf818,
              child: Column(children: [
                SettingsRow.chevron(
                    title: L10n.of(context).ui_ban_an_instance_5b6843fc,
                    leading: Padding(
                      padding: EdgeInsets.all(3),
                      child: Icon(Icons.public_off_rounded,
                          size: 20, color: context.kaede.muted),
                    ),
                    divider: true,
                    onTap: widget.canBanInstances ? _addInstance : null),
                for (final ban in _instances)
                  if (guildInstanceBanDomain(ban) case final domain?)
                    SettingsRow(
                        title: domain.value,
                        subtitle: L10n.of(context)
                                .ui_value0_26e9163c(
                                    (ban['reason'] ?? 'No reason').toString())
                                .isEmpty
                            ? L10n.of(context)
                                .ui_every_account_on_this_domain_is_blocked_e77c5aa6
                            : L10n.of(context).ui_value0_26e9163c(
                                (ban['reason'] ?? '').toString()),
                        leading: Padding(
                          padding: EdgeInsets.all(3),
                          child: Icon(Icons.public_rounded,
                              size: 20, color: context.kaede.muted),
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
                              foregroundColor: context.kaede.danger,
                              minimumSize: Size(0, 34),
                              padding: EdgeInsets.symmetric(horizontal: 10),
                            ),
                            child: Text(L10n.of(context).ui_remove_21a5901d)))
                  else
                    Padding(
                      padding: EdgeInsets.symmetric(vertical: 8),
                      child: Row(
                        children: [
                          Icon(Icons.warning_amber_rounded,
                              size: 19, color: context.kaede.warning),
                          SizedBox(width: 11),
                          Expanded(
                            child: Text(
                              L10n.of(context)
                                  .ui_invalid_instance_ban_record_refresh_or_contac_0ca2c462,
                              style: TextStyle(
                                  color: context.kaede.muted, fontSize: 13.5),
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
      title: L10n.of(context).ui_ban_an_entire_instance_a8523156,
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
        throw UserInputException(
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
    required this.actorRef,
    required this.canCreate,
    required this.canManage,
    required this.canListGuild,
    required this.managedChannels,
    required this.canManageRoles,
    required this.currentGuild,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final EntityRef? actorRef;
  final bool canCreate;
  final bool canManage;
  final bool canListGuild;
  final List<KaedeChannel> managedChannels;
  final bool canManageRoles;
  final KaedeGuild? Function() currentGuild;
  @override
  State<_InvitesTab> createState() => _InvitesTabState();
}

final class _InvitesTabState extends State<_InvitesTab> {
  List<Map<String, Object?>> _items = const [];
  var _loading = true;
  KaedeChannel? _selectedChannel;
  @override
  void initState() {
    super.initState();
    _selectedChannel = widget.managedChannels.firstOrNull;
    _load();
  }

  @override
  void didUpdateWidget(covariant _InvitesTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.canCreate != widget.canCreate ||
        oldWidget.canManage != widget.canManage ||
        oldWidget.canListGuild != widget.canListGuild ||
        !listEquals(oldWidget.managedChannels.map((item) => item.ref).toList(),
            widget.managedChannels.map((item) => item.ref).toList()) ||
        oldWidget.canManageRoles != widget.canManageRoles ||
        oldWidget.actorRef != widget.actorRef) {
      if (_selectedChannel == null ||
          !widget.managedChannels
              .any((item) => item.ref == _selectedChannel!.ref)) {
        _selectedChannel = widget.managedChannels.firstOrNull;
      }
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final items = widget.canListGuild
          ? await widget.repository.invites(widget.guild.ref)
          : _selectedChannel == null
              ? const <Map<String, Object?>>[]
              : await widget.repository.channelInvites(_selectedChannel!.ref);
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
      backgroundColor: settingsSurface(context),
      body: _loading
          ? Center(child: CircularProgressIndicator())
          : ListView(
              padding: EdgeInsets.fromLTRB(14, 12, 14, 90),
              children: [
                const _TabHint(
                  'Anyone with an invite link can join. Treat private invites '
                  'like passwords.',
                ),
                if (!widget.canListGuild && widget.managedChannels.isEmpty)
                  const _TabHint(
                    'Manage Guild is required to list every guild invite.',
                  ),
                if (!widget.canListGuild && widget.managedChannels.isNotEmpty)
                  DropdownButtonFormField<KaedeChannel>(
                    initialValue: _selectedChannel,
                    decoration: InputDecoration(
                      labelText: L10n.of(context).ui_channel_invites_9a2fab12,
                      prefixIcon: Icon(Icons.tag_rounded),
                    ),
                    items: widget.managedChannels
                        .map((channel) => DropdownMenuItem(
                              value: channel,
                              child: Text(channel.name ?? channel.ref.wire),
                            ))
                        .toList(growable: false),
                    onChanged: (channel) {
                      if (channel == null ||
                          channel.ref == _selectedChannel?.ref) {
                        return;
                      }
                      setState(() {
                        _selectedChannel = channel;
                        _loading = true;
                      });
                      unawaited(_load());
                    },
                  ),
                if ((widget.canListGuild ||
                        widget.managedChannels.isNotEmpty) &&
                    _items.isEmpty)
                  _TabEmpty(
                    icon: Icons.link_off_rounded,
                    title: L10n.of(context).ui_no_active_invites_4ee55886,
                    body: 'Create one to bring people in.',
                  ),
                for (final item in _items)
                  _ManagementRow(
                    leading: Icon(Icons.link_rounded,
                        size: 19, color: context.kaede.muted),
                    title: L10n.of(context)
                        .ui_value0_26e9163c((item['code']).toString()),
                    subtitle: inviteSummaryLine(item),
                    onTap: () => _copyInvite('${item['code']}'),
                    trailing: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        IconButton(
                          tooltip:
                              L10n.of(context).ui_copy_invite_link_a7c3b223,
                          onPressed: () => _copyInvite('${item['code']}'),
                          icon: Icon(Icons.copy_rounded, size: 18),
                        ),
                        IconButton(
                          tooltip: L10n.of(context).ui_revoke_invite_a3ecfb24,
                          style: IconButton.styleFrom(
                            foregroundColor: context.kaede.danger,
                          ),
                          onPressed: _canRevoke(item)
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
                                    await widget.repository.revokeInvite(
                                      '${item['code']}',
                                      guild: widget.guild.ref,
                                    );
                                    await _load();
                                  } on Object catch (error) {
                                    if (mounted) {
                                      _tabError(this.context,
                                          'Could not revoke invite', error);
                                    }
                                  }
                                }
                              : null,
                          icon: Icon(Icons.delete_outline_rounded, size: 18),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: widget.canCreate ? _create : null,
          icon: Icon(Icons.person_add_alt_1),
          label: Text(L10n.of(context).ui_create_invite_918a1372)));

  bool _canRevoke(Map<String, Object?> invite) {
    if (widget.canManage) return true;
    final channelId = '${invite['channel_id'] ?? ''}';
    return channelId.isNotEmpty &&
        widget.managedChannels
            .any((channel) => channel.ref.id.value == channelId);
  }

  Future<void> _copyInvite(String code) async {
    final host = widget.guild.ref.domain.value;
    await Clipboard.setData(ClipboardData(text: 'https://$host/invite/$code'));
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(L10n.of(context).ui_invite_link_copied_b511318e)),
    );
  }

  Future<void> _create() async {
    List<GuildScheduledEvent> events;
    try {
      events = await widget.repository.scheduledEvents(widget.guild.ref);
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not load scheduled events', error);
      return;
    }
    List<GuildMember> members = const <GuildMember>[];
    try {
      members = await widget.repository.members(widget.guild.ref);
    } on Object {
      // Normal and scheduled-event invites remain available if the optional
      // live-target discovery surfaces cannot be loaded.
    }
    if (!mounted) return;
    final request = await showAdvancedInviteEditor(
      context,
      widget.guild,
      scheduledEvents: events,
      members: members,
      actorRef: widget.actorRef,
      canManageRoles: widget.canManageRoles,
    );
    if (request == null || !mounted) return;
    final currentGuild = widget.currentGuild();
    if (currentGuild == null ||
        !inviteRequestStillAuthorized(
          currentGuild,
          widget.actorRef,
          request,
        )) {
      _tabError(
        context,
        'Could not create invite',
        UserInputException(
          'Invite permissions changed before the invite was created.',
        ),
      );
      return;
    }
    try {
      await widget.repository.createInvite(currentGuild.ref, request);
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create invite', error);
    }
  }
}

bool inviteRequestStillAuthorized(
  KaedeGuild guild,
  EntityRef? actorRef,
  Map<String, Object?> request,
) {
  final rawChannel = '${request['channel_id'] ?? ''}'.trim();
  if (rawChannel.isEmpty) {
    return actorRef == guild.ownerRef || guild.allows(Permission.createInvite);
  }
  EntityRef target;
  try {
    target = rawChannel.contains('@')
        ? EntityRef.parse(rawChannel)
        : EntityRef(Snowflake(rawChannel), guild.ref.domain);
  } on FormatException {
    return false;
  }
  return guildInviteCreationTargets(
    guild.channels,
    isOwner: actorRef == guild.ownerRef,
  ).any((channel) => channel.ref == target);
}

Future<(int?, int?)?> showInviteRestrictions(BuildContext context) async {
  var age = 604800;
  var uses = 100;
  return showDialog<(int?, int?)>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setDialogState) => AlertDialog(
        title: Text(L10n.of(context).ui_invite_limits_e57813c0),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            DropdownButtonFormField<int>(
              initialValue: age,
              decoration: InputDecoration(
                  labelText: L10n.of(context).ui_expires_after_9311682d),
              items: [
                DropdownMenuItem(
                    value: 1800,
                    child: Text(L10n.of(context).ui_30_minutes_80e4b47f)),
                DropdownMenuItem(
                    value: 21600,
                    child: Text(L10n.of(context).ui_6_hours_27b1b222)),
                DropdownMenuItem(
                    value: 86400,
                    child: Text(L10n.of(context).ui_1_day_db83588e)),
                DropdownMenuItem(
                    value: 604800,
                    child: Text(L10n.of(context).ui_7_days_ba4b8279)),
                DropdownMenuItem(
                    value: 0, child: Text(L10n.of(context).ui_never_b2ff2b29)),
              ],
              onChanged: (value) => setDialogState(() => age = value ?? age),
            ),
            SizedBox(height: 12),
            DropdownButtonFormField<int>(
              initialValue: uses,
              decoration: InputDecoration(
                  labelText: L10n.of(context).ui_maximum_uses_e65fe543),
              items: [
                DropdownMenuItem(
                    value: 1, child: Text(L10n.of(context).ui_1_use_626b601f)),
                DropdownMenuItem(
                    value: 5, child: Text(L10n.of(context).ui_5_uses_8c613760)),
                DropdownMenuItem(
                    value: 10,
                    child: Text(L10n.of(context).ui_10_uses_9fef9f3c)),
                DropdownMenuItem(
                    value: 25,
                    child: Text(L10n.of(context).ui_25_uses_2f0f35f0)),
                DropdownMenuItem(
                    value: 100,
                    child: Text(L10n.of(context).ui_100_uses_ddfef284)),
                DropdownMenuItem(
                    value: 0,
                    child: Text(L10n.of(context).ui_unlimited_2d283a80)),
              ],
              onChanged: (value) => setDialogState(() => uses = value ?? uses),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(
              dialogContext,
              (age == 0 ? null : age, uses == 0 ? null : uses),
            ),
            child: Text(L10n.of(context).ui_create_invite_918a1372),
          ),
        ],
      ),
    ),
  );
}

Future<Map<String, Object?>?> showAdvancedInviteEditor(
  BuildContext context,
  KaedeGuild guild, {
  List<GuildScheduledEvent> scheduledEvents = const [],
  List<GuildMember> members = const [],
  EntityRef? actorRef,
  bool canManageRoles = false,
}) async {
  var age = 604800;
  var uses = 100;
  var temporary = false;
  var unique = false;
  var targetType = 'none';
  String? channelRef;
  EntityRef? targetUserRef;
  EntityRef? scheduledEventRef;
  final selectedRoleRefs = <EntityRef>{};
  String? validationError;
  final channels = guildInviteCreationTargets(
    guild.channels,
    isOwner: actorRef == guild.ownerRef,
  );
  final actorHighestRole = guildActorHighestRole(guild);
  final assignableRoles = guild.roles
      .where((role) =>
          !role.managed &&
          role.position != 0 &&
          role.ref != guild.ref &&
          canManageRoles &&
          guildActorCanManageRole(
            guild: guild,
            actorRef: actorRef,
            actorHighestRole: actorHighestRole,
            target: role,
          ))
      .toList()
    ..sort((a, b) => b.position.compareTo(a.position));
  final result = await showDialog<Map<String, Object?>>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setDialogState) {
        final availableChannels = channels;
        if (channelRef != null &&
            !availableChannels.any((item) => item.ref.wire == channelRef)) {
          channelRef = null;
        }
        void submit() {
          if (targetType == 'stream' && targetUserRef == null) {
            setDialogState(() => validationError = L10n.of(context)
                .ui_choose_the_member_whose_stream_should_open_47283c3a);
            return;
          }
          Navigator.pop(dialogContext, <String, Object?>{
            'channel_id': channelRef,
            'max_age_seconds': age == 0 ? null : age,
            'max_uses': uses == 0 ? null : uses,
            'temporary': temporary,
            'unique': unique,
            if (targetType != 'none') 'target_type': targetType,
            if (targetType == 'stream') 'target_user_id': targetUserRef!.wire,
            if (scheduledEventRef != null)
              'scheduled_event_id': scheduledEventRef!.wire,
            if (selectedRoleRefs.isNotEmpty)
              'role_ids': selectedRoleRefs.map((role) => role.wire).toList()
                ..sort(),
          });
        }

        return AlertDialog(
          title: Text(L10n.of(context).ui_create_advanced_invite_76fe1666),
          content: SizedBox(
            width: 520,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  DropdownButtonFormField<String?>(
                    initialValue: channelRef,
                    isExpanded: true,
                    decoration: InputDecoration(
                      labelText: L10n.of(context).ui_channel_655d8a44,
                      helperText: L10n.of(context)
                          .ui_optional_leave_empty_for_a_guild_level_invite_c5adce41,
                    ),
                    items: [
                      DropdownMenuItem<String?>(
                        value: null,
                        child: Text(L10n.of(context)
                            .ui_guild_landing_no_channel_4f86f176),
                      ),
                      for (final channel in availableChannels)
                        DropdownMenuItem<String?>(
                          value: channel.ref.wire,
                          child: Text(
                            L10n.of(context).ui_value0_value1_c1a3651c(
                                (channel.type.isVoiceLike ? '🔊' : '#')
                                    .toString(),
                                (channel.name ?? 'channel').toString()),
                          ),
                        ),
                    ],
                    onChanged: (value) =>
                        setDialogState(() => channelRef = value),
                  ),
                  SizedBox(height: 12),
                  DropdownButtonFormField<int>(
                    initialValue: age,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_expires_after_9311682d),
                    items: [
                      DropdownMenuItem(
                          value: 1800,
                          child: Text(L10n.of(context).ui_30_minutes_80e4b47f)),
                      DropdownMenuItem(
                          value: 21600,
                          child: Text(L10n.of(context).ui_6_hours_27b1b222)),
                      DropdownMenuItem(
                          value: 86400,
                          child: Text(L10n.of(context).ui_1_day_db83588e)),
                      DropdownMenuItem(
                          value: 604800,
                          child: Text(L10n.of(context).ui_7_days_ba4b8279)),
                      DropdownMenuItem(
                          value: 0,
                          child: Text(L10n.of(context).ui_never_b2ff2b29)),
                    ],
                    onChanged: (value) =>
                        setDialogState(() => age = value ?? age),
                  ),
                  SizedBox(height: 12),
                  DropdownButtonFormField<int>(
                    initialValue: uses,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_maximum_uses_e65fe543),
                    items: [
                      DropdownMenuItem(
                          value: 1,
                          child: Text(L10n.of(context).ui_1_use_626b601f)),
                      DropdownMenuItem(
                          value: 5,
                          child: Text(L10n.of(context).ui_5_uses_8c613760)),
                      DropdownMenuItem(
                          value: 10,
                          child: Text(L10n.of(context).ui_10_uses_9fef9f3c)),
                      DropdownMenuItem(
                          value: 25,
                          child: Text(L10n.of(context).ui_25_uses_2f0f35f0)),
                      DropdownMenuItem(
                          value: 100,
                          child: Text(L10n.of(context).ui_100_uses_ddfef284)),
                      DropdownMenuItem(
                          value: 0,
                          child: Text(L10n.of(context).ui_unlimited_2d283a80)),
                    ],
                    onChanged: (value) =>
                        setDialogState(() => uses = value ?? uses),
                  ),
                  SizedBox(height: 12),
                  DropdownButtonFormField<String>(
                    initialValue: targetType,
                    decoration: InputDecoration(
                      labelText:
                          L10n.of(context).ui_voice_invite_target_bca51f73,
                      helperText: L10n.of(context)
                          .ui_requires_a_voice_or_stage_destination_59941a17,
                    ),
                    items: [
                      DropdownMenuItem(
                          value: 'none',
                          child: Text(L10n.of(context).ui_none_304ff7fb)),
                      DropdownMenuItem(
                          value: 'stream',
                          child: Text(L10n.of(context)
                              .ui_member_s_go_live_stream_685000f6)),
                    ],
                    onChanged: (value) => setDialogState(() {
                      targetType = value ?? 'none';
                      validationError = null;
                    }),
                  ),
                  if (targetType == 'stream') ...[
                    SizedBox(height: 12),
                    DropdownButtonFormField<EntityRef>(
                      initialValue: targetUserRef,
                      isExpanded: true,
                      decoration: InputDecoration(
                        labelText:
                            L10n.of(context).ui_streaming_member_b3703b3f,
                        helperText: members.isEmpty
                            ? L10n.of(context)
                                .ui_no_members_are_available_ce1d2466
                            : L10n.of(context)
                                .ui_the_member_must_currently_be_able_to_stream_i_95147817,
                      ),
                      items: [
                        for (final member in members)
                          DropdownMenuItem(
                            value: member.user.ref,
                            child: Text(
                              member.nickname ??
                                  member.user.displayName ??
                                  member.user.username,
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                      ],
                      onChanged: members.isEmpty
                          ? null
                          : (value) => setDialogState(() {
                                targetUserRef = value;
                                validationError = null;
                              }),
                    ),
                  ],
                  SizedBox(height: 12),
                  DropdownButtonFormField<EntityRef?>(
                    initialValue: scheduledEventRef,
                    isExpanded: true,
                    decoration: InputDecoration(
                      labelText: L10n.of(context).ui_scheduled_event_ff96a250,
                      helperText: scheduledEvents.isEmpty
                          ? L10n.of(context)
                              .ui_no_upcoming_events_are_available_5548ff9e
                          : L10n.of(context)
                              .ui_optional_and_independent_of_the_voice_target_29ae45bf,
                    ),
                    items: [
                      DropdownMenuItem<EntityRef?>(
                        value: null,
                        child: Text(
                            L10n.of(context).ui_no_event_association_874e0e99),
                      ),
                      for (final event in scheduledEvents)
                        DropdownMenuItem<EntityRef?>(
                          value: event.ref,
                          child: Text(
                            L10n.of(context).ui_value0_value1_947523d9(
                                (event.name).toString(),
                                (DateFormat.yMMMd()
                                        .add_jm()
                                        .format(event.startTime))
                                    .toString()),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: (value) => setDialogState(() {
                      scheduledEventRef = value;
                      validationError = null;
                    }),
                  ),
                  if (assignableRoles.isNotEmpty) ...[
                    SizedBox(height: 16),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: Text(L10n.of(context).ui_roles_optional_5359fafd,
                          style: Theme.of(context).textTheme.titleSmall),
                    ),
                    SizedBox(height: 4),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: Text(
                        L10n.of(context)
                            .ui_members_receive_these_roles_when_they_accept__050cdefd,
                        style: Theme.of(context).textTheme.bodySmall?.copyWith(
                              color: context.kaede.muted,
                            ),
                      ),
                    ),
                    SizedBox(height: 8),
                    for (final role in assignableRoles)
                      CheckboxListTile(
                        contentPadding: EdgeInsets.zero,
                        dense: true,
                        value: selectedRoleRefs.contains(role.ref),
                        title: Text(role.name),
                        onChanged: (selected) => setDialogState(() {
                          if (selected ?? false) {
                            selectedRoleRefs.add(role.ref);
                          } else {
                            selectedRoleRefs.remove(role.ref);
                          }
                        }),
                      ),
                  ],
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: temporary,
                    title:
                        Text(L10n.of(context).ui_temporary_membership_4f72bf5c),
                    subtitle: Text(
                      L10n.of(context)
                          .ui_remove_members_when_they_disconnect_unless_a__3922afa5,
                    ),
                    onChanged: (value) =>
                        setDialogState(() => temporary = value),
                  ),
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    value: unique,
                    title: Text(L10n.of(context)
                        .ui_always_create_a_unique_code_310a73ab),
                    subtitle: Text(
                      L10n.of(context)
                          .ui_otherwise_kaede_may_reuse_a_compatible_invite_e6bba363,
                    ),
                    onChanged: (value) => setDialogState(() => unique = value),
                  ),
                  if (validationError != null)
                    Align(
                      alignment: Alignment.centerLeft,
                      child: Text(
                        validationError!,
                        style: TextStyle(color: context.kaede.danger),
                      ),
                    ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
                onPressed: submit,
                child: Text(L10n.of(context).ui_create_invite_918a1372)),
          ],
        );
      },
    ),
  );
  return result;
}

final class _EmojiTab extends StatefulWidget {
  const _EmojiTab({
    required this.guild,
    required this.repository,
    required this.currentUserRef,
    required this.canCreate,
    required this.canManage,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final EntityRef? currentUserRef;
  final bool canCreate;
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
        oldWidget.currentUserRef != widget.currentUserRef ||
        oldWidget.canCreate != widget.canCreate ||
        oldWidget.canManage != widget.canManage) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    try {
      final items = await widget.repository.guildEmojis(widget.guild.ref);
      if (mounted) {
        setState(() {
          _items = items;
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
      backgroundColor: settingsSurface(context),
      body: _loading
          ? Center(child: CircularProgressIndicator())
          : ListView(
              padding: EdgeInsets.fromLTRB(14, 12, 14, 90),
              children: [
                const _TabHint(
                  'Custom emoji work in other guilds for members who are '
                  'allowed to use external emoji. Federated tokens keep the '
                  'emoji\u2019s origin.',
                ),
                if (_items.isEmpty)
                  _TabEmpty(
                    icon: Icons.emoji_emotions_outlined,
                    title: L10n.of(context).ui_no_custom_emoji_yet_ceb2cb11,
                    body: 'Upload a PNG, GIF or WebP up to 256 KB.',
                  ),
                for (final item in _items)
                  _ManagementRow(
                    leading: _EmojiThumbnail(
                      emoji: item,
                      fallbackDomain: widget.guild.ref.domain,
                    ),
                    title: L10n.of(context)
                        .ui_value0_2397634a((item['name']).toString()),
                    subtitle: emojiRestrictionSummary(item),
                    trailing: (widget.canManage ||
                            (widget.canCreate &&
                                guildExpressionOwnedBy(
                                  item,
                                  widget.currentUserRef,
                                )))
                        ? PopupMenuButton<String>(
                            key: ValueKey(
                              'emoji-actions-${_mapRef(item, widget.guild.ref.domain).wire}',
                            ),
                            onSelected: (value) =>
                                value == 'edit' ? _edit(item) : _delete(item),
                            itemBuilder: (_) => [
                              PopupMenuItem(
                                value: 'edit',
                                child: Text(
                                    L10n.of(context).ui_edit_emoji_26aed3f7),
                              ),
                              PopupMenuItem(
                                value: 'delete',
                                child: Text(
                                  L10n.of(context).ui_delete_emoji_892d89b8,
                                  style: TextStyle(color: context.kaede.danger),
                                ),
                              ),
                            ],
                          )
                        : null,
                  ),
              ],
            ),
      floatingActionButton: widget.canCreate
          ? FloatingActionButton.extended(
              onPressed: _upload,
              icon: Icon(Icons.add_photo_alternate_outlined),
              label: Text(L10n.of(context).ui_upload_emoji_1e4ec752),
            )
          : null);

  Future<void> _edit(Map<String, Object?> item) async {
    final patch = await showEmojiSettingsEditor(
      context,
      guild: widget.guild,
      emoji: item,
    );
    if (patch == null || !mounted) return;
    try {
      await widget.repository.updateGuildEmoji(
        widget.guild.ref,
        _mapRef(item, widget.guild.ref.domain),
        patch,
      );
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not save emoji', error);
    }
  }

  Future<void> _delete(Map<String, Object?> item) async {
    if (!await _confirm(
      context,
      'Delete :${item['name']}:?',
      'Messages that already use it will show the name instead.',
      destructive: true,
    )) {
      return;
    }
    try {
      await widget.repository.deleteEmoji(
        widget.guild.ref,
        _mapRef(item, widget.guild.ref.domain),
      );
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not delete emoji', error);
    }
  }

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
    final selectedFile = File(file.path);
    if (await selectedFile.length() > widget.guild.emojiMaxBytes) {
      if (mounted) {
        _tabError(context, 'Could not upload emoji',
            'Emoji images can be at most ${(widget.guild.emojiMaxBytes / 1024).ceil()} KiB.');
      }
      return;
    }
    try {
      await widget.repository.uploadEmoji(
          guild: widget.guild.ref,
          name: name,
          filename: file.name,
          contentType: contentType,
          file: selectedFile);
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not upload emoji', error);
    }
  }
}

String emojiRestrictionSummary(Map<String, Object?> emoji) {
  final roles =
      emoji['roles'] is List ? emoji['roles']! as List : const <Object?>[];
  final availability = emoji['available'] == false ? 'disabled' : 'available';
  return roles.isEmpty
      ? 'All members · $availability'
      : '${roles.length} allowed role${roles.length == 1 ? '' : 's'} · $availability';
}

Future<Map<String, Object?>?> showEmojiSettingsEditor(
  BuildContext context, {
  required KaedeGuild guild,
  required Map<String, Object?> emoji,
}) async {
  final name = TextEditingController(text: '${emoji['name'] ?? ''}');
  final available = emoji['available'] != false;
  final selectedRoles = <EntityRef>{};
  for (final raw
      in emoji['roles'] is List ? emoji['roles']! as List : const []) {
    try {
      selectedRoles.add(
        EntityRef.parse('$raw', localDomain: guild.ref.domain),
      );
    } on FormatException {
      // Ignore stale role restrictions that cannot be safely resubmitted.
    }
  }
  String? error;
  final result = await showDialog<Map<String, Object?>>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setDialogState) => AlertDialog(
        title: Text(L10n.of(context)
            .ui_edit_value0_dcc58e10((emoji['name'] ?? 'emoji').toString())),
        content: SizedBox(
          width: 520,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                TextField(
                  controller: name,
                  maxLength: 32,
                  decoration: InputDecoration(
                    labelText: L10n.of(context).ui_name_0fe07306,
                    helperText: L10n.of(context)
                        .ui_2_32_letters_numbers_or_underscores_a62d0546,
                  ),
                ),
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(available
                      ? Icons.check_circle_outline
                      : Icons.do_not_disturb_on_outlined),
                  title: Text(available
                      ? L10n.of(context).ui_available_72402638
                      : L10n.of(context).ui_unavailable_da4baaa9),
                  subtitle: Text(
                    L10n.of(context)
                        .ui_availability_is_controlled_by_the_server_and__f2b39dad,
                  ),
                ),
                Divider(height: 28),
                Text(
                  L10n.of(context).ui_allowed_roles_d65c2a22,
                  style: TextStyle(fontWeight: FontWeight.w800),
                ),
                Text(
                  L10n.of(context)
                      .ui_leave_every_role_unchecked_to_allow_all_membe_479134e0,
                  style: TextStyle(color: context.kaede.muted, fontSize: 12),
                ),
                for (final role in guild.roles)
                  if (role.ref != guild.ref)
                    CheckboxListTile(
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      value: selectedRoles.contains(role.ref),
                      title: Text(role.name),
                      onChanged: (value) => setDialogState(() {
                        if (value == true && selectedRoles.length < 100) {
                          selectedRoles.add(role.ref);
                        } else if (value != true) {
                          selectedRoles.remove(role.ref);
                        }
                      }),
                    ),
                if (error != null)
                  Text(error!, style: TextStyle(color: context.kaede.danger)),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            onPressed: () {
              final cleaned = name.text.trim();
              if (!RegExp(r'^[A-Za-z0-9_]{2,32}$').hasMatch(cleaned)) {
                setDialogState(() => error = L10n.of(context)
                    .ui_use_2_32_letters_numbers_or_underscores_for_t_5c871c2e);
                return;
              }
              Navigator.pop(dialogContext, <String, Object?>{
                'name': cleaned,
                'role_ids': selectedRoles.map((item) => item.wire).toList(),
              });
            },
            child: Text(L10n.of(context).ui_save_emoji_69500726),
          ),
        ],
      ),
    ),
  );
  name.dispose();
  return result;
}

final class _StickersTab extends StatefulWidget {
  const _StickersTab({
    required this.guild,
    required this.repository,
    required this.currentUserRef,
    required this.canCreate,
    required this.canManage,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final EntityRef? currentUserRef;
  final bool canCreate;
  final bool canManage;

  @override
  State<_StickersTab> createState() => _StickersTabState();
}

final class _StickersTabState extends State<_StickersTab> {
  List<ComposerSticker> _items = const [];
  Map<EntityRef, Map<String, Object?>> _raw = const {};
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
      final response = await widget.repository.guildStickers(widget.guild.ref);
      final items = response
          .map(ComposerSticker.tryParse)
          .whereType<ComposerSticker>()
          .toList(growable: false);
      if (!mounted) return;
      setState(() {
        _items = items;
        _raw = {
          for (final item in response)
            if (ComposerSticker.tryParse(item) case final parsed?)
              parsed.ref: item,
        };
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
        backgroundColor: settingsSurface(context),
        body: _loading
            ? Center(child: CircularProgressIndicator())
            : ListView(
                padding: EdgeInsets.fromLTRB(14, 12, 14, 90),
                children: [
                  _TabHint(
                    'Crop an image in the app and optionally remove its '
                    'background. Animated GIF stickers keep their animation. '
                    '${_items.length} of ${widget.guild.stickerLimit} used.',
                  ),
                  if (_items.isEmpty)
                    _TabEmpty(
                      icon: Icons.sticky_note_2_outlined,
                      title: L10n.of(context).ui_no_stickers_yet_374ae8a8,
                      body: 'Upload a PNG, JPEG, GIF or WebP up to '
                          '${(widget.guild.stickerMaxBytes / 1048576).ceil()} MiB.',
                    ),
                  for (final sticker in _items)
                    _ManagementRow(
                      leading: StickerImage(sticker: sticker, size: 46),
                      title: sticker.name,
                      subtitle: stickerSettingsSummary(
                        _raw[sticker.ref] ?? const <String, Object?>{},
                        fallbackDescription: sticker.description,
                      ),
                      trailing: (widget.canManage ||
                              (widget.canCreate &&
                                  guildExpressionOwnedBy(
                                    _raw[sticker.ref] ??
                                        const <String, Object?>{},
                                    widget.currentUserRef,
                                  )))
                          ? PopupMenuButton<String>(
                              key: ValueKey(
                                'sticker-actions-${sticker.ref.wire}',
                              ),
                              enabled: !_busy,
                              onSelected: (value) => value == 'edit'
                                  ? _edit(sticker)
                                  : _delete(sticker),
                              itemBuilder: (_) => [
                                PopupMenuItem(
                                  value: 'edit',
                                  child: Text(L10n.of(context)
                                      .ui_edit_sticker_a47edfbe),
                                ),
                                PopupMenuItem(
                                  value: 'delete',
                                  child: Text(
                                    L10n.of(context).ui_delete_sticker_ac57d8d1,
                                    style:
                                        TextStyle(color: context.kaede.danger),
                                  ),
                                ),
                              ],
                            )
                          : null,
                    ),
                ],
              ),
        floatingActionButton: widget.canCreate
            ? FloatingActionButton.extended(
                onPressed: !_busy && _items.length < widget.guild.stickerLimit
                    ? _upload
                    : null,
                icon: Icon(Icons.add_photo_alternate_outlined),
                label: Text(_busy
                    ? L10n.of(context).ui_creating_01fcd896
                    : L10n.of(context).ui_create_sticker_80b5cb0a),
              )
            : null,
      );

  Future<void> _edit(ComposerSticker sticker) async {
    final patch = await showStickerSettingsEditor(
      context,
      sticker: _raw[sticker.ref] ??
          <String, Object?>{
            'name': sticker.name,
            'description': sticker.description,
          },
    );
    if (patch == null || !mounted) return;
    setState(() => _busy = true);
    try {
      await widget.repository.updateGuildSticker(
        widget.guild.ref,
        sticker.ref,
        patch,
      );
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not save sticker', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

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
            'Sticker images can be at most ${(widget.guild.stickerMaxBytes / 1024).ceil()} KiB.');
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
          SnackBar(
              content: Text(L10n.of(context)
                  .ui_value0_is_ready_to_use_a797f43f((edit.name).toString()))),
        );
      }
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create sticker', error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }
}

String stickerSettingsSummary(
  Map<String, Object?> sticker, {
  String? fallbackDescription,
}) {
  final tags = sticker['tags'] is List
      ? (sticker['tags']! as List).map((item) => '$item').toList()
      : const <String>[];
  final parts = <String>[
    if (fallbackDescription?.trim().isNotEmpty == true)
      fallbackDescription!.trim(),
    if (tags.isNotEmpty) tags.join(', '),
    sticker['available'] == false ? 'disabled' : 'available',
  ];
  return parts.join(' · ');
}

bool validStickerName(String value) {
  final cleaned = value.trim();
  return cleaned.runes.length >= 2 &&
      cleaned.runes.length <= 30 &&
      !cleaned.runes.any((rune) => rune < 32 || rune == 127);
}

bool validStickerDescription(String value) {
  final length = value.trim().runes.length;
  return length == 0 || (length >= 2 && length <= 100);
}

List<String>? normalizedStickerTags(String value) {
  final cleaned = value
      .replaceAll(',', '\n')
      .split('\n')
      .map((item) => item.trim())
      .where((item) => item.isNotEmpty)
      .toList(growable: false);
  if (cleaned.isEmpty ||
      cleaned.length > 10 ||
      cleaned.toSet().length != cleaned.length ||
      cleaned.any((item) => item.runes.length > 100) ||
      cleaned.join(',').runes.length > 200) {
    return null;
  }
  return cleaned;
}

Future<Map<String, Object?>?> showStickerSettingsEditor(
  BuildContext context, {
  required Map<String, Object?> sticker,
}) async {
  final name = TextEditingController(text: '${sticker['name'] ?? ''}');
  final description =
      TextEditingController(text: '${sticker['description'] ?? ''}');
  final tags = TextEditingController(
    text: sticker['tags'] is List
        ? (sticker['tags']! as List).map((item) => '$item').join('\n')
        : '',
  );
  final available = sticker['available'] != false;
  String? error;
  final result = await showDialog<Map<String, Object?>>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setDialogState) => AlertDialog(
        title: Text(L10n.of(context).ui_edit_value0_8e92dac2(
            (sticker['name'] ?? 'sticker').toString())),
        content: SizedBox(
          width: 480,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: name,
                  maxLength: 30,
                  decoration: InputDecoration(
                    labelText: L10n.of(context).ui_name_0fe07306,
                    helperText: L10n.of(context).ui_2_30_characters_387b9381,
                  ),
                ),
                TextField(
                  controller: description,
                  maxLength: 100,
                  decoration: InputDecoration(
                      labelText:
                          L10n.of(context).ui_description_optional_31e71764),
                ),
                TextField(
                  controller: tags,
                  minLines: 2,
                  maxLines: 5,
                  decoration: InputDecoration(
                    labelText: L10n.of(context).ui_tags_76bda0c0,
                    helperText: L10n.of(context)
                        .ui_one_per_line_1_10_unique_tags_200_characters__61eb4094,
                    alignLabelWithHint: true,
                  ),
                ),
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(available
                      ? Icons.check_circle_outline
                      : Icons.do_not_disturb_on_outlined),
                  title: Text(available
                      ? L10n.of(context).ui_available_72402638
                      : L10n.of(context).ui_unavailable_da4baaa9),
                  subtitle: Text(
                    L10n.of(context)
                        .ui_availability_is_controlled_by_the_server_and__f2b39dad,
                  ),
                ),
                if (error != null)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(error!,
                        style: TextStyle(color: context.kaede.danger)),
                  ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            onPressed: () {
              final cleanedName = name.text.trim();
              final cleanedDescription = description.text.trim();
              final cleanedTags = normalizedStickerTags(tags.text);
              if (!validStickerName(cleanedName)) {
                setDialogState(() => error = L10n.of(context)
                    .ui_use_2_30_meaningful_characters_for_the_name_03d0bacf);
                return;
              }
              if (!validStickerDescription(cleanedDescription)) {
                setDialogState(() => error = L10n.of(context)
                    .ui_descriptions_must_be_empty_or_contain_2_100_c_ddf91a48);
                return;
              }
              if (cleanedTags == null) {
                setDialogState(() => error = L10n.of(context)
                    .ui_add_1_10_unique_tags_using_at_most_200_charac_1a954689);
                return;
              }
              Navigator.pop(dialogContext, <String, Object?>{
                'name': cleanedName,
                'description':
                    cleanedDescription.isEmpty ? null : cleanedDescription,
                'tags': cleanedTags,
              });
            },
            child: Text(L10n.of(context).ui_save_sticker_e955143f),
          ),
        ],
      ),
    ),
  );
  name.dispose();
  description.dispose();
  tags.dispose();
  return result;
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
        final canCreate =
            validStickerName(name) && validStickerDescription(description);
        return AlertDialog(
          title: Text(L10n.of(context).ui_create_sticker_80b5cb0a),
          content: SizedBox(
            width: 430,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  TextField(
                    key: ValueKey('sticker-name'),
                    maxLength: 30,
                    decoration: InputDecoration(
                      labelText: L10n.of(context).ui_name_0fe07306,
                      helperText: L10n.of(context).ui_2_30_characters_387b9381,
                    ),
                    onChanged: (value) => setDialogState(() => name = value),
                  ),
                  TextField(
                    key: ValueKey('sticker-description'),
                    maxLength: 100,
                    decoration: InputDecoration(
                      labelText:
                          L10n.of(context).ui_description_optional_31e71764,
                    ),
                    onChanged: (value) =>
                        setDialogState(() => description = value),
                  ),
                  SizedBox(height: 12),
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
                  SizedBox(height: 10),
                  Text(
                    key: ValueKey('sticker-crop-summary'),
                    L10n.of(context).ui_selection_value0_value1_a3be5c32(
                        ((cropWidth * 100).round()).toString(),
                        ((cropHeight * 100).round()).toString()),
                    style: TextStyle(
                      color: context.kaede.muted,
                      fontSize: 12,
                    ),
                  ),
                  SizedBox(height: 4),
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          L10n.of(context)
                              .ui_drag_the_box_to_move_it_drag_a_corner_to_resi_8aea0ecb,
                          style: TextStyle(
                            color: context.kaede.muted,
                            fontSize: 12,
                          ),
                        ),
                      ),
                      TextButton(
                        key: ValueKey('sticker-crop-reset'),
                        onPressed: () => setDialogState(() {
                          cropX = 0;
                          cropY = 0;
                          cropWidth = 1;
                          cropHeight = 1;
                        }),
                        child: Text(L10n.of(context).ui_reset_0ac8a560),
                      ),
                    ],
                  ),
                  SwitchListTile(
                    key: ValueKey('sticker-remove-background'),
                    contentPadding: EdgeInsets.zero,
                    title: Text(L10n.of(context).ui_remove_background_a47feceb),
                    subtitle: Text(animated
                        ? L10n.of(context)
                            .ui_background_removal_is_unavailable_for_animate_b08efd28
                        : backgroundRemovalAvailable
                            ? L10n.of(context)
                                .ui_creates_a_transparent_cutout_on_the_server_01590512
                            : L10n.of(context)
                                .ui_this_server_has_not_enabled_background_remova_db177c6e),
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
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton.icon(
              onPressed: canCreate
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
              icon: Icon(Icons.add_photo_alternate_outlined),
              label: Text(L10n.of(context).ui_create_990de47d),
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
                key: ValueKey('sticker-crop-preview'),
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
                            color: context.kaede.rail,
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
                            label: L10n.of(context)
                                .ui_crop_selection_drag_to_move_c0ba9b5b,
                            child: Stack(
                              clipBehavior: Clip.none,
                              children: [
                                Positioned.fill(
                                  child: Listener(
                                    key: ValueKey('sticker-crop-selection'),
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
        child: IgnorePointer(
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
        label: L10n.of(context).ui_resize_crop_from_value0_corner_f027c9d1(
            (corner.name).toString()),
        child: Listener(
          key: ValueKey('sticker-crop-handle-${corner.name}'),
          behavior: HitTestBehavior.opaque,
          onPointerDown: (event) => _beginGesture(event, corner),
          child: Center(
            child: Container(
              width: 15,
              height: 15,
              decoration: BoxDecoration(
                color: context.kaede.coral,
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
              child: VerticalDivider(
                width: 1,
                thickness: 1,
                color: Color(0x66FFFFFF),
              ),
            ),
            Align(
              alignment: Alignment(0, alignment),
              child: Divider(
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
  const _BotIntegrationsTab({
    required this.guild,
    required this.repository,
    required this.canManageE2ee,
    required this.canManageCommandPermissions,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canManageE2ee;
  final bool canManageCommandPermissions;

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
        backgroundColor: settingsSurface(context),
        body: _loading
            ? Center(child: CircularProgressIndicator())
            : RefreshIndicator(
                onRefresh: _load,
                child: ListView(
                  padding: EdgeInsets.fromLTRB(14, 12, 14, 24),
                  children: [
                    const _TabHint(
                      'Bots and apps keep only the scopes, live-event intents, guild permissions and channel access approved for this server. Removing one immediately revokes future access.',
                    ),
                    if (_items.isEmpty)
                      _TabEmpty(
                        icon: Icons.smart_toy_outlined,
                        title: L10n.of(context)
                            .ui_no_bots_or_apps_installed_5544cdf9,
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
    final channelRestrictions =
        (item['channel_restrictions'] as List? ?? const [])
            .map((e) => '$e')
            .toList();
    final permissions = _installedApplicationPermissions(item['permissions']);
    return Card(
      margin: EdgeInsets.only(top: 10),
      child: Padding(
        padding: EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              CircleAvatar(
                backgroundColor: context.kaede.coralSoft,
                foregroundColor: context.kaede.coralText,
                child: Text(name.characters.first.toUpperCase()),
              ),
              SizedBox(width: 12),
              Expanded(
                  child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(name, style: TextStyle(fontWeight: FontWeight.w800)),
                  Text(
                      L10n.of(context).ui_value0_26e9163c(
                          (bot['handle'] ?? application['origin_domain'] ?? '')
                              .toString()),
                      style:
                          TextStyle(color: context.kaede.muted, fontSize: 12)),
                ],
              )),
              IconButton(
                tooltip: L10n.of(context).ui_remove_bot_869c788c,
                color: context.kaede.danger,
                onPressed: () => _remove(item, application, name),
                icon: Icon(Icons.delete_outline_rounded),
              ),
            ]),
            if ('${application['description'] ?? ''}'.trim().isNotEmpty) ...[
              SizedBox(height: 8),
              Text(
                  L10n.of(context).ui_value0_26e9163c(
                      (application['description']).toString()),
                  style: TextStyle(color: context.kaede.textSoft)),
            ],
            SizedBox(height: 10),
            Wrap(spacing: 6, runSpacing: 6, children: [
              _Tag('${item['status'] ?? 'unknown'}'),
              _Tag('${item['e2ee_mode'] ?? 'disabled'} E2EE'),
              _Tag('${scopes.length} scopes'),
              _Tag('${intents.length} intents'),
            ]),
            if (scopes.isNotEmpty ||
                intents.isNotEmpty ||
                permissions.isNotEmpty)
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                title: Text(L10n.of(context).ui_approved_access_7875cc68),
                children: [
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      [
                        ...scopes.map((scope) => 'Scope · $scope'),
                        ...intents.map((intent) => 'Event · $intent'),
                        ...permissions.map(
                          (permission) => 'Permission · ${permission.label}',
                        ),
                      ].join('\n'),
                      style:
                          TextStyle(color: context.kaede.muted, height: 1.45),
                    ),
                  ),
                ],
              ),
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.tag_rounded),
              title: Text(L10n.of(context).ui_channel_access_b7959e86),
              subtitle: Text(
                channelRestrictions.isEmpty
                    ? L10n.of(context)
                        .ui_all_channels_allowed_by_the_bot_role_2dc63875
                    : L10n.of(context)
                        .ui_value0_selected_channels_or_categories_fbef2df8(
                            (channelRestrictions.length).toString()),
              ),
              trailing: Icon(Icons.chevron_right_rounded),
              onTap: () => _editChannelAccess(item, application, name),
            ),
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.tune_rounded),
              title: Text(L10n.of(context).ui_command_permissions_24ca2c7e),
              subtitle: Text(
                L10n.of(context)
                    .ui_choose_which_roles_members_and_channels_can_u_fad0659a,
              ),
              trailing: Icon(Icons.chevron_right_rounded),
              onTap: () {
                final applicationRef = _applicationRef(application);
                Navigator.of(context).push(MaterialPageRoute<void>(
                  builder: (_) => ApplicationCommandPermissionsScreen(
                    guild: widget.guild,
                    application: applicationRef,
                    applicationName: name,
                    repository: widget.repository,
                    canManage: widget.canManageCommandPermissions,
                  ),
                ));
              },
            ),
            if (item['e2ee_mode'] == 'participant')
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.enhanced_encryption_outlined),
                title:
                    Text(L10n.of(context).ui_encrypted_channel_access_45930d32),
                subtitle: Text(
                  L10n.of(context)
                      .ui_grant_review_or_revoke_this_app_per_encrypted_daceaecd,
                ),
                trailing: const Icon(Icons.chevron_right_rounded),
                onTap: () {
                  Navigator.of(context).push(MaterialPageRoute<void>(
                    builder: (_) => BotE2eeParticipationScreen(
                      guild: widget.guild,
                      application: _applicationRef(application),
                      applicationName: name,
                      repository: widget.repository,
                      canManage: widget.canManageE2ee,
                    ),
                  ));
                },
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
      final applicationRef = _applicationRef(application);
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

  Future<void> _editChannelAccess(
    Map<String, Object?> item,
    Map<String, Object?> application,
    String name,
  ) async {
    final channels = widget.guild.channels
        .where(
          (channel) => !const <ChannelType>{
            ChannelType.announcementThread,
            ChannelType.publicThread,
            ChannelType.privateThread,
          }.contains(channel.type),
        )
        .toList(growable: false)
      ..sort((left, right) {
        final position = left.position.compareTo(right.position);
        return position != 0
            ? position
            : left.ref.wire.compareTo(right.ref.wire);
      });
    final selected = (item['channel_restrictions'] as List? ?? const [])
        .map((value) => '$value')
        .toSet();
    final updatedSelection = await showDialog<Set<String>>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(L10n.of(context)
              .ui_value0_channel_access_31bc35cb((name).toString())),
          content: SizedBox(
            width: 520,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    L10n.of(context)
                        .ui_this_ceiling_is_applied_in_addition_to_the_bo_b42aa2d1,
                    style: TextStyle(color: context.kaede.muted),
                  ),
                  SizedBox(height: 8),
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text(L10n.of(context)
                        .ui_all_role_permitted_channels_17a7345f),
                    value: selected.isEmpty,
                    onChanged: (enabled) {
                      if (enabled) setDialogState(selected.clear);
                    },
                  ),
                  for (final channel in channels)
                    CheckboxListTile(
                      contentPadding: EdgeInsets.zero,
                      title: Text(channel.name ?? channel.ref.id.value),
                      subtitle: Text(channel.type == ChannelType.category
                          ? L10n.of(context).ui_category_afb3e591
                          : L10n.of(context).ui_channel_655d8a44),
                      value: selected.contains(channel.ref.wire),
                      onChanged: (enabled) => setDialogState(() {
                        if (enabled == true) {
                          selected.add(channel.ref.wire);
                        } else {
                          selected.remove(channel.ref.wire);
                        }
                      }),
                    ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(dialogContext, Set.of(selected)),
              child: Text(L10n.of(context).ui_save_4d2d5d68),
            ),
          ],
        ),
      ),
    );
    if (updatedSelection == null || !mounted) return;
    try {
      final updated =
          await widget.repository.updateBotIntegrationChannelRestrictions(
        widget.guild.ref,
        _applicationRef(application),
        updatedSelection.map(EntityRef.parse),
      );
      if (!mounted) return;
      setState(() {
        _items = _items
            .map((candidate) => candidate != item
                ? candidate
                : <String, Object?>{
                    ...candidate,
                    'status': updated['status'] ?? candidate['status'],
                    'channel_restrictions':
                        updated['channel_restrictions'] ?? const <Object?>[],
                    'grant_revision': updated['grant_revision'] ??
                        candidate['grant_revision'],
                  })
            .toList(growable: false);
      });
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not update channel access', error);
    }
  }

  EntityRef _applicationRef(Map<String, Object?> application) {
    final rawRef = '${application['ref'] ?? ''}';
    return rawRef.isNotEmpty
        ? EntityRef.parse(rawRef)
        : EntityRef(
            Snowflake('${application['id']}'),
            Domain('${application['origin_domain']}'),
          );
  }
}

List<PermissionMetadata> _installedApplicationPermissions(Object? value) {
  try {
    return selectedApplicationPermissions('${value ?? '0'}');
  } on FormatException {
    return const <PermissionMetadata>[];
  }
}

final class _Tag extends StatelessWidget {
  const _Tag(this.label);
  final String label;
  @override
  Widget build(BuildContext context) => Container(
        padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(99),
          border: Border.all(color: context.kaede.border),
        ),
        child: Text(label.replaceAll('_', ' '),
            style: TextStyle(color: context.kaede.muted, fontSize: 11)),
      );
}

final class _WebhooksTab extends StatefulWidget {
  const _WebhooksTab({
    required this.guild,
    required this.repository,
    required this.canManageGuild,
    required this.managedChannels,
  });
  final KaedeGuild guild;
  final KaedeRepository repository;
  final bool canManageGuild;
  final List<KaedeChannel> managedChannels;
  @override
  State<_WebhooksTab> createState() => _WebhooksTabState();
}

final class _WebhooksTabState extends State<_WebhooksTab> {
  List<Map<String, Object?>> _items = const [];
  var _loading = true;
  String? _busyWebhook;
  var _uploadProgress = 0;
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
        oldWidget.canManageGuild != widget.canManageGuild ||
        !listEquals(
          oldWidget.managedChannels.map((channel) => channel.ref).toList(),
          widget.managedChannels.map((channel) => channel.ref).toList(),
        )) {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  bool get _canManage => widget.managedChannels.isNotEmpty;

  Future<void> _load() async {
    try {
      final items = widget.canManageGuild
          ? await widget.repository.webhooks(widget.guild.ref)
          : (await Future.wait(widget.managedChannels.map(
              (channel) => widget.repository
                  .channelWebhooks(widget.guild.ref, channel.ref),
            )))
              .expand((items) => items)
              .toList(growable: false);
      final managed =
          widget.managedChannels.map((channel) => channel.ref).toSet();
      if (mounted) {
        setState(() {
          _items = items
              .where((item) => managed.contains(_webhookChannelRef(item)))
              .toList(growable: false);
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
      backgroundColor: settingsSurface(context),
      body: _loading
          ? Center(child: CircularProgressIndicator())
          : ListView(padding: EdgeInsets.all(14), children: [
              const _TabHint(
                  'Authorized server managers can copy a webhook URL whenever an external website needs it.'),
              if (!_canManage)
                Padding(
                  padding: EdgeInsets.only(top: 8),
                  child: Row(
                    children: [
                      Icon(Icons.lock_outline_rounded,
                          size: 19, color: context.kaede.muted),
                      SizedBox(width: 11),
                      Text(
                          L10n.of(context)
                              .ui_manage_webhooks_is_required_995d93b1,
                          style: TextStyle(
                              color: context.kaede.muted, fontSize: 13.5)),
                    ],
                  ),
                ),
              for (final item in _items)
                _ManagementRow(
                    leading: _WebhookAvatar(
                      avatarHash: item['avatar_hash'] as String?,
                      domain: widget.guild.ref.domain,
                    ),
                    title: L10n.of(context).ui_value0_26e9163c(
                        (item['name'] ?? 'Webhook').toString()),
                    subtitle: _webhookChannelLabel(item),
                    trailing: PopupMenuButton<String>(
                      enabled: _canManage && _busyWebhook == null,
                      onSelected: (value) => _handleAction(item, value),
                      itemBuilder: (_) => [
                        if (item['type'] != 2) ...[
                          if ('${item['execution_url'] ?? ''}'.isNotEmpty)
                            PopupMenuItem(
                                value: 'copy-url',
                                child: Text(L10n.of(context)
                                    .ui_copy_webhook_url_60c3b358)),
                          PopupMenuItem(
                              value: 'edit',
                              child: Text(
                                  L10n.of(context).ui_edit_or_move_474f6553)),
                          PopupMenuItem(
                              value: 'avatar',
                              child: Text(
                                  L10n.of(context).ui_change_avatar_7b892dce)),
                          if (item['avatar_hash'] != null)
                            PopupMenuItem(
                              value: 'clear-avatar',
                              child: Text(
                                  L10n.of(context).ui_remove_avatar_d6403a8e),
                            ),
                          PopupMenuItem(
                              value: 'rotate',
                              child: Text(
                                  L10n.of(context).ui_rotate_token_9c8fb5eb)),
                        ],
                        PopupMenuItem(
                          value: 'delete',
                          child: Text(
                            item[L10n.of(context).ui_type_5127f14d] == 2
                                ? L10n.of(context).ui_stop_following_7a795c5e
                                : L10n.of(context).ui_delete_5797ea6a,
                            style: TextStyle(color: context.kaede.danger),
                          ),
                        ),
                      ],
                    )),
              if (_busyWebhook != null && _uploadProgress > 0)
                Padding(
                  padding: EdgeInsets.fromLTRB(12, 4, 12, 0),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(
                        _uploadProgress < 100
                            ? L10n.of(context)
                                .ui_uploading_webhook_avatar_value0_f73e9506(
                                    (_uploadProgress).toString())
                            : L10n.of(context)
                                .ui_upload_complete_running_media_safety_checks_47ba9a1d,
                        style: TextStyle(
                          color: context.kaede.muted,
                          fontSize: 12,
                        ),
                      ),
                      SizedBox(height: 6),
                      LinearProgressIndicator(
                        key: Key('webhook-avatar-upload-progress'),
                        value: _uploadProgress < 100
                            ? _uploadProgress / 100
                            : null,
                      ),
                    ],
                  ),
                ),
            ]),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: _canManage ? _create : null,
          icon: Icon(Icons.add_rounded),
          label: Text(L10n.of(context).ui_webhook_bac3fbc6)));

  String _webhookChannelLabel(Map<String, Object?> webhook) {
    final id = '${webhook['channel_id'] ?? ''}';
    final domain = '${webhook['channel_domain'] ?? widget.guild.ref.domain}';
    final channel = widget.guild.channels
        .where((item) =>
            item.ref.id.value == id && item.ref.domain.value == domain)
        .firstOrNull;
    final type = switch (channel?.type) {
      ChannelType.announcement => 'announcement',
      ChannelType.forum => 'forum',
      _ => 'text',
    };
    final destination = channel == null
        ? '$id@$domain'
        : '#${channel.name ?? 'channel'} · $type channel';
    if (webhook['type'] != 2) return destination;
    final sourceGuild = webhook['source_guild'];
    final sourceChannel = webhook['source_channel'];
    final sourceGuildName = sourceGuild is Map
        ? '${sourceGuild['name'] ?? 'Announcement source'}'
        : '${webhook['name'] ?? 'Announcement source'}';
    final sourceChannelName = sourceChannel is Map
        ? '#${sourceChannel['name'] ?? 'announcement'}'
        : 'announcement channel';
    return 'Following $sourceGuildName · $sourceChannelName into $destination';
  }

  EntityRef _webhookChannelRef(Map<String, Object?> webhook) => EntityRef(
        Snowflake('${webhook['channel_id']}'),
        Domain(
          '${webhook['channel_domain'] ?? widget.guild.ref.domain.value}',
        ),
      );

  EntityRef _webhookRef(Map<String, Object?> webhook) {
    final qualified = '${webhook['ref'] ?? ''}'.trim();
    if (qualified.isNotEmpty) return EntityRef.parse(qualified);
    return EntityRef(
      Snowflake('${webhook['id']}'),
      Domain('${webhook['guild_domain'] ?? widget.guild.ref.domain}'),
    );
  }

  Future<void> _handleAction(
    Map<String, Object?> item,
    String action,
  ) async {
    final id = '${item['id']}';
    final webhookRef = _webhookRef(item);
    try {
      switch (action) {
        case 'copy-url':
          final url = '${item['execution_url'] ?? ''}';
          if (url.isEmpty) return;
          await Clipboard.setData(ClipboardData(text: url));
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(
                  content:
                      Text(L10n.of(context).ui_webhook_url_copied_62a2ee15)),
            );
          }
          return;
        case 'edit':
          final channels = widget.managedChannels;
          if (channels.isEmpty) {
            throw UserInputException(
              'Create a text, announcement, or forum channel before moving this webhook.',
            );
          }
          final draft = await showWebhookSettingsEditor(
            context,
            webhook: item,
            channels: channels,
            fallbackDomain: widget.guild.ref.domain,
          );
          if (draft == null || !mounted) return;
          setState(() => _busyWebhook = id);
          await widget.repository
              .updateWebhook(widget.guild.ref, webhookRef, <String, Object?>{
            'name': draft.name,
            'channel_id': draft.channel.wire,
          });
          break;
        case 'avatar':
          await _replaceAvatar(item);
          return;
        case 'clear-avatar':
          if (!await _confirm(
            context,
            'Remove ${item['name'] ?? 'webhook'}’s avatar?',
            'Future messages use the default webhook icon. Existing webhook messages are updated to keep their author image consistent.',
            destructive: true,
          )) {
            return;
          }
          setState(() => _busyWebhook = id);
          await widget.repository
              .clearWebhookAvatar(widget.guild.ref, webhookRef);
          break;
        case 'rotate':
          setState(() => _busyWebhook = id);
          final rotated = await widget.repository
              .rotateWebhook(widget.guild.ref, webhookRef);
          if (mounted) {
            await showDialog<void>(
              context: context,
              builder: (dialogContext) => AlertDialog(
                title: Text(L10n.of(context).ui_new_webhook_url_5a8adb85),
                content: SelectableText(
                  L10n.of(context).ui_value0_26e9163c(
                      (rotated['execution_url'] ?? 'Webhook URL unavailable.')
                          .toString()),
                ),
                actions: [
                  if (rotated['execution_url'] is String)
                    TextButton(
                      onPressed: () async {
                        await Clipboard.setData(
                            ClipboardData(text: '${rotated['execution_url']}'));
                        if (dialogContext.mounted) {
                          Navigator.pop(dialogContext);
                        }
                      },
                      child: Text(L10n.of(context).ui_copy_url_0c62d7e5),
                    ),
                  TextButton(
                    onPressed: () => Navigator.pop(dialogContext),
                    child: Text(L10n.of(context).ui_done_8dd31791),
                  ),
                ],
              ),
            );
          }
          break;
        case 'delete':
          final channelFollower = item['type'] == 2;
          if (!await _confirm(
            context,
            channelFollower
                ? 'Stop following ${item['name'] ?? 'this announcement channel'}?'
                : 'Delete ${item['name'] ?? 'webhook'}?',
            channelFollower
                ? 'New published posts will no longer be delivered here. Existing posts remain.'
                : 'The token stops working immediately. Messages already posted by this webhook remain.',
            destructive: true,
          )) {
            return;
          }
          setState(() => _busyWebhook = id);
          await widget.repository.deleteWebhook(widget.guild.ref, webhookRef);
          break;
        default:
          return;
      }
      await _load();
    } on Object catch (error) {
      if (mounted) {
        _tabError(context, 'Could not update webhook', error);
      }
    } finally {
      if (mounted) {
        setState(() {
          _busyWebhook = null;
          _uploadProgress = 0;
        });
      }
    }
  }

  Future<void> _replaceAvatar(Map<String, Object?> webhook) async {
    final selected = await ImagePicker().pickImage(source: ImageSource.gallery);
    if (selected == null || !mounted) return;
    final contentType = imageUploadContentType(
      selected.name,
      reportedType: selected.mimeType,
    );
    if (contentType == null) {
      throw UserInputException(
        'Choose a PNG, JPEG, GIF, or WebP webhook avatar.',
      );
    }
    final file = File(selected.path);
    final accepted = await showWebhookAvatarPreviewConfirmation(
      context,
      file: file,
      webhookName: '${webhook['name'] ?? 'Webhook'}',
      replacing: webhook['avatar_hash'] != null,
    );
    if (!accepted || !mounted) return;
    final id = '${webhook['id']}';
    final webhookRef = _webhookRef(webhook);
    setState(() {
      _busyWebhook = id;
      _uploadProgress = 0;
    });
    await widget.repository.uploadWebhookAvatar(
      guild: widget.guild.ref,
      webhook: webhookRef,
      filename: selected.name,
      contentType: contentType,
      file: file,
      onProgress: (sent, total) {
        if (!mounted || total < 1) return;
        setState(
            () => _uploadProgress = (sent * 100 / total).round().clamp(0, 100));
      },
    );
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
          content: Text(L10n.of(context).ui_webhook_avatar_updated_a553b36e)),
    );
    await _load();
  }

  Future<void> _create() async {
    final name = await _prompt(context, 'Create webhook', 'Webhook name');
    if (name == null || !mounted) return;
    final channels = widget.managedChannels;
    if (channels.isEmpty) {
      _tabError(
        context,
        'Could not create the webhook',
        UserInputException(
          'Create a text, announcement, or forum channel before creating a webhook.',
        ),
      );
      return;
    }
    final channel = await showGuildChannelPicker(
      context,
      channels: channels,
      title: L10n.of(context).ui_post_this_webhook_in_97bf590d,
    );
    if (channel == null || !mounted) return;
    try {
      final created = await widget.repository
          .createWebhook(widget.guild.ref, channel.ref, name);
      if (mounted) {
        await showDialog<void>(
            context: context,
            builder: (dialogContext) => AlertDialog(
                    title: Text(L10n.of(context).ui_webhook_created_1bca0506),
                    content: SelectableText(L10n.of(context).ui_value0_26e9163c(
                        (created['execution_url'] ??
                                'Webhook URL is available only now.')
                            .toString())),
                    actions: [
                      if (created['execution_url'] is String)
                        TextButton(
                            onPressed: () async {
                              await Clipboard.setData(ClipboardData(
                                  text: '${created['execution_url']}'));
                              if (dialogContext.mounted) {
                                Navigator.pop(dialogContext);
                              }
                            },
                            child: Text(L10n.of(context).ui_copy_url_0c62d7e5)),
                      TextButton(
                          onPressed: () => Navigator.pop(dialogContext),
                          child: Text(L10n.of(context).ui_done_8dd31791))
                    ]));
      }
      await _load();
    } on Object catch (error) {
      if (mounted) _tabError(context, 'Could not create webhook', error);
    }
  }
}

typedef WebhookSettingsDraft = ({String name, EntityRef channel});

Future<WebhookSettingsDraft?> showWebhookSettingsEditor(
  BuildContext context, {
  required Map<String, Object?> webhook,
  required List<KaedeChannel> channels,
  required Domain fallbackDomain,
}) {
  final currentId = '${webhook['channel_id'] ?? ''}';
  final currentDomain =
      Domain('${webhook['channel_domain'] ?? fallbackDomain.value}');
  final current = channels
          .where((channel) =>
              channel.ref.id.value == currentId &&
              channel.ref.domain == currentDomain)
          .firstOrNull ??
      channels.first;
  return showDialog<WebhookSettingsDraft>(
    context: context,
    builder: (_) => _WebhookSettingsEditorDialog(
      initialName: '${webhook['name'] ?? 'Webhook'}',
      initialChannel: current,
      channels: channels,
    ),
  );
}

Future<bool> showWebhookAvatarPreviewConfirmation(
  BuildContext context, {
  required File file,
  required String webhookName,
  required bool replacing,
}) async =>
    await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(replacing
            ? L10n.of(context).ui_replace_webhook_avatar_4c569128
            : L10n.of(context).ui_use_this_avatar_048a68fa),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(48),
              child: Image.file(
                file,
                key: Key('webhook-avatar-preview'),
                width: 96,
                height: 96,
                fit: BoxFit.cover,
                errorBuilder: (_, __, ___) => SizedBox.square(
                  dimension: 96,
                  child: ColoredBox(
                    color: context.kaede.raised,
                    child: Icon(Icons.broken_image_outlined),
                  ),
                ),
              ),
            ),
            SizedBox(height: 12),
            Text(
              replacing
                  ? L10n.of(context)
                      .ui_value0_s_existing_avatar_and_historical_webho_c17038d2(
                          (webhookName).toString())
                  : L10n.of(context)
                      .ui_value0_will_use_this_image_after_it_passes_sa_39eda3a5(
                          (webhookName).toString()),
              textAlign: TextAlign.center,
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            key: Key('confirm-webhook-avatar'),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(L10n.of(context).ui_upload_avatar_5c00299d),
          ),
        ],
      ),
    ) ??
    false;

final class _WebhookSettingsEditorDialog extends StatefulWidget {
  const _WebhookSettingsEditorDialog({
    required this.initialName,
    required this.initialChannel,
    required this.channels,
  });

  final String initialName;
  final KaedeChannel initialChannel;
  final List<KaedeChannel> channels;

  @override
  State<_WebhookSettingsEditorDialog> createState() =>
      _WebhookSettingsEditorDialogState();
}

final class _WebhookSettingsEditorDialogState
    extends State<_WebhookSettingsEditorDialog> {
  late final TextEditingController _name;
  late KaedeChannel _channel;
  String? _error;

  @override
  void initState() {
    super.initState();
    _name = TextEditingController(text: widget.initialName);
    _channel = widget.initialChannel;
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  void _save() {
    final cleaned = _name.text.trim();
    if (cleaned.isEmpty) {
      setState(() =>
          _error = L10n.of(context).ui_webhook_names_cannot_be_blank_83327c5e);
      return;
    }
    Navigator.pop(
      context,
      (name: cleaned, channel: _channel.ref),
    );
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(L10n.of(context).ui_edit_webhook_d4d6ac44),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              TextField(
                key: Key('webhook-name-field'),
                controller: _name,
                maxLength: 80,
                decoration: InputDecoration(
                    labelText: L10n.of(context).ui_webhook_name_d1cff1d9),
              ),
              DropdownButtonFormField<KaedeChannel>(
                key: Key('webhook-channel-field'),
                initialValue: _channel,
                decoration: InputDecoration(
                    labelText: L10n.of(context).ui_post_in_fc98179e),
                items: [
                  for (final channel in widget.channels)
                    DropdownMenuItem(
                      value: channel,
                      child: Text(
                        L10n.of(context).ui_value0_value1_947523d9(
                            (channel.type == ChannelType.forum
                                    ? 'Forum'
                                    : channel.type == ChannelType.announcement
                                        ? 'Announcement'
                                        : 'Text')
                                .toString(),
                            (channel.name ?? channel.ref.id.value).toString()),
                      ),
                    ),
                ],
                onChanged: (value) {
                  if (value != null) setState(() => _channel = value);
                },
              ),
              if (_error case final message?) ...[
                SizedBox(height: 8),
                Text(message, style: TextStyle(color: context.kaede.danger)),
              ],
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            key: Key('save-webhook-settings'),
            onPressed: _save,
            child: Text(L10n.of(context).ui_save_webhook_f889ec79),
          ),
        ],
      );
}

final class _WebhookAvatar extends StatelessWidget {
  const _WebhookAvatar({required this.avatarHash, required this.domain});

  final String? avatarHash;
  final Domain domain;

  @override
  Widget build(BuildContext context) {
    final uri = publicAssetUri(
      domain,
      avatarHash,
      variant: 'thumbnail_128',
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(18),
      child: SizedBox.square(
        dimension: 36,
        child: uri == null
            ? ColoredBox(
                color: context.kaede.raised,
                child: Icon(Icons.webhook_rounded,
                    size: 20, color: context.kaede.muted),
              )
            : CachedNetworkImage(
                imageUrl: '$uri',
                fit: BoxFit.cover,
                errorWidget: (_, __, ___) => ColoredBox(
                  color: context.kaede.raised,
                  child: Icon(Icons.webhook_rounded,
                      size: 20, color: context.kaede.muted),
                ),
              ),
      ),
    );
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
  final Set<String> _knownActorKeys = <String>{};
  final Map<EntityRef, KaedeUser> _resolvedActors = <EntityRef, KaedeUser>{};
  var _loading = true;
  var _refreshing = false;
  var _loadingOlder = false;
  var _hasMore = false;
  var _requestGeneration = 0;
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
    final changedGuild = oldWidget.guild.ref != widget.guild.ref;
    final shouldReload = changedGuild ||
        oldWidget.guild.version != widget.guild.version ||
        oldWidget.canView != widget.canView;
    if (shouldReload) {
      _requestGeneration += 1;
      setState(() {
        _loading = widget.canView;
        _refreshing = false;
        _loadingOlder = false;
        _hasMore = false;
        _error = null;
        if (changedGuild) {
          _items = const [];
          _members = const [];
          _knownActorKeys.clear();
          _resolvedActors.clear();
          _actorFilter = null;
          _actionFilter = null;
        }
      });
      if (widget.canView) unawaited(_load());
    }
  }

  Future<void> _load({bool refresh = false}) async {
    if (!widget.canView) {
      if (mounted) setState(() => _loading = false);
      return;
    }
    final generation = ++_requestGeneration;
    final actorFilter = _parseAuditActorFilter(_actorFilter);
    final actionFilter = parseGuildAuditActionFilter(_actionFilter);
    if (refresh && mounted) {
      setState(() {
        _error = null;
        _refreshing = true;
        _loadingOlder = false;
      });
    }
    try {
      final items = await widget.repository.auditLog(
        widget.guild.ref,
        userId: actorFilter,
        actionType: actionFilter?.actionType,
        targetType: actionFilter?.targetType,
      );
      var members = _members;
      try {
        members = await widget.repository.members(widget.guild.ref);
      } on Object {
        // Actor IDs and targets still render if roster resolution is denied.
      }
      if (mounted && generation == _requestGeneration) {
        setState(() {
          _items = items;
          _members = members;
          _rememberAuditActors(items, members);
          _hasMore = items.length == 50;
          _loading = false;
          _refreshing = false;
          _loadingOlder = false;
          _error = null;
        });
      }
    } on Object catch (error) {
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _error = userFacingError(
          error,
          summary: L10n.of(context).ui_could_not_load_the_audit_log_e027207f,
        );
        _loading = false;
        _refreshing = false;
      });
    }
  }

  Future<void> _loadOlder() async {
    if (_loadingOlder || !_hasMore || _items.isEmpty) return;
    final generation = _requestGeneration;
    final actorFilter = _parseAuditActorFilter(_actorFilter);
    final actionFilter = parseGuildAuditActionFilter(_actionFilter);
    setState(() => _loadingOlder = true);
    try {
      final older = await widget.repository.auditLog(
        widget.guild.ref,
        before: '${_items.last['id']}',
        userId: actorFilter,
        actionType: actionFilter?.actionType,
        targetType: actionFilter?.targetType,
      );
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        final known = _items.map((item) => '${item['id']}').toSet();
        _items = [
          ..._items,
          ...older.where((item) => !known.contains('${item['id']}')),
        ];
        _rememberAuditActors(older, _members);
        _hasMore = older.length == 50;
        _loadingOlder = false;
      });
    } on Object catch (error) {
      if (!mounted || generation != _requestGeneration) return;
      _tabError(context, 'Could not load older audit events', error);
      setState(() => _loadingOlder = false);
    }
  }

  Map<EntityRef, KaedeUser> get _users => <EntityRef, KaedeUser>{
        ...widget.userProfiles,
        for (final member in _members) member.user.ref: member.user,
        ..._resolvedActors,
      };

  void _rememberAuditActors(
    Iterable<Map<String, Object?>> items,
    Iterable<GuildMember> members,
  ) {
    _knownActorKeys.addAll(members.map((member) => member.user.ref.wire));
    _knownActorKeys.addAll(items
        .map((item) => guildAuditActorKey(item, widget.guild.ref.domain))
        .whereType<String>());
    if (_actorFilter case final selected?) _knownActorKeys.add(selected);
  }

  Future<void> _setActorFilter(String? value) async {
    if (value == _actorFilter) return;
    setState(() {
      _actorFilter = value;
      _items = const [];
      _hasMore = false;
      _loading = true;
      _refreshing = false;
      _loadingOlder = false;
      _error = null;
    });
    await _load();
  }

  Future<void> _pickActorFilter() async {
    final selected = await showModalBottomSheet<_AuditActorSelection>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) => _AuditActorPicker(
        guild: widget.guild,
        repository: widget.repository,
        initialMembers: _members,
        knownActorKeys: Set<String>.of(_knownActorKeys),
        knownUsers: _users,
        selectedKey: _actorFilter,
      ),
    );
    if (selected == null || !mounted) return;
    final user = selected.user;
    if (user != null) _resolvedActors[user.ref] = user;
    await _setActorFilter(selected.ref?.wire);
  }

  Future<void> _setActionFilter(String? value) async {
    if (value == _actionFilter) return;
    setState(() {
      _actionFilter = value;
      _items = const [];
      _hasMore = false;
      _loading = true;
      _refreshing = false;
      _loadingOlder = false;
      _error = null;
    });
    await _load();
  }

  Future<void> _clearFilters() async {
    if (_actorFilter == null && _actionFilter == null) return;
    setState(() {
      _actorFilter = null;
      _actionFilter = null;
      _items = const [];
      _hasMore = false;
      _loading = true;
      _refreshing = false;
      _loadingOlder = false;
      _error = null;
    });
    await _load();
  }

  @override
  Widget build(BuildContext context) => !widget.canView
      ? ColoredBox(
          color: settingsSurface(context),
          child: Center(
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.lock_outline_rounded,
                    size: 19, color: context.kaede.muted),
                SizedBox(width: 11),
                Text(L10n.of(context).ui_view_audit_log_is_required_c3a0fa90,
                    style:
                        TextStyle(color: context.kaede.muted, fontSize: 13.5)),
              ],
            ),
          ),
        )
      : _loading
          ? Center(child: CircularProgressIndicator())
          : ColoredBox(
              color: settingsSurface(context),
              child: RefreshIndicator(
                onRefresh: () => _load(refresh: true),
                child: _buildList(),
              ),
            );

  Widget _buildList() {
    final users = _users;
    final actions = <String, String>{
      ...guildAuditActionFilterOptions,
      for (final item in _items)
        guildAuditActionFilterKey(item): guildAuditActionLabel(item),
    };
    if (_actionFilter case final selected?) {
      actions.putIfAbsent(
          selected, () => guildAuditActionFilterLabel(selected));
    }
    final actionKeys = actions.keys.toList()
      ..sort((left, right) => actions[left]!.compareTo(actions[right]!));

    return ListView(
      physics: AlwaysScrollableScrollPhysics(),
      padding: EdgeInsets.fromLTRB(14, 12, 14, 32),
      children: [
        SettingsSectionHeader(
          L10n.of(context).ui_audit_log_cb83f9ca,
          top: 0,
          subheading: L10n.of(context)
              .ui_review_moderation_and_configuration_changes_m_e46d550e,
        ),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              _AuditActorFilterButton(
                icon: Icons.person_outline_rounded,
                label: _actorFilter == null
                    ? L10n.of(context).ui_all_members_15595a57
                    : guildAuditActorNameFromKey(_actorFilter!, users),
                onPressed: _pickActorFilter,
              ),
              SizedBox(width: 8),
              _AuditFilterButton<String>(
                icon: Icons.tune_rounded,
                label: _actionFilter == null
                    ? L10n.of(context).ui_all_actions_4ff15157
                    : actions[_actionFilter] ?? 'All actions',
                value: _actionFilter,
                allLabel: 'All actions',
                values: actionKeys,
                itemLabel: (value) => actions[value]!,
                changed: (value) => unawaited(_setActionFilter(value)),
              ),
              if (_actorFilter != null || _actionFilter != null) ...[
                SizedBox(width: 4),
                TextButton(
                  onPressed: () => unawaited(_clearFilters()),
                  child: Text(L10n.of(context).ui_clear_04a57fc2),
                ),
              ],
              SizedBox(width: 4),
              IconButton(
                tooltip: L10n.of(context).ui_refresh_audit_log_8ef0543d,
                onPressed: _loading || _refreshing || _loadingOlder
                    ? null
                    : () => unawaited(_load(refresh: true)),
                icon: _refreshing
                    ? SizedBox.square(
                        dimension: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : Icon(Icons.refresh_rounded),
              ),
            ],
          ),
        ),
        SizedBox(height: 14),
        if (_error case final message?) ...[
          _AuditInlineError(
            message: message,
            retry: () => _load(refresh: true),
          ),
          SizedBox(height: 10),
        ],
        if (_items.isEmpty && _error == null)
          _actorFilter == null && _actionFilter == null
              ? _AuditEmptyState(
                  title: L10n.of(context).ui_no_audit_events_yet_e67a2180,
                  message: L10n.of(context)
                      .ui_administrative_actions_will_show_up_here_fc7a8e6e,
                )
              : _AuditEmptyState(
                  title: L10n.of(context).ui_no_matching_events_192c7226,
                  message: L10n.of(context)
                      .ui_try_another_member_or_action_filter_24ef4c64,
                  action: TextButton(
                    onPressed: () => unawaited(_clearFilters()),
                    child: Text(L10n.of(context).ui_clear_filters_240d53c9),
                  ),
                )
        else
          for (final item in _items)
            _AuditEventCard(
              item: item,
              guild: widget.guild,
              users: users,
            ),
        if (_hasMore && _items.isNotEmpty) ...[
          SizedBox(height: 8),
          Center(
            child: OutlinedButton.icon(
              onPressed: _loadingOlder ? null : _loadOlder,
              icon: _loadingOlder
                  ? SizedBox.square(
                      dimension: 15,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Icon(Icons.expand_more_rounded),
              label: Text(_loadingOlder
                  ? L10n.of(context).ui_loading_c4e2f181
                  : L10n.of(context).ui_load_older_events_0c5a4a98),
            ),
          ),
        ],
      ],
    );
  }
}

final class _AuditActorSelection {
  const _AuditActorSelection(this.ref, {this.user});

  const _AuditActorSelection.all()
      : ref = null,
        user = null;

  final EntityRef? ref;
  final KaedeUser? user;
}

final class _AuditActorFilterButton extends StatelessWidget {
  const _AuditActorFilterButton({
    required this.icon,
    required this.label,
    required this.onPressed,
  });

  final IconData icon;
  final String label;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => Tooltip(
        message: L10n.of(context).ui_filter_audit_log_by_actor_de3dff5a,
        child: Semantics(
          button: true,
          label: L10n.of(context).ui_filter_audit_log_by_actor_de3dff5a,
          value: label,
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              key: ValueKey('audit-actor-filter'),
              borderRadius: BorderRadius.circular(KaedeRadius.small),
              onTap: onPressed,
              child: Container(
                constraints: BoxConstraints(maxWidth: 210, minHeight: 44),
                padding: EdgeInsets.symmetric(horizontal: 11, vertical: 8),
                decoration: BoxDecoration(
                  color: context.kaede.raised,
                  borderRadius: BorderRadius.circular(KaedeRadius.small),
                  border: Border.all(color: context.kaede.border),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(icon, size: 16, color: context.kaede.textSoft),
                    SizedBox(width: 7),
                    Flexible(
                      child: Text(
                        label,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: context.kaede.textSoft,
                          fontSize: 12.5,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    SizedBox(width: 4),
                    Icon(
                      Icons.arrow_drop_down_rounded,
                      size: 18,
                      color: context.kaede.muted,
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
}

final class _AuditActorPicker extends StatefulWidget {
  const _AuditActorPicker({
    required this.guild,
    required this.repository,
    required this.initialMembers,
    required this.knownActorKeys,
    required this.knownUsers,
    required this.selectedKey,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final List<GuildMember> initialMembers;
  final Set<String> knownActorKeys;
  final Map<EntityRef, KaedeUser> knownUsers;
  final String? selectedKey;

  @override
  State<_AuditActorPicker> createState() => _AuditActorPickerState();
}

final class _AuditActorCandidate {
  const _AuditActorCandidate(this.ref, this.user);

  final EntityRef ref;
  final KaedeUser? user;
}

final class _AuditActorPickerState extends State<_AuditActorPicker> {
  final _query = TextEditingController();
  final _scroll = ScrollController();
  final Map<EntityRef, KaedeUser?> _knownActors = <EntityRef, KaedeUser?>{};
  Timer? _debounce;
  late List<GuildMember> _remoteMembers;
  var _requestGeneration = 0;
  var _loading = false;
  var _loadingMore = false;
  var _hasMore = false;
  var _retryLoadsMore = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _remoteMembers = List<GuildMember>.of(widget.initialMembers);
    _hasMore = _remoteMembers.length == 100;
    for (final key in <String>{
      ...widget.knownActorKeys,
      if (widget.selectedKey case final selected?) selected,
    }) {
      try {
        final ref = EntityRef.parse(key);
        _knownActors[ref] = widget.knownUsers[ref];
      } on FormatException {
        // Audit payload parsing is already fail-closed; ignore stale UI keys.
      }
    }
    for (final member in widget.initialMembers) {
      _knownActors[member.user.ref] = member.user;
    }
    _scroll.addListener(_maybeLoadMore);
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _query.dispose();
    _scroll
      ..removeListener(_maybeLoadMore)
      ..dispose();
    super.dispose();
  }

  EntityRef? get _canonicalTypedRef {
    final value = _query.text.trim();
    if (!value.contains('@')) return null;
    try {
      final ref = EntityRef.parse(value);
      return ref.wire == value ? ref : null;
    } on FormatException {
      return null;
    }
  }

  bool get _looksLikeReference => _query.text.trim().contains('@');

  void _queryChanged(String _) {
    _debounce?.cancel();
    final generation = ++_requestGeneration;
    final value = _query.text.trim();
    setState(() {
      _error = null;
      _retryLoadsMore = false;
      if (value.isEmpty) {
        _remoteMembers = List<GuildMember>.of(widget.initialMembers);
        _hasMore = _remoteMembers.length == 100;
        _loading = false;
        _loadingMore = false;
      }
    });
    if (value.isEmpty) return;
    _debounce = Timer(
      Duration(milliseconds: 300),
      () => _search(value, generation),
    );
  }

  Future<void> _search(String value, int generation) async {
    if (!mounted || generation != _requestGeneration) return;
    setState(() {
      _loading = true;
      _loadingMore = false;
      _error = null;
    });
    try {
      final members = await widget.repository.members(
        widget.guild.ref,
        query: value,
      );
      if (!mounted ||
          generation != _requestGeneration ||
          value != _query.text.trim()) {
        return;
      }
      setState(() {
        _remoteMembers = members;
        _hasMore = members.length == 100;
        for (final member in members) {
          _knownActors[member.user.ref] = member.user;
        }
      });
    } on Object catch (error) {
      if (!mounted ||
          generation != _requestGeneration ||
          value != _query.text.trim()) {
        return;
      }
      setState(() {
        _error = userFacingError(
          error,
          summary: L10n.of(context).ui_could_not_search_guild_members_67e5f469,
        );
        _retryLoadsMore = false;
      });
    } finally {
      if (mounted &&
          generation == _requestGeneration &&
          value == _query.text.trim()) {
        setState(() => _loading = false);
      }
    }
  }

  void _maybeLoadMore() {
    if (_scroll.hasClients &&
        _scroll.position.extentAfter < 320 &&
        _hasMore &&
        !_loading &&
        !_loadingMore) {
      unawaited(_loadMore());
    }
  }

  Future<void> _loadMore() async {
    if (_remoteMembers.isEmpty || !_hasMore || _loading || _loadingMore) {
      return;
    }
    final generation = _requestGeneration;
    final value = _query.text.trim();
    setState(() {
      _loadingMore = true;
      _error = null;
      _retryLoadsMore = false;
    });
    try {
      final page = await widget.repository.members(
        widget.guild.ref,
        query: value.isEmpty ? null : value,
        after: _remoteMembers.last.user.ref,
      );
      if (!mounted ||
          generation != _requestGeneration ||
          value != _query.text.trim()) {
        return;
      }
      final known = _remoteMembers.map((member) => member.user.ref).toSet();
      setState(() {
        _remoteMembers = <GuildMember>[
          ..._remoteMembers,
          ...page.where((member) => known.add(member.user.ref)),
        ];
        _hasMore = page.length == 100;
        for (final member in page) {
          _knownActors[member.user.ref] = member.user;
        }
      });
    } on Object catch (error) {
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _error = userFacingError(
          error,
          summary:
              L10n.of(context).ui_could_not_load_more_guild_members_f1b1067c,
        );
        _retryLoadsMore = true;
      });
    } finally {
      if (mounted && generation == _requestGeneration) {
        setState(() => _loadingMore = false);
      }
    }
  }

  List<_AuditActorCandidate> get _visibleActors {
    final value = _query.text.trim().toLowerCase();
    final selected = widget.selectedKey;
    final merged = <EntityRef, _AuditActorCandidate>{};
    bool matches(EntityRef ref, KaedeUser? user) =>
        value.isEmpty ||
        ref.wire.toLowerCase().contains(value) ||
        (user?.name.toLowerCase().contains(value) ?? false) ||
        (user?.handle.toLowerCase().contains(value) ?? false);

    for (final entry in _knownActors.entries) {
      if (entry.key.wire == selected || matches(entry.key, entry.value)) {
        merged[entry.key] = _AuditActorCandidate(entry.key, entry.value);
      }
    }
    for (final member in _remoteMembers) {
      merged[member.user.ref] =
          _AuditActorCandidate(member.user.ref, member.user);
    }
    if (_canonicalTypedRef case final exact?) {
      merged.putIfAbsent(
        exact,
        () => _AuditActorCandidate(exact, widget.knownUsers[exact]),
      );
    }
    final actors = merged.values.toList()
      ..sort((left, right) {
        if (left.ref.wire == selected) return -1;
        if (right.ref.wire == selected) return 1;
        final leftName = left.user?.name ?? left.ref.wire;
        final rightName = right.user?.name ?? right.ref.wire;
        return leftName.toLowerCase().compareTo(rightName.toLowerCase());
      });
    return actors;
  }

  void _select(_AuditActorCandidate actor) => Navigator.pop(
        context,
        _AuditActorSelection(actor.ref, user: actor.user),
      );

  @override
  Widget build(BuildContext context) {
    final actors = _visibleActors;
    final exact = _canonicalTypedRef;
    final exactIsDeparted = exact != null &&
        !_knownActors.containsKey(exact) &&
        !_remoteMembers.any((member) => member.user.ref == exact);
    return FractionallySizedBox(
      heightFactor: .86,
      child: Column(
        children: [
          Padding(
            padding: EdgeInsets.fromLTRB(16, 0, 16, 10),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  L10n.of(context).ui_filter_by_audit_actor_98b183de,
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                SizedBox(height: 6),
                Text(
                  L10n.of(context)
                      .ui_search_the_full_guild_roster_or_enter_an_exac_5b817a46,
                  style: TextStyle(color: context.kaede.muted),
                ),
                SizedBox(height: 12),
                SearchBar(
                  key: ValueKey('audit-actor-query'),
                  controller: _query,
                  autoFocus: true,
                  hintText: L10n.of(context)
                      .ui_name_handle_or_123_chat_example_54cb5e3d,
                  leading: Icon(Icons.search_rounded),
                  onChanged: _queryChanged,
                ),
                if (_looksLikeReference && exact == null)
                  Semantics(
                    liveRegion: true,
                    child: Padding(
                      padding: EdgeInsets.fromLTRB(4, 8, 4, 0),
                      child: Text(
                        L10n.of(context)
                            .ui_enter_a_canonical_user_id_like_123_chat_examp_f80e9a27,
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                          fontSize: 12,
                        ),
                      ),
                    ),
                  ),
                if (_error case final error?)
                  Semantics(
                    liveRegion: true,
                    child: Padding(
                      padding: EdgeInsets.only(top: 8),
                      child: Row(
                        children: [
                          Expanded(
                            child: Text(
                              error,
                              style: TextStyle(
                                color: Theme.of(context).colorScheme.error,
                                fontSize: 12.5,
                              ),
                            ),
                          ),
                          TextButton(
                            onPressed: _loading || _loadingMore
                                ? null
                                : _retryLoadsMore
                                    ? _loadMore
                                    : () {
                                        final value = _query.text.trim();
                                        if (value.isNotEmpty) {
                                          final generation =
                                              ++_requestGeneration;
                                          unawaited(_search(value, generation));
                                        }
                                      },
                            child: Text(L10n.of(context).ui_retry_8036af59),
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          ),
          Divider(height: 1),
          Expanded(
            child: ListView(
              key: ValueKey('audit-actor-picker'),
              controller: _scroll,
              keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
              children: [
                ListTile(
                  key: ValueKey('audit-actor-all'),
                  leading: Icon(Icons.groups_outlined),
                  title: Text(L10n.of(context).ui_all_members_15595a57),
                  subtitle:
                      Text(L10n.of(context).ui_do_not_filter_by_actor_e7d232fb),
                  trailing: widget.selectedKey == null
                      ? Icon(Icons.check_rounded)
                      : null,
                  onTap: () => Navigator.pop(
                    context,
                    const _AuditActorSelection.all(),
                  ),
                ),
                Divider(height: 1),
                if (actors.isEmpty && _loading)
                  Padding(
                    padding: EdgeInsets.all(28),
                    child: Center(child: CircularProgressIndicator()),
                  )
                else if (actors.isEmpty)
                  Padding(
                    padding: EdgeInsets.all(28),
                    child: Center(
                        child: Text(L10n.of(context)
                            .ui_no_matching_guild_members_962648f1)),
                  )
                else
                  for (final actor in actors)
                    ListTile(
                      key: ValueKey('audit-actor-${actor.ref.wire}'),
                      leading: actor.user == null
                          ? CircleAvatar(
                              child: Icon(Icons.person_off_outlined),
                            )
                          : UserAvatar(user: actor.user!, radius: 20),
                      title: Text(
                        actor.user?.name ??
                            (exactIsDeparted && actor.ref == exact
                                ? L10n.of(context).ui_use_value0_a62295a9(
                                    (actor.ref.wire).toString())
                                : L10n.of(context).ui_value0_1b34e556(
                                    (actor.ref.id.value).toString())),
                      ),
                      subtitle: Text(
                        actor.user == null
                            ? exactIsDeparted && actor.ref == exact
                                ? L10n.of(context)
                                    .ui_filter_a_departed_actor_by_exact_canonical_id_7a8774d3
                                : L10n.of(context)
                                    .ui_value0_known_audit_actor_e242117d(
                                        (actor.ref.wire).toString())
                            : L10n.of(context).ui_value0_value1_d927892c(
                                (actor.user!.handle).toString(),
                                (actor.ref.wire).toString()),
                      ),
                      trailing: actor.ref.wire == widget.selectedKey
                          ? Icon(Icons.check_rounded)
                          : exactIsDeparted && actor.ref == exact
                              ? Chip(
                                  label: Text(
                                      L10n.of(context).ui_exact_id_1c3e7ca1))
                              : null,
                      onTap: () => _select(actor),
                    ),
                if (_loading && actors.isNotEmpty)
                  Padding(
                    padding: EdgeInsets.all(12),
                    child: Center(child: CircularProgressIndicator()),
                  ),
                if (_hasMore)
                  Padding(
                    padding: EdgeInsets.fromLTRB(16, 8, 16, 16),
                    child: OutlinedButton.icon(
                      key: ValueKey('audit-actor-load-more'),
                      onPressed: _loadingMore ? null : _loadMore,
                      icon: _loadingMore
                          ? SizedBox.square(
                              dimension: 15,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : Icon(Icons.expand_more_rounded),
                      label: Text(
                        _loadingMore
                            ? L10n.of(context).ui_loading_c4e2f181
                            : L10n.of(context).ui_load_more_members_01d2c605,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
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
          constraints: BoxConstraints(maxWidth: 210),
          padding: EdgeInsets.symmetric(horizontal: 11, vertical: 8),
          decoration: BoxDecoration(
            color: context.kaede.raised,
            borderRadius: BorderRadius.circular(KaedeRadius.small),
            border: Border.all(color: context.kaede.border),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 16, color: context.kaede.textSoft),
              SizedBox(width: 7),
              Flexible(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: context.kaede.textSoft,
                    fontSize: 12.5,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              SizedBox(width: 4),
              Icon(Icons.arrow_drop_down_rounded,
                  size: 18, color: context.kaede.muted),
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
      padding: EdgeInsets.only(bottom: 8),
      child: Material(
        color: context.kaede.panel,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        clipBehavior: Clip.antiAlias,
        child: ExpansionTile(
          tilePadding: EdgeInsets.fromLTRB(12, 8, 8, 8),
          childrenPadding: EdgeInsets.fromLTRB(14, 0, 14, 14),
          shape: Border(),
          collapsedShape: Border(),
          leading: actor == null
              ? CircleAvatar(
                  radius: 18,
                  backgroundColor: guildAuditActionColor(context, item),
                  child: Icon(guildAuditActionIcon(item),
                      size: 18, color: context.kaede.text),
                )
              : UserAvatar(user: actor, radius: 18),
          title: Text(
            summary,
            style: TextStyle(
              fontSize: 14,
              height: 1.3,
              fontWeight: FontWeight.w600,
            ),
          ),
          subtitle: Padding(
            padding: EdgeInsets.only(top: 3),
            child: Text(
              metadata,
              style: TextStyle(
                color: context.kaede.muted,
                fontSize: 11.5,
              ),
            ),
          ),
          children: [
            Divider(height: 1),
            SizedBox(height: 12),
            if (reason.isNotEmpty) ...[
              _AuditDetailLabel(
                  label: L10n.of(context).ui_reason_41314139, value: reason),
              SizedBox(height: 10),
            ],
            _AuditDetailLabel(
              label: L10n.of(context).ui_target_8b67f168,
              value: guildAuditTargetDetail(item, guild, users),
            ),
            if (createdAt != null) ...[
              SizedBox(height: 10),
              _AuditDetailLabel(
                label: L10n.of(context).ui_when_021e3a39,
                value: DateFormat('MMM d, y • h:mm:ss a').format(createdAt),
              ),
            ],
            if (reason.isEmpty) ...[
              SizedBox(height: 10),
              _AuditDetailLabel(
                  label: L10n.of(context).ui_reason_41314139,
                  value: 'No reason provided'),
            ],
            if (changes.isNotEmpty) ...[
              SizedBox(height: 14),
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  L10n.of(context).ui_changes_1c5e9f2a,
                  style: TextStyle(
                    color: context.kaede.muted,
                    fontSize: 10.5,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1,
                  ),
                ),
              ),
              SizedBox(height: 7),
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
            style: TextStyle(
              color: context.kaede.textSoft,
              fontSize: 12.5,
              height: 1.4,
            ),
            children: [
              TextSpan(
                text: L10n.of(context).ui_value0_7e5b9bdc((label).toString()),
                style: TextStyle(
                  color: context.kaede.muted,
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
      padding: EdgeInsets.only(bottom: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: EdgeInsets.only(top: 5),
            child: Icon(Icons.circle, size: 5, color: context.kaede.coralText),
          ),
          SizedBox(width: 8),
          Expanded(
            child: Text.rich(
              TextSpan(
                style: TextStyle(
                  color: context.kaede.textSoft,
                  fontSize: 12,
                  height: 1.4,
                ),
                children: [
                  TextSpan(
                    text: L10n.of(context).ui_value0_fa24b216((key).toString()),
                    style: TextStyle(fontWeight: FontWeight.w600),
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
        padding: EdgeInsets.symmetric(vertical: 52, horizontal: 20),
        child: Column(
          children: [
            Icon(Icons.manage_search_rounded,
                size: 42, color: context.kaede.muted),
            SizedBox(height: 12),
            Text(title,
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
            SizedBox(height: 5),
            Text(message,
                textAlign: TextAlign.center,
                style: TextStyle(color: context.kaede.muted, fontSize: 13)),
            if (action case final button?) ...[
              SizedBox(height: 8),
              button,
            ],
          ],
        ),
      );
}

final class _AuditInlineError extends StatelessWidget {
  const _AuditInlineError({required this.message, required this.retry});

  final String message;
  final Future<void> Function() retry;

  @override
  Widget build(BuildContext context) => Container(
        padding: EdgeInsets.fromLTRB(12, 9, 8, 9),
        decoration: BoxDecoration(
          color: context.kaede.dangerSoft,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          border:
              Border.all(color: context.kaede.danger.withValues(alpha: 0.3)),
        ),
        child: Row(
          children: [
            Icon(Icons.error_outline_rounded,
                size: 19, color: context.kaede.danger),
            SizedBox(width: 9),
            Expanded(
              child: Text(
                message,
                style: TextStyle(
                  color: context.kaede.textSoft,
                  fontSize: 12.5,
                  height: 1.35,
                ),
              ),
            ),
            TextButton(
              onPressed: () => unawaited(retry()),
              child: Text(L10n.of(context).ui_retry_8036af59),
            ),
          ],
        ),
      );
}

final class _PermissionScreen extends StatefulWidget {
  const _PermissionScreen(
      {required this.guild,
      required this.actorRef,
      required this.actorHighestRole,
      required this.channel,
      required this.heldPermissions,
      required this.members,
      required this.existing,
      required this.repository});
  final KaedeGuild guild;
  final EntityRef? actorRef;
  final KaedeRole? actorHighestRole;
  final KaedeChannel channel;
  final BigInt heldPermissions;
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
    _searchDebounce = Timer(Duration(milliseconds: 250), _findMembers);
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
              summary:
                  L10n.of(context).ui_could_not_search_the_members_7dee0555,
            )),
            backgroundColor: context.kaede.danger,
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
              summary: L10n.of(context).ui_could_not_load_more_members_89005185,
            )),
            backgroundColor: context.kaede.danger,
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
        if ((query.isEmpty || role.name.toLowerCase().contains(query)) &&
            channelOverwriteTargetEligible(
              guild: widget.guild,
              actorRef: widget.actorRef,
              actorHighestRole: widget.actorHighestRole,
              target: role.ref,
              targetType: 'role',
              members: _members,
            ))
          (role.name, role.ref, 'role', null),
      for (final member in _members)
        if (channelOverwriteTargetEligible(
          guild: widget.guild,
          actorRef: widget.actorRef,
          actorHighestRole: widget.actorHighestRole,
          target: member.user.ref,
          targetType: 'member',
          members: _members,
        ))
          (
            member.nickname ?? member.user.name,
            member.user.ref,
            'member',
            member.user
          ),
    ];
    return Scaffold(
      appBar: AppBar(
          title: Text(L10n.of(context).ui_value0_permissions_31cd145b(
              (widget.channel.name).toString()))),
      body: Column(children: [
        if (widget.channel.parentRef != null &&
            !widget.channel.permissionsSynced)
          MaterialBanner(
            content: Text(L10n.of(context)
                .ui_permissions_are_independent_from_the_parent_c_fdf95228),
            actions: [
              TextButton(
                onPressed: _mutating ? null : _sync,
                child: Text(L10n.of(context).ui_sync_with_category_d984adf6),
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
                  constraints: BoxConstraints(maxHeight: 260),
                  child: rail,
                ),
                Divider(height: 1),
                Expanded(child: editor),
              ]);
            }
            return Row(children: [
              SizedBox(width: 300, child: rail),
              VerticalDivider(width: 1),
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
        padding: EdgeInsets.all(12),
        child: SearchBar(
          controller: _search,
          hintText: L10n.of(context).ui_search_roles_or_members_570939c6,
          leading: Icon(Icons.search_rounded),
          trailing: [
            if (_loadingMembers)
              SizedBox.square(
                  dimension: 18, child: CircularProgressIndicator()),
            if (_search.text.isNotEmpty)
              IconButton(
                  onPressed: _search.clear, icon: Icon(Icons.close_rounded)),
          ],
        ),
      ),
      Expanded(
        child: ListView.builder(
          controller: _targetScroll,
          itemCount: targets.length + (_loadingMembers ? 1 : 0),
          itemBuilder: (_, index) {
            if (index == targets.length) {
              return Padding(
                padding: EdgeInsets.all(16),
                child: Center(child: CircularProgressIndicator()),
              );
            }
            final target = targets[index];
            return ListTile(
              selected: _target == target.$2,
              leading: target.$4 == null
                  ? CircleAvatar(
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
      return Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.shield_outlined, size: 36, color: context.kaede.muted),
              SizedBox(height: 10),
              Text(L10n.of(context).ui_choose_a_role_or_member_a7e7d77c,
                  style: TextStyle(fontWeight: FontWeight.w800)),
              SizedBox(height: 4),
              Text(
                  L10n.of(context)
                      .ui_then_set_channel_specific_permissions_fc806672,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: context.kaede.muted)),
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
    final targetEligible = _selectedTargetEligible;
    return ListView(padding: EdgeInsets.all(16), children: [
      Text(L10n.of(context).ui_channel_override_03778c74,
          style: TextStyle(fontSize: 19, fontWeight: FontWeight.w800)),
      SizedBox(height: 4),
      Text(
        L10n.of(context)
            .ui_deny_blocks_the_role_permission_here_inherit__31d3fe51,
        style: TextStyle(color: context.kaede.muted),
      ),
      if (!targetEligible) ...[
        SizedBox(height: 8),
        Text(
          L10n.of(context).ui_you_can_no_longer_manage_this_target_4a555956,
          style: TextStyle(color: context.kaede.danger),
        ),
      ],
      SizedBox(height: 14),
      for (final group in groups)
        Card(
          margin: EdgeInsets.only(bottom: 12),
          clipBehavior: Clip.antiAlias,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: EdgeInsets.fromLTRB(16, 14, 16, 4),
                child:
                    Text(group, style: Theme.of(context).textTheme.titleLarge),
              ),
              for (final permission
                  in relevant.where((item) => item.group == group))
                _PermissionRow(
                    metadata: permission,
                    value: _permissionValue(permission.bit),
                    enabled: channelOverwritePermissionCanChange(
                      widget.heldPermissions,
                      permission.bit,
                    ),
                    changed: (value) =>
                        setState(() => _set(permission.bit, value))),
            ],
          ),
        ),
      SizedBox(height: 4),
      Row(children: [
        Expanded(
          child: FilledButton.icon(
              onPressed: _mutating || !targetEligible ? null : _save,
              icon: Icon(Icons.save_outlined),
              label: Text(_mutating
                  ? L10n.of(context).ui_saving_bd79b37d
                  : L10n.of(context).ui_save_overwrite_739365dd)),
        ),
        if (_hasOverwrite) ...[
          SizedBox(width: 10),
          OutlinedButton.icon(
            onPressed: _mutating ||
                    !targetEligible ||
                    !channelOverwriteCanReset(
                      _allow,
                      _deny,
                      widget.heldPermissions,
                    )
                ? null
                : _delete,
            icon: Icon(Icons.restart_alt_rounded),
            label: Text(L10n.of(context).ui_reset_0ac8a560),
          ),
        ],
      ]),
      SizedBox(height: 12),
    ]);
  }

  void _select(EntityRef target, String type) {
    if (!_targetEligible(target, type)) return;
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
    if (target == null || _mutating || !_targetEligible(target, _type)) return;
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
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(L10n.of(context).ui_permissions_saved_b08c8fd8)));
      }
    } on Object catch (error) {
      if (mounted) _showMutationError('Could not save permissions', error);
    } finally {
      if (mounted) setState(() => _mutating = false);
    }
  }

  Future<void> _delete() async {
    final target = _target;
    if (target == null || _mutating || !_targetEligible(target, _type)) return;
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
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(L10n.of(context).ui_overwrite_reset_bcd25479)));
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
        backgroundColor: context.kaede.danger,
      ),
    );
  }

  bool get _selectedTargetEligible {
    final target = _target;
    return target != null && _targetEligible(target, _type);
  }

  bool _targetEligible(EntityRef target, String type) =>
      channelOverwriteTargetEligible(
        guild: widget.guild,
        actorRef: widget.actorRef,
        actorHighestRole: widget.actorHighestRole,
        target: target,
        targetType: type,
        members: <GuildMember>[...widget.members, ..._members],
      );
}

bool channelOverwriteTargetEligible({
  required KaedeGuild guild,
  required EntityRef? actorRef,
  required KaedeRole? actorHighestRole,
  required EntityRef target,
  required String targetType,
  required Iterable<GuildMember> members,
}) {
  if (targetType == 'role') {
    final role = guild.roles.where((item) => item.ref == target).firstOrNull;
    return role != null &&
        guildActorCanManageRole(
          guild: guild,
          actorRef: actorRef,
          actorHighestRole: actorHighestRole,
          target: role,
        );
  }
  if (targetType != 'member') return false;
  final member = members.where((item) => item.user.ref == target).firstOrNull;
  return member != null &&
      guildActorCanManageMember(
        guild: guild,
        actorRef: actorRef,
        actorHighestRole: actorHighestRole,
        target: member,
      );
}

bool canChangeGuildMemberNickname({
  required KaedeGuild guild,
  required EntityRef? actorRef,
  required KaedeRole? actorHighestRole,
  required GuildMember target,
}) {
  final owner = actorRef != null && actorRef == guild.ownerRef;
  if (target.user.ref == actorRef) {
    return owner || guild.allows(Permission.changeNickname);
  }
  return (owner || guild.allows(Permission.manageNicknames)) &&
      guildActorCanManageMember(
        guild: guild,
        actorRef: actorRef,
        actorHighestRole: actorHighestRole,
        target: target,
      );
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

/// Builds a partial channel-position batch for the qualified guild authority.
///
/// Channel position IDs and parents are guild-local snowflakes, not composite
/// entity references. An omitted [parent_id] preserves the existing category,
/// so unrelated rows shifted by the local list animation stay out of the
/// authority-checked request. Moving a category carries its children as a
/// group.
List<Map<String, Object?>> guildChannelPositionRequest(
  List<KaedeChannel> previous,
  List<KaedeChannel> next, {
  required EntityRef movedRef,
}) {
  final previousByRef = <EntityRef, KaedeChannel>{
    for (final channel in previous) channel.ref: channel,
  };
  final moved = next.where((channel) => channel.ref == movedRef).firstOrNull;
  if (moved == null) return const <Map<String, Object?>>[];
  final includedRefs = <EntityRef>{
    movedRef,
    if (moved.type == ChannelType.category)
      for (final channel in next)
        if (channel.parentRef == movedRef) channel.ref,
  };
  return <Map<String, Object?>>[
    for (var index = 0; index < next.length; index++)
      if (includedRefs.contains(next[index].ref))
        <String, Object?>{
          'id': next[index].ref.id.value,
          'position': index,
          if (previousByRef[next[index].ref]?.parentRef !=
              next[index].parentRef)
            'parent_id': next[index].parentRef?.id.value,
        },
  ];
}

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

typedef GuildAuditActionFilter = ({int actionType, String? targetType});

enum GuildAuditActionTone { neutral, danger, success }

final class GuildAuditActionDefinition {
  const GuildAuditActionDefinition({
    required this.actionType,
    required this.label,
    this.targetType,
    this.tone = GuildAuditActionTone.neutral,
  });

  final int actionType;
  final String? targetType;
  final String label;
  final GuildAuditActionTone tone;
}

/// Stable Discord-style action choices. Most actions only need an action code;
/// Kaede's existing overloaded member/instance and channel-order codes also
/// include a target type so the server can disambiguate them without changing
/// any protocol values.
final List<GuildAuditActionDefinition> guildAuditActionDefinitions = [
  GuildAuditActionDefinition(
      actionType: 1, label: L10n.current.ui_guild_updated_f6a4e19f),
  GuildAuditActionDefinition(
    actionType: 10,
    label: L10n.current.ui_channel_created_23af65f4,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
    actionType: 11,
    targetType: 'channel',
    label: L10n.current.ui_channel_updated_0b334ccb,
  ),
  GuildAuditActionDefinition(
    actionType: 11,
    targetType: 'channel_order',
    label: L10n.current.ui_channel_order_updated_691b36eb,
  ),
  GuildAuditActionDefinition(
    actionType: 12,
    label: L10n.current.ui_channel_deleted_302f4b59,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
      actionType: 13, label: L10n.current.ui_channel_permission_added_2d003831),
  GuildAuditActionDefinition(
    actionType: 14,
    label: L10n.current.ui_channel_permission_updated_16332b24,
  ),
  GuildAuditActionDefinition(
    actionType: 15,
    label: L10n.current.ui_channel_permissions_updated_7c7e5fbf,
  ),
  GuildAuditActionDefinition(
    actionType: 16,
    label: L10n.current.ui_channel_permissions_removed_4585d6e4,
  ),
  GuildAuditActionDefinition(
    actionType: 17,
    label: L10n.current.ui_channel_permissions_synced_db0dc85a,
  ),
  GuildAuditActionDefinition(
    actionType: 20,
    label: L10n.current.ui_member_kicked_a300da32,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
      actionType: 21, label: L10n.current.ui_members_pruned_2abddb7c),
  GuildAuditActionDefinition(
    actionType: 22,
    label: L10n.current.ui_member_banned_7f42801d,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 23,
    label: L10n.current.ui_member_unbanned_0c6a5dc6,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 24, label: L10n.current.ui_member_updated_370ac234),
  GuildAuditActionDefinition(
    actionType: 25,
    targetType: 'member',
    label: L10n.current.ui_member_roles_updated_00295fb1,
  ),
  GuildAuditActionDefinition(
    actionType: 25,
    targetType: 'instance',
    label: L10n.current.ui_instance_banned_740b0efe,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 26,
    targetType: 'member',
    label: L10n.current.ui_member_moved_69b0bb14,
  ),
  GuildAuditActionDefinition(
    actionType: 26,
    targetType: 'instance',
    label: L10n.current.ui_instance_unbanned_edcb88c9,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
    actionType: 27,
    targetType: 'member',
    label: L10n.current.ui_member_disconnected_fcf4df8e,
  ),
  GuildAuditActionDefinition(
    actionType: 27,
    targetType: 'user',
    label: L10n.current.ui_ownership_transferred_bdc1b5b0,
  ),
  GuildAuditActionDefinition(
    actionType: 28,
    label: L10n.current.ui_bot_added_aef436b8,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
    actionType: 30,
    label: L10n.current.ui_role_created_9afd451d,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 31, label: L10n.current.ui_role_updated_22ca2f7a),
  GuildAuditActionDefinition(
    actionType: 32,
    label: L10n.current.ui_role_deleted_23b256a4,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
      actionType: 33, label: L10n.current.ui_roles_reordered_0182f1b0),
  GuildAuditActionDefinition(
    actionType: 40,
    label: L10n.current.ui_invite_created_37a57250,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 41, label: L10n.current.ui_invite_updated_8f52b70f),
  GuildAuditActionDefinition(
      actionType: 42, label: L10n.current.ui_invite_deleted_c6b85add),
  GuildAuditActionDefinition(
    actionType: 50,
    label: L10n.current.ui_webhook_created_1bca0506,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 51, label: L10n.current.ui_webhook_updated_ec85f9bd),
  GuildAuditActionDefinition(
    actionType: 52,
    label: L10n.current.ui_webhook_deleted_dc43192b,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 60,
    label: L10n.current.ui_emoji_created_16fdca11,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 61, label: L10n.current.ui_emoji_updated_29d84a76),
  GuildAuditActionDefinition(
    actionType: 62,
    label: L10n.current.ui_emoji_deleted_fc82c9b0,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 72,
    label: L10n.current.ui_message_deleted_edcce219,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 73,
    label: L10n.current.ui_messages_bulk_deleted_1c218842,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
      actionType: 74, label: L10n.current.ui_message_pinned_0245fc7e),
  GuildAuditActionDefinition(
      actionType: 75, label: L10n.current.ui_message_unpinned_5a9b3c59),
  GuildAuditActionDefinition(
    actionType: 80,
    label: L10n.current.ui_app_integration_added_2c3214a0,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 81, label: L10n.current.ui_app_integration_updated_a03127bd),
  GuildAuditActionDefinition(
    actionType: 82,
    label: L10n.current.ui_app_integration_removed_0457ca96,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 83,
    label: L10n.current.ui_stage_instance_created_e7072554,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 84, label: L10n.current.ui_stage_instance_updated_50c0832b),
  GuildAuditActionDefinition(
    actionType: 85,
    label: L10n.current.ui_stage_instance_deleted_1fc488b9,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 90,
    label: L10n.current.ui_sticker_created_3c32a2cc,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 91, label: L10n.current.ui_sticker_updated_9edf4cc3),
  GuildAuditActionDefinition(
    actionType: 92,
    label: L10n.current.ui_sticker_deleted_b18c5be1,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 100,
    label: L10n.current.ui_scheduled_event_created_02bbcaf0,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 101, label: L10n.current.ui_scheduled_event_updated_57f039af),
  GuildAuditActionDefinition(
    actionType: 102,
    label: L10n.current.ui_scheduled_event_deleted_91ceb37d,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 110,
    label: L10n.current.ui_thread_created_7a4bcdb5,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 111, label: L10n.current.ui_thread_updated_0fc12d52),
  GuildAuditActionDefinition(
    actionType: 112,
    label: L10n.current.ui_thread_deleted_3e8d0d2c,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 121,
    label: L10n.current.ui_command_permissions_updated_f62b2015,
  ),
  GuildAuditActionDefinition(
    actionType: 130,
    label: L10n.current.ui_soundboard_sound_created_4c2a7bdf,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 131,
      label: L10n.current.ui_soundboard_sound_updated_eab6637c),
  GuildAuditActionDefinition(
    actionType: 132,
    label: L10n.current.ui_soundboard_sound_deleted_1ea7f246,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 140,
    label: L10n.current.ui_automod_rule_created_384f9568,
    tone: GuildAuditActionTone.success,
  ),
  GuildAuditActionDefinition(
      actionType: 141, label: L10n.current.ui_automod_rule_updated_3dffe807),
  GuildAuditActionDefinition(
    actionType: 142,
    label: L10n.current.ui_automod_rule_deleted_b5012fc5,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 143,
    label: L10n.current.ui_automod_action_applied_7bd25a81,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
      actionType: 144,
      label: L10n.current.ui_message_flagged_by_automod_ee2afb82),
  GuildAuditActionDefinition(
    actionType: 145,
    label: L10n.current.ui_member_timed_out_by_automod_bc7f6770,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
    actionType: 146,
    label: L10n.current.ui_member_quarantined_by_automod_554276b5,
    tone: GuildAuditActionTone.danger,
  ),
  GuildAuditActionDefinition(
      actionType: 192,
      label: L10n.current.ui_voice_channel_status_set_7459c4e8),
  GuildAuditActionDefinition(
    actionType: 193,
    label: L10n.current.ui_voice_channel_status_removed_87d001bc,
  ),
];

final Map<String, String> guildAuditActionFilterOptions = <String, String>{
  for (final definition in guildAuditActionDefinitions)
    '${definition.actionType}|${definition.targetType ?? ''}': definition.label,
};

GuildAuditActionFilter? parseGuildAuditActionFilter(String? value) {
  if (value == null) return null;
  final separator = value.indexOf('|');
  final action =
      int.tryParse(separator < 0 ? value : value.substring(0, separator));
  if (action == null) return null;
  final target = separator < 0 ? '' : value.substring(separator + 1).trim();
  return (actionType: action, targetType: target.isEmpty ? null : target);
}

EntityRef? _parseAuditActorFilter(String? value) {
  if (value == null) return null;
  try {
    return EntityRef.parse(value);
  } on FormatException {
    return null;
  }
}

String guildAuditActionLabel(Map<String, Object?> item) {
  final definition = guildAuditActionDefinition(item);
  if (definition != null) return definition.label;
  final code = guildAuditActionCode(item);
  return code == null
      ? _humanizeAuditAction('${item['action_type'] ?? ''}')
      : 'Unknown action ($code)';
}

GuildAuditActionDefinition? guildAuditActionDefinition(
  Map<String, Object?> item,
) {
  final code = guildAuditActionCode(item);
  final targetType = '${item['target_type'] ?? ''}';
  if (code == null) return null;
  GuildAuditActionDefinition? fallback;
  final normalizedTarget = targetType.isEmpty ? null : targetType;
  for (final definition in guildAuditActionDefinitions) {
    if (definition.actionType != code) continue;
    fallback ??= definition;
    if (definition.targetType == normalizedTarget) return definition;
  }
  return fallback;
}

int? guildAuditActionCode(Map<String, Object?> item) {
  final value = item['action_type'];
  return value is num ? value.toInt() : int.tryParse('$value');
}

String guildAuditActionFilterKey(Map<String, Object?> item) {
  final code = guildAuditActionCode(item);
  if (code == null) {
    return '${item['action_type'] ?? 'unknown'}|${item['target_type'] ?? ''}';
  }
  final exact = '$code|${item['target_type'] ?? ''}';
  if (guildAuditActionFilterOptions.containsKey(exact)) return exact;
  final generic = '$code|';
  if (guildAuditActionFilterOptions.containsKey(generic)) return generic;
  final hasTargetSpecificDefinition = guildAuditActionDefinitions.any(
    (definition) =>
        definition.actionType == code && definition.targetType != null,
  );
  return hasTargetSpecificDefinition ? exact : generic;
}

String guildAuditActionFilterLabel(String value) {
  if (guildAuditActionFilterOptions[value] case final label?) return label;
  final parsed = parseGuildAuditActionFilter(value);
  if (parsed != null) {
    return guildAuditActionLabel(<String, Object?>{
      'action_type': parsed.actionType,
      if (parsed.targetType != null) 'target_type': parsed.targetType,
    });
  }
  return _humanizeAuditAction(value.split('|').first);
}

String _humanizeAuditAction(String value) {
  final normalized = value.trim();
  if (normalized.isEmpty) return 'Guild action';
  final words = normalized
      .split(RegExp(r'[._\-\s]+'))
      .where((part) => part.isNotEmpty)
      .toList();
  if (words.isEmpty) return 'Guild action';
  final useful = words.first == 'guild' ? words.sublist(1) : words;
  if (useful.isEmpty) return 'Guild action';
  const pastTense = <String, String>{
    'add': 'added',
    'ban': 'banned',
    'block': 'blocked',
    'create': 'created',
    'delete': 'deleted',
    'disconnect': 'disconnected',
    'execute': 'applied',
    'kick': 'kicked',
    'move': 'moved',
    'prune': 'pruned',
    'publish': 'published',
    'remove': 'removed',
    'reorder': 'reordered',
    'sync': 'synced',
    'unban': 'unbanned',
    'update': 'updated',
  };
  final operation = pastTense[useful.last];
  final label = operation == null
      ? useful.join(' ')
      : [...useful.take(useful.length - 1), operation].join(' ');
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
    return users[ref]?.name ?? ref.wire;
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
  String targetText(String key) => '${target[key] ?? ''}'.trim();
  String targetName() {
    final direct = targetText('name');
    if (direct.isNotEmpty) return direct;
    for (final change in guildAuditChanges(item)) {
      if (change['key'] != 'name') continue;
      final value = change['new_value'] ?? change['old_value'];
      if (value != null && '$value'.trim().isNotEmpty) return '$value'.trim();
    }
    return '';
  }

  if (type == 'guild') return guild.name;
  if (type == 'channel_order') return 'the channel list';
  if (type == 'channel' || type == 'thread' || type == 'forum_post') {
    final channel =
        guild.channels.where((value) => value.ref == ref).firstOrNull;
    final name = channel?.name ?? targetName();
    if (name.isNotEmpty) return '#$name';
    if (type == 'thread' || type == 'forum_post') {
      return ref == null ? 'a thread' : 'thread ${ref.id.value}';
    }
    return 'a channel';
  }
  if (type == 'role') {
    final role = guild.roles.where((value) => value.ref == ref).firstOrNull;
    final name = role?.name ?? targetName();
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
    final code = targetText('code');
    return code.isEmpty ? 'an invite' : 'invite $code';
  }
  if (type == 'scheduled_event') {
    final name = targetName();
    return name.isEmpty ? 'the scheduled event' : 'event $name';
  }
  if (type == 'webhook') {
    final name = targetName();
    return name.isEmpty ? 'a webhook' : 'webhook $name';
  }
  if (type == 'emoji' || type == 'application_emoji') {
    final name = targetName();
    return name.isEmpty ? 'an emoji' : ':$name:';
  }
  if (type == 'sticker') {
    final name = targetName();
    return name.isEmpty ? 'a sticker' : 'sticker $name';
  }
  if (type == 'soundboard_sound' || type == 'sound') {
    final name = targetName();
    return name.isEmpty ? 'a soundboard sound' : 'sound $name';
  }
  if (type == 'auto_mod_rule' || type == 'automod_rule') {
    final name = targetName();
    return name.isEmpty ? 'an AutoMod rule' : 'AutoMod rule $name';
  }
  if (type == 'message') {
    final messageId = targetText('message_id').isNotEmpty
        ? targetText('message_id')
        : ref?.id.value ?? targetText('id');
    return messageId.isEmpty ? 'a message' : 'message $messageId';
  }
  if (type == 'poll') {
    final question = targetText('question');
    return question.isEmpty ? 'a poll' : 'poll “$question”';
  }
  if (type == 'integration' ||
      type == 'application' ||
      type == 'application_command' ||
      type == 'application_asset') {
    final name = targetName();
    if (name.isNotEmpty) return name;
    return switch (type) {
      'application_command' => 'an application command',
      'application_asset' => 'an application asset',
      'application' => 'an application',
      _ => 'an app integration',
    };
  }
  if (type == 'member_prune' || guildAuditActionCode(item) == 21) {
    final removed = target['members_removed'] ?? target['count'];
    return removed == null ? 'inactive members' : '$removed inactive members';
  }
  if (type.isEmpty) return 'the guild';
  final readable = type.replaceAll(RegExp(r'[_\-.]+'), ' ');
  final article =
      RegExp(r'^[aeiou]', caseSensitive: false).hasMatch(readable) ? 'an' : 'a';
  return '$article $readable';
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
    13 => 'added permissions to',
    14 => 'updated permissions for',
    15 => 'updated permissions for',
    16 => 'removed a permission override from',
    17 => 'synced permissions for',
    20 => 'kicked',
    21 => 'pruned',
    22 => 'banned',
    23 => 'unbanned',
    24 => 'updated',
    25 when targetType == 'instance' => 'banned',
    25 => 'updated roles for',
    26 when targetType == 'instance' => 'unbanned',
    26 => 'moved',
    27 when targetType == 'user' => 'transferred ownership to',
    27 => 'disconnected',
    28 => 'added',
    30 => 'created',
    31 => 'updated',
    32 => 'deleted',
    33 => 'reordered',
    40 => 'created',
    41 => 'updated',
    42 => 'deleted',
    50 => 'created',
    51 => 'updated',
    52 => 'deleted',
    60 => 'created',
    61 => 'updated',
    62 => 'deleted',
    72 => 'deleted',
    73 => 'bulk deleted',
    74 => 'pinned',
    75 => 'unpinned',
    80 => 'added',
    81 => 'updated',
    82 => 'removed',
    83 => 'created',
    84 => 'updated',
    85 => 'deleted',
    90 => 'created',
    91 => 'updated',
    92 => 'deleted',
    100 => 'created',
    101 => 'updated',
    102 => 'deleted',
    110 => 'created',
    111 => 'updated',
    112 => 'deleted',
    121 => 'updated permissions for',
    130 => 'created',
    131 => 'updated',
    132 => 'deleted',
    140 => 'created',
    141 => 'updated',
    142 => 'deleted',
    143 => 'applied AutoMod to',
    144 => 'flagged a message from',
    145 => 'timed out',
    146 => 'quarantined',
    192 => 'set the voice status for',
    193 => 'removed the voice status from',
    _ => 'performed an action on',
  };
  return '$actorName $verb $targetName';
}

IconData guildAuditActionIcon(Map<String, Object?> item) {
  final code = guildAuditActionCode(item);
  final targetType = '${item['target_type'] ?? ''}';
  if (targetType == 'instance') return Icons.public_off_outlined;
  if (targetType == 'thread' || targetType == 'forum_post') {
    return Icons.forum_outlined;
  }
  if (targetType == 'sticker') return Icons.sticky_note_2_outlined;
  if (targetType == 'soundboard_sound' || targetType == 'sound') {
    return Icons.music_note_outlined;
  }
  if (targetType == 'auto_mod_rule' || targetType == 'automod_rule') {
    return Icons.shield_outlined;
  }
  if (code == 1) return Icons.settings_outlined;
  if (code != null && code >= 10 && code < 20) return Icons.tag_rounded;
  if (code != null && code >= 20 && code < 30) {
    return code == 21
        ? Icons.cleaning_services_outlined
        : code == 22 || code == 20
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
  if (code != null && code >= 72 && code < 76) {
    return code == 74 || code == 75
        ? Icons.push_pin_outlined
        : Icons.chat_bubble_outline_rounded;
  }
  if (code != null && code >= 80 && code < 83) {
    return Icons.extension_outlined;
  }
  if (code != null && code >= 83 && code < 86) {
    return Icons.record_voice_over_outlined;
  }
  if (code != null && code >= 90 && code < 93) {
    return Icons.sticky_note_2_outlined;
  }
  if (code != null && code >= 100 && code < 103) {
    return Icons.event_available_outlined;
  }
  if (code != null && code >= 110 && code < 113) {
    return Icons.forum_outlined;
  }
  if (code == 121) return Icons.rule_folder_outlined;
  if (code != null && code >= 130 && code < 133) {
    return Icons.music_note_outlined;
  }
  if (code != null && code >= 140 && code < 147) {
    return Icons.shield_outlined;
  }
  if (code == 192 || code == 193) return Icons.record_voice_over_outlined;
  return Icons.receipt_long_outlined;
}

Color guildAuditActionColor(
  BuildContext context,
  Map<String, Object?> item,
) {
  return switch (guildAuditActionTone(item)) {
    GuildAuditActionTone.danger => context.kaede.dangerSoft,
    GuildAuditActionTone.success => context.kaede.mintSoft,
    GuildAuditActionTone.neutral => context.kaede.coralSoft,
  };
}

GuildAuditActionTone guildAuditActionTone(Map<String, Object?> item) =>
    guildAuditActionDefinition(item)?.tone ?? GuildAuditActionTone.neutral;

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

final class _PermissionRow extends StatelessWidget {
  const _PermissionRow(
      {required this.metadata,
      required this.value,
      required this.enabled,
      required this.changed});
  final PermissionMetadata metadata;
  final int value;
  final bool enabled;
  final ValueChanged<int> changed;

  Widget _selector(BuildContext context) => SegmentedButton<int>(
        showSelectedIcon: false,
        segments: [
          ButtonSegment(
              value: -1,
              icon: Icon(Icons.close, color: context.kaede.danger),
              tooltip: L10n.of(context).ui_deny_07d5fa87),
          ButtonSegment(
              value: 0,
              icon: Icon(Icons.horizontal_rule),
              tooltip: L10n.of(context).ui_inherit_e1611680),
          ButtonSegment(
              value: 1,
              icon: Icon(Icons.check, color: context.kaede.mint),
              tooltip: L10n.of(context).ui_allow_546d19d2)
        ],
        selected: {value},
        onSelectionChanged: enabled ? (value) => changed(value.first) : null,
      );

  @override
  Widget build(BuildContext context) => LayoutBuilder(
        builder: (context, constraints) {
          final copy = Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(metadata.label,
                  style: TextStyle(fontWeight: FontWeight.w700)),
              SizedBox(height: 2),
              Text(metadata.description,
                  style: TextStyle(color: context.kaede.muted, fontSize: 13)),
              if (channelPermissionDependencyLabels(metadata)
                  case final dependencies when dependencies.isNotEmpty)
                Text(
                  L10n.of(context).ui_also_requires_value0_7f2e23b9(
                      (dependencies.join(', ')).toString()),
                  style: TextStyle(color: context.kaede.muted, fontSize: 12),
                ),
              if (!enabled)
                Text(
                  L10n.of(context)
                      .ui_you_can_only_change_permissions_you_currently_84b00a2e,
                  style: TextStyle(color: context.kaede.muted, fontSize: 12),
                ),
            ],
          );
          return Padding(
            padding: EdgeInsets.fromLTRB(16, 10, 16, 12),
            child: constraints.maxWidth < 480
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      copy,
                      SizedBox(height: 9),
                      Align(
                        alignment: Alignment.centerRight,
                        child: _selector(context),
                      ),
                    ],
                  )
                : Row(
                    children: [
                      Expanded(child: copy),
                      SizedBox(width: 16),
                      _selector(context),
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
  Future<List<VoiceRegion>> Function()? loadVoiceRegions,
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
        loadVoiceRegions: loadVoiceRegions,
      ),
    );

List<KaedeChannel> guildTextChannelTargets(Iterable<KaedeChannel> channels) =>
    channels
        .where((channel) =>
            channel.type == ChannelType.text ||
            channel.type == ChannelType.announcement ||
            channel.type == ChannelType.forum)
        .toList(growable: false)
      ..sort((a, b) => a.position.compareTo(b.position));

List<KaedeChannel> guildWebhookManagementTargets(
  Iterable<KaedeChannel> channels, {
  required bool isOwner,
}) =>
    guildTextChannelTargets(channels)
        .where((channel) =>
            channel.encryptionMode != 'e2ee' &&
            canManageEffectiveChannel(
              channel,
              Permission.manageWebhooks,
              isOwner: isOwner,
            ))
        .toList(growable: false);

List<KaedeChannel> guildInviteCreationTargets(
  Iterable<KaedeChannel> channels, {
  required bool isOwner,
}) =>
    channels
        .where((channel) => {
              ChannelType.text,
              ChannelType.voice,
              ChannelType.announcement,
              ChannelType.stage,
              ChannelType.forum,
              ChannelType.tracker,
            }.contains(channel.type))
        .where((channel) =>
            isOwner ||
            channel.allows(Permission.administrator) ||
            channel.allows(Permission.createInvite))
        .toList(growable: false)
      ..sort((a, b) => a.position.compareTo(b.position));

Future<KaedeChannel?> showGuildChannelPicker(
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
            padding: EdgeInsets.fromLTRB(20, 0, 20, 10),
            child: Text(title,
                style: Theme.of(context)
                    .textTheme
                    .headlineSmall
                    ?.copyWith(fontWeight: FontWeight.w900)),
          ),
          Flexible(
            child: ListView(
              shrinkWrap: true,
              padding: EdgeInsets.fromLTRB(10, 0, 10, 16),
              children: [
                for (final channel in channels)
                  ListTile(
                    leading: Icon(switch (channel.type) {
                      ChannelType.announcement => Icons.campaign_rounded,
                      ChannelType.forum => Icons.forum_outlined,
                      ChannelType.voice => Icons.volume_up_rounded,
                      ChannelType.stage => Icons.podcasts_rounded,
                      ChannelType.tracker => Icons.view_kanban_outlined,
                      _ => Icons.tag_rounded,
                    }),
                    title: Text(channel.name ?? 'channel'),
                    subtitle: Text(switch (channel.type) {
                      ChannelType.announcement =>
                        L10n.of(context).ui_announcement_channel_ec8b9fb9,
                      ChannelType.forum =>
                        L10n.of(context).ui_forum_channel_cdffa0a5,
                      ChannelType.voice =>
                        L10n.of(context).ui_voice_channel_d243a4f2,
                      ChannelType.stage =>
                        L10n.of(context).ui_stage_channel_8ff46126,
                      ChannelType.tracker =>
                        L10n.of(context).ui_task_tracker_channel_7292dbc3,
                      _ => L10n.of(context).ui_text_channel_58162d0d,
                    }),
                    trailing: Icon(Icons.chevron_right_rounded),
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
    this.loadVoiceRegions,
  });

  final KaedeChannel? channel;
  final List<KaedeChannel> channels;
  final EntityRef? initialParent;
  final bool e2eeActivationEnabled;
  final Future<List<VoiceRegion>> Function()? loadVoiceRegions;

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
  late bool _nsfw = widget.channel?.nsfw ?? false;
  late int _slow = widget.channel?.slowModeSeconds ?? 0;
  late int _bitrate = widget.channel?.bitrate ?? 64000;
  late int _userLimit = widget.channel?.userLimit ?? 0;
  late int _videoQualityMode = widget.channel?.videoQualityMode ?? 1;
  late String? _rtcRegion = widget.channel?.rtcRegion;
  List<VoiceRegion> _voiceRegions = const <VoiceRegion>[];
  var _loadingVoiceRegions = false;
  String? _voiceRegionsError;
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

  bool get _voiceLike => _type.isVoiceLike;

  static const _types = <(ChannelType, String, IconData)>[
    (ChannelType.text, 'Text', Icons.tag_rounded),
    (ChannelType.voice, 'Voice', Icons.volume_up_rounded),
    (ChannelType.stage, 'Stage', Icons.record_voice_over_outlined),
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
  void initState() {
    super.initState();
    if (widget.loadVoiceRegions != null) unawaited(_loadVoiceRegions());
  }

  Future<void> _loadVoiceRegions() async {
    final load = widget.loadVoiceRegions;
    if (load == null || _loadingVoiceRegions) return;
    setState(() {
      _loadingVoiceRegions = true;
      _voiceRegionsError = null;
    });
    try {
      final regions = await load();
      if (!mounted) return;
      setState(() => _voiceRegions = regions);
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _voiceRegionsError = userFacingError(
          error,
          summary: L10n.of(context)
              .ui_region_overrides_are_temporarily_unavailable_84a7f6a8,
        );
      });
    } finally {
      if (mounted) setState(() => _loadingVoiceRegions = false);
    }
  }

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
        title: Text(L10n.of(context)
            .ui_require_end_to_end_encryption_for_posts_c262b4d3),
        content: Text(
          L10n.of(context)
              .ui_only_new_posts_will_use_this_policy_each_post_1635f1d1,
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton.icon(
            onPressed: () => Navigator.pop(dialogContext, true),
            icon: Icon(Icons.lock_rounded),
            label: Text(L10n.of(context).ui_require_encryption_aa6a2ced),
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
      padding: EdgeInsets.fromLTRB(20, 10, 20, 18),
      child: SizedBox(
        width: double.infinity,
        child: FilledButton.icon(
          key: ValueKey('save-channel-button'),
          onPressed: _save,
          icon: Icon(
              widget.channel == null ? Icons.add_rounded : Icons.save_outlined),
          label: Text(widget.channel == null
              ? L10n.of(context).ui_create_channel_fd2226d6
              : L10n.of(context).ui_save_changes_61efb279),
        ),
      ),
    );
    return AnimatedPadding(
      duration: Duration(milliseconds: 180),
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
                  padding: EdgeInsets.fromLTRB(20, 0, 12, 12),
                  child: Row(
                    children: [
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              widget.channel == null
                                  ? L10n.of(context)
                                      .ui_create_a_channel_fa787131
                                  : L10n.of(context).ui_edit_channel_dfb3900a,
                              style: Theme.of(context)
                                  .textTheme
                                  .headlineSmall
                                  ?.copyWith(fontWeight: FontWeight.w900),
                            ),
                            SizedBox(height: 3),
                            Text(
                              widget.channel == null
                                  ? L10n.of(context)
                                      .ui_choose_what_members_can_use_this_space_for_71b22a5d
                                  : L10n.of(context)
                                      .ui_update_how_this_channel_appears_and_behaves_d3703cec,
                              style: TextStyle(color: context.kaede.muted),
                            ),
                          ],
                        ),
                      ),
                      IconButton(
                        tooltip: L10n.of(context).ui_close_cd86acc3,
                        onPressed: () => Navigator.pop(context),
                        icon: Icon(Icons.close_rounded),
                      ),
                    ],
                  ),
                ),
              if (!compactKeyboardLayout) Divider(height: 1),
              Flexible(
                child: SingleChildScrollView(
                  keyboardDismissBehavior:
                      ScrollViewKeyboardDismissBehavior.onDrag,
                  padding: EdgeInsets.fromLTRB(20, 18, 20, 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      TextFormField(
                        key: ValueKey('channel-name-field'),
                        controller: _name,
                        autofocus: widget.channel == null,
                        maxLength: 100,
                        textInputAction: TextInputAction.next,
                        decoration: InputDecoration(
                          labelText: L10n.of(context).ui_channel_name_0700b8a7,
                          hintText: _type == ChannelType.category
                              ? L10n.of(context).ui_new_category_e3f79467
                              : L10n.of(context).ui_new_channel_34469ca7,
                          prefixIcon: Icon(switch (_type) {
                            ChannelType.category => Icons.folder_outlined,
                            ChannelType.voice => Icons.volume_up_rounded,
                            ChannelType.stage =>
                              Icons.record_voice_over_outlined,
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
                        SizedBox(height: 8),
                        Text(L10n.of(context).ui_channel_type_151683a4,
                            style: Theme.of(context).textTheme.titleMedium),
                        SizedBox(height: 9),
                        GridView.count(
                          crossAxisCount: 2,
                          mainAxisSpacing: 8,
                          crossAxisSpacing: 8,
                          childAspectRatio: 2.55,
                          shrinkWrap: true,
                          physics: NeverScrollableScrollPhysics(),
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
                                  if (_type == ChannelType.stage) {
                                    _bitrate = _bitrate.clamp(8000, 64000);
                                  } else if (_type == ChannelType.voice) {
                                    _userLimit = _userLimit.clamp(0, 99);
                                  }
                                  if (_type == ChannelType.category) {
                                    _parent = '';
                                  }
                                }),
                              ),
                          ],
                        ),
                      ] else ...[
                        SizedBox(height: 4),
                        Row(
                          children: [
                            Icon(
                              switch (_type) {
                                ChannelType.category => Icons.folder_outlined,
                                ChannelType.voice => Icons.volume_up_rounded,
                                ChannelType.stage =>
                                  Icons.record_voice_over_outlined,
                                ChannelType.announcement =>
                                  Icons.campaign_rounded,
                                ChannelType.forum => Icons.forum_outlined,
                                ChannelType.tracker =>
                                  Icons.view_kanban_outlined,
                                _ => Icons.tag_rounded,
                              },
                              size: 15,
                              color: context.kaede.muted,
                            ),
                            SizedBox(width: 7),
                            Expanded(
                              child: Text(
                                L10n.of(context)
                                    .ui_value0_the_type_cannot_change_after_creation_5ea0cc90(
                                        (switch (_type) {
                                  ChannelType.category => 'Category',
                                  ChannelType.voice => 'Voice channel',
                                  ChannelType.stage => 'Stage channel',
                                  ChannelType.announcement =>
                                    'Announcement channel',
                                  ChannelType.forum => 'Forum channel',
                                  ChannelType.tracker => 'Task tracker',
                                  _ => 'Text channel'
                                })
                                            .toString()),
                                style: TextStyle(
                                  color: context.kaede.muted,
                                  fontSize: 12,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ],
                      if (_type != ChannelType.category) ...[
                        SizedBox(height: 18),
                        DropdownButtonFormField<String>(
                          key: ValueKey('channel-category-field'),
                          initialValue: categories
                                  .any((channel) => channel.ref.wire == _parent)
                              ? _parent
                              : '',
                          isExpanded: true,
                          decoration: InputDecoration(
                            labelText: L10n.of(context).ui_category_afb3e591,
                            prefixIcon: Icon(Icons.folder_outlined),
                          ),
                          items: [
                            DropdownMenuItem(
                                value: '',
                                child: Text(
                                    L10n.of(context).ui_no_category_44662d06)),
                            for (final category in categories)
                              DropdownMenuItem(
                                value: category.ref.wire,
                                child: Text(category.name ?? 'Category'),
                              ),
                          ],
                          onChanged: (value) =>
                              setState(() => _parent = value ?? ''),
                        ),
                        SizedBox(height: 14),
                        TextFormField(
                          controller: _topic,
                          maxLength: 1024,
                          minLines: 2,
                          maxLines: 3,
                          textCapitalization: TextCapitalization.sentences,
                          decoration: InputDecoration(
                            labelText: _voiceLike
                                ? L10n.of(context)
                                    .ui_description_optional_31e71764
                                : _type == ChannelType.forum
                                    ? L10n.of(context)
                                        .ui_post_guidelines_optional_5d4ad955
                                    : L10n.of(context)
                                        .ui_topic_optional_3542345b,
                            alignLabelWithHint: true,
                          ),
                        ),
                        if (_voiceLike) ...[
                          SizedBox(height: 14),
                          Text(
                            L10n.of(context)
                                .ui_audio_bitrate_value0_kbps_4f0c3405(
                                    ((_bitrate / 1000).round()).toString()),
                            style: TextStyle(fontWeight: FontWeight.w700),
                          ),
                          Slider(
                            key: ValueKey('voice-bitrate-field'),
                            value: _bitrate
                                .clamp(
                                  8000,
                                  _type == ChannelType.stage ? 64000 : 384000,
                                )
                                .toDouble(),
                            min: 8000,
                            max: _type == ChannelType.stage ? 64000 : 384000,
                            divisions: _type == ChannelType.stage ? 7 : 47,
                            label: L10n.of(context).ui_value0_kbps_f480dbec(
                                ((_bitrate / 1000).round()).toString()),
                            onChanged: (value) => setState(
                              () => _bitrate = (value / 8000)
                                      .round()
                                      .clamp(
                                        1,
                                        _type == ChannelType.stage ? 8 : 48,
                                      )
                                      .toInt() *
                                  8000,
                            ),
                          ),
                          SizedBox(height: 4),
                          Text(
                            _userLimit == 0
                                ? L10n.of(context)
                                    .ui_user_limit_unlimited_fce2e0fa
                                : L10n.of(context)
                                    .ui_user_limit_value0_08b75f4e(
                                        (_userLimit).toString()),
                            style: TextStyle(fontWeight: FontWeight.w700),
                          ),
                          Slider(
                            key: ValueKey('voice-user-limit-field'),
                            value: _userLimit.toDouble(),
                            min: 0,
                            max: _type == ChannelType.stage ? 10000 : 99,
                            divisions: _type == ChannelType.stage ? 10000 : 99,
                            label: _userLimit == 0
                                ? L10n.of(context).ui_unlimited_2d283a80
                                : L10n.of(context).ui_value0_26e9163c(
                                    (_userLimit).toString()),
                            onChanged: (value) =>
                                setState(() => _userLimit = value.round()),
                          ),
                          SizedBox(height: 4),
                          DropdownButtonFormField<int>(
                            key: ValueKey('voice-video-quality-field'),
                            initialValue: _videoQualityMode,
                            decoration: InputDecoration(
                              labelText: L10n.of(context)
                                  .ui_camera_video_quality_b2692988,
                              helperText: L10n.of(context)
                                  .ui_automatic_adapts_to_network_conditions_full_k_e86fa367,
                              prefixIcon: Icon(Icons.videocam_outlined),
                            ),
                            items: [
                              DropdownMenuItem(
                                  value: 1,
                                  child: Text(
                                      L10n.of(context).ui_automatic_616a6784)),
                              DropdownMenuItem(
                                  value: 2,
                                  child: Text(L10n.of(context)
                                      .ui_full_quality_d085cb95)),
                            ],
                            onChanged: (value) =>
                                setState(() => _videoQualityMode = value ?? 1),
                          ),
                          SizedBox(height: 4),
                          DropdownButtonFormField<String>(
                            key: ValueKey('voice-region-override-field'),
                            initialValue: _rtcRegion ?? '',
                            isExpanded: true,
                            decoration: InputDecoration(
                              labelText:
                                  L10n.of(context).ui_region_override_0f60d437,
                              helperText: _voiceRegionsError ??
                                  'Automatic chooses the lowest-latency region advertised by this server.',
                              errorMaxLines: 2,
                              prefixIcon: Icon(Icons.public_rounded),
                              suffixIcon: _loadingVoiceRegions
                                  ? Padding(
                                      padding: EdgeInsets.all(14),
                                      child: SizedBox.square(
                                        dimension: 18,
                                        child: CircularProgressIndicator(
                                            strokeWidth: 2),
                                      ),
                                    )
                                  : _voiceRegionsError == null
                                      ? null
                                      : IconButton(
                                          tooltip: L10n.of(context)
                                              .ui_retry_region_discovery_ceb09eb1,
                                          onPressed: _loadVoiceRegions,
                                          icon: Icon(Icons.refresh_rounded),
                                        ),
                            ),
                            items: [
                              DropdownMenuItem(
                                value: '',
                                child: Text(
                                    L10n.of(context).ui_automatic_616a6784),
                              ),
                              if (_rtcRegion != null &&
                                  !_voiceRegions
                                      .any((region) => region.id == _rtcRegion))
                                DropdownMenuItem(
                                  value: _rtcRegion,
                                  child: Text(L10n.of(context)
                                      .ui_value0_unavailable_b6c9188d(
                                          (_rtcRegion).toString())),
                                ),
                              for (final region in _voiceRegions)
                                DropdownMenuItem(
                                  value: region.id,
                                  enabled: !region.deprecated ||
                                      region.id == _rtcRegion,
                                  child: Text(
                                    L10n.of(context)
                                        .ui_value0_value1_value2_d1940c97(
                                            (region.name).toString(),
                                            (region.optimal
                                                    ? ' — Recommended'
                                                    : '')
                                                .toString(),
                                            (region.deprecated
                                                    ? ' — Deprecated'
                                                    : '')
                                                .toString()),
                                  ),
                                ),
                            ],
                            onChanged: _loadingVoiceRegions
                                ? null
                                : (value) => setState(
                                      () => _rtcRegion =
                                          value?.isEmpty == true ? null : value,
                                    ),
                          ),
                        ],
                        if (!_voiceLike && _type != ChannelType.tracker) ...[
                          SizedBox(height: 4),
                          DropdownButtonFormField<int>(
                            initialValue: _slow,
                            isExpanded: true,
                            decoration: InputDecoration(
                              labelText: _type == ChannelType.forum
                                  ? L10n.of(context).ui_post_slow_mode_3edd7fdb
                                  : L10n.of(context).ui_slow_mode_855fc3fb,
                              helperText: _type == ChannelType.forum
                                  ? L10n.of(context)
                                      .ui_how_long_members_wait_between_posts_f02942be
                                  : L10n.of(context)
                                      .ui_how_long_members_wait_between_messages_234b28b9,
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
                          if (_type == ChannelType.text ||
                              _type == ChannelType.announcement ||
                              _type == ChannelType.forum) ...[
                            SizedBox(height: 8),
                            SwitchListTile.adaptive(
                              key: ValueKey('channel-nsfw-field'),
                              contentPadding: EdgeInsets.zero,
                              title: Text(L10n.of(context)
                                  .ui_age_restricted_channel_9dfeba31),
                              subtitle: Text(
                                L10n.of(context)
                                    .ui_only_age_assured_adults_can_use_age_restricte_e906349c,
                              ),
                              value: _nsfw,
                              onChanged: (value) =>
                                  setState(() => _nsfw = value),
                            ),
                          ],
                        ],
                        if (_type == ChannelType.tracker &&
                            widget.channel == null) ...[
                          SizedBox(height: 14),
                          TextFormField(
                            key: ValueKey('tracker-key-prefix-field'),
                            controller: _trackerPrefix,
                            maxLength: 10,
                            textCapitalization: TextCapitalization.characters,
                            decoration: InputDecoration(
                              labelText:
                                  L10n.of(context).ui_task_key_prefix_37585bcd,
                              helperText: L10n.of(context)
                                  .ui_used_for_task_ids_for_example_task_24_97923cfd,
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
                          SizedBox(height: 16),
                          DropdownButtonFormField<int>(
                            initialValue: _defaultAutoArchive,
                            decoration: InputDecoration(
                              labelText: L10n.of(context)
                                  .ui_hide_posts_after_inactivity_d29b91de,
                              prefixIcon: Icon(Icons.archive_outlined),
                            ),
                            items: [
                              DropdownMenuItem(
                                  value: 60,
                                  child: Text(
                                      L10n.of(context).ui_1_hour_0c43fb4a)),
                              DropdownMenuItem(
                                  value: 1440,
                                  child: Text(
                                      L10n.of(context).ui_24_hours_42929680)),
                              DropdownMenuItem(
                                  value: 4320,
                                  child: Text(
                                      L10n.of(context).ui_3_days_2d8e0d65)),
                              DropdownMenuItem(
                                  value: 10080,
                                  child: Text(
                                      L10n.of(context).ui_1_week_d0d53158)),
                            ],
                            onChanged: (value) => setState(() =>
                                _defaultAutoArchive =
                                    value ?? _defaultAutoArchive),
                          ),
                          SizedBox(height: 14),
                          DropdownButtonFormField<int>(
                            initialValue: _threadSlow,
                            decoration: InputDecoration(
                              labelText: L10n.of(context)
                                  .ui_default_reply_slow_mode_b521b1f6,
                              helperText: L10n.of(context)
                                  .ui_how_long_members_wait_between_replies_6513bbbb,
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
                          SizedBox(height: 14),
                          DropdownButtonFormField<int>(
                            initialValue: _forumSort,
                            decoration: InputDecoration(
                              labelText: L10n.of(context)
                                  .ui_default_sort_order_d01cf9d6,
                              prefixIcon: Icon(Icons.swap_vert_rounded),
                            ),
                            items: [
                              DropdownMenuItem(
                                  value: 0,
                                  child: Text(L10n.of(context)
                                      .ui_recently_active_58db5e19)),
                              DropdownMenuItem(
                                  value: 1,
                                  child: Text(L10n.of(context)
                                      .ui_date_posted_1e479eb4)),
                            ],
                            onChanged: (value) =>
                                setState(() => _forumSort = value ?? 0),
                          ),
                          SizedBox(height: 14),
                          DropdownButtonFormField<int>(
                            initialValue: _forumLayout,
                            decoration: InputDecoration(
                              labelText:
                                  L10n.of(context).ui_default_layout_04af82c4,
                              prefixIcon: Icon(Icons.view_agenda_outlined),
                            ),
                            items: [
                              DropdownMenuItem(
                                  value: 0,
                                  child: Text(
                                      L10n.of(context).ui_not_set_d7958fc6)),
                              DropdownMenuItem(
                                  value: 1,
                                  child:
                                      Text(L10n.of(context).ui_list_8d2937a1)),
                              DropdownMenuItem(
                                  value: 2,
                                  child: Text(
                                      L10n.of(context).ui_gallery_d7b54cdd)),
                            ],
                            onChanged: (value) =>
                                setState(() => _forumLayout = value ?? 0),
                          ),
                          SizedBox(height: 14),
                          TextFormField(
                            controller: _defaultReaction,
                            maxLength: 64,
                            onChanged: (_) =>
                                setState(() => _defaultReactionEdited = true),
                            decoration: InputDecoration(
                              labelText: L10n.of(context)
                                  .ui_default_reaction_emoji_optional_edb59386,
                              counterText: '',
                              prefixIcon: Icon(Icons.add_reaction_outlined),
                              helperText: !_defaultReactionEdited &&
                                      _defaultReactionId?.isNotEmpty == true
                                  ? L10n.of(context)
                                      .ui_a_custom_emoji_is_selected_84469cf2
                                  : null,
                              suffixIcon: !_defaultReactionEdited &&
                                      _defaultReactionId?.isNotEmpty == true
                                  ? IconButton(
                                      tooltip: L10n.of(context)
                                          .ui_clear_default_reaction_c02d6396,
                                      onPressed: () => setState(() {
                                        _defaultReactionEdited = true;
                                        _defaultReaction.clear();
                                      }),
                                      icon: Icon(Icons.close_rounded),
                                    )
                                  : null,
                            ),
                          ),
                          SwitchListTile(
                            contentPadding: EdgeInsets.zero,
                            value: _requireTag,
                            onChanged: (value) =>
                                setState(() => _requireTag = value),
                            title: Text(
                                L10n.of(context).ui_require_a_tag_96b692a3),
                            subtitle: Text(L10n.of(context)
                                .ui_members_must_select_a_tag_before_posting_70a7a179),
                          ),
                          if (widget.e2eeActivationEnabled || _e2eeRequired)
                            SwitchListTile(
                              contentPadding: EdgeInsets.zero,
                              value: _e2eeRequired,
                              onChanged: widget.channel?.e2eeRequired == true
                                  ? null
                                  : (value) =>
                                      unawaited(_setE2eeRequired(value)),
                              title: Text(L10n.of(context)
                                  .ui_require_end_to_end_encryption_for_posts_c95f87fe),
                              subtitle: Text(_e2eeRequired
                                  ? L10n.of(context)
                                      .ui_the_entire_starter_its_files_and_all_future_r_b7ce425c
                                  : L10n.of(context)
                                      .ui_new_post_starters_files_and_replies_remain_re_bf6049f4),
                            ),
                          SizedBox(height: 8),
                          Row(
                            children: [
                              Expanded(
                                child: Text(
                                    L10n.of(context).ui_tags_value0_20_3af19317(
                                        (_forumTags.length).toString()),
                                    style: Theme.of(context)
                                        .textTheme
                                        .titleMedium),
                              ),
                              TextButton.icon(
                                onPressed: _forumTags.length >= 20
                                    ? null
                                    : () => _editForumTag(),
                                icon: Icon(Icons.add_rounded, size: 17),
                                label: Text(
                                    L10n.of(context).ui_create_tag_deec4e4d),
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
                                backgroundColor: context.kaede.raised,
                                child: Text(_forumTags[index].emoji ?? '#'),
                              ),
                              title: Text(_forumTags[index].name),
                              subtitle: _forumTags[index].moderated
                                  ? Text(L10n.of(context).ui_moderated_eaca0b0c)
                                  : null,
                              trailing: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  IconButton(
                                    tooltip:
                                        L10n.of(context).ui_edit_tag_82fd7fa1,
                                    onPressed: () => _editForumTag(index),
                                    icon: Icon(Icons.edit_outlined),
                                  ),
                                  IconButton(
                                    tooltip:
                                        L10n.of(context).ui_delete_tag_5b3fe0de,
                                    onPressed: () => setState(
                                        () => _forumTags.removeAt(index)),
                                    icon: Icon(Icons.delete_outline_rounded),
                                  ),
                                ],
                              ),
                            ),
                        ],
                        if (_type == ChannelType.text ||
                            _type == ChannelType.announcement) ...[
                          SizedBox(height: 14),
                          DropdownButtonFormField<String>(
                            key: ValueKey('channel-history-policy-field'),
                            initialValue: _history,
                            isExpanded: true,
                            decoration: InputDecoration(
                              labelText: L10n.of(context)
                                  .ui_federated_history_3e086dab,
                              helperText: L10n.of(context)
                                  .ui_override_how_remote_instances_retain_this_cha_783e4cf3,
                              prefixIcon: Icon(Icons.history_rounded),
                            ),
                            items: [
                              DropdownMenuItem(
                                  value: 'inherit',
                                  child: Text(L10n.of(context)
                                      .ui_use_guild_setting_5c1e91cb)),
                              DropdownMenuItem(
                                  value: 'disabled',
                                  child: Text(L10n.of(context)
                                      .ui_recent_messages_only_9a1ca024)),
                              DropdownMenuItem(
                                  value: 'full_retained',
                                  child: Text(L10n.of(context)
                                      .ui_retain_full_history_55f35965)),
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
          title: Text(existing == null
              ? L10n.of(context).ui_create_tag_deec4e4d
              : L10n.of(context).ui_edit_tag_e5881741),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: name,
                autofocus: true,
                maxLength: 20,
                decoration: InputDecoration(
                  labelText: L10n.of(context).ui_tag_name_e96325c2,
                  counterText: '',
                ),
              ),
              SizedBox(height: 10),
              TextField(
                controller: emoji,
                maxLength: 64,
                onChanged: (_) => setDialogState(() => emojiEdited = true),
                decoration: InputDecoration(
                  labelText: L10n.of(context).ui_emoji_optional_008fc6b8,
                  counterText: '',
                  helperText: !emojiEdited &&
                          existing?.emojiId?.isNotEmpty == true
                      ? L10n.of(context).ui_a_custom_emoji_is_selected_84469cf2
                      : null,
                  suffixIcon: !emojiEdited &&
                          existing?.emojiId?.isNotEmpty == true
                      ? IconButton(
                          tooltip: L10n.of(context).ui_clear_tag_emoji_c5f2138c,
                          onPressed: () => setDialogState(() {
                            emojiEdited = true;
                            emoji.clear();
                          }),
                          icon: Icon(Icons.close_rounded),
                        )
                      : null,
                ),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                value: moderated,
                onChanged: (value) => setDialogState(() => moderated = value),
                title: Text(L10n.of(context).ui_moderated_eaca0b0c),
                subtitle: Text(L10n.of(context)
                    .ui_only_members_who_can_manage_threads_can_use_t_7a11d6af),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
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
                child: Text(existing == null
                    ? L10n.of(context).ui_create_990de47d
                    : L10n.of(context).ui_save_4d2d5d68),
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
        nsfw: _nsfw,
        type: _type,
        slowModeSeconds: _type == ChannelType.category ||
                _voiceLike ||
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
        bitrate: _voiceLike ? _bitrate : null,
        userLimit: _voiceLike ? _userLimit : null,
        rtcRegion: _voiceLike ? _rtcRegion : null,
        videoQualityMode: _voiceLike ? _videoQualityMode : null,
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
        color: selected ? context.kaede.selected : context.kaede.raised,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(
            color: selected ? context.kaede.coral : context.kaede.border,
            width: selected ? 1.5 : 1,
          ),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: enabled ? onTap : null,
          child: Padding(
            padding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            child: Row(
              children: [
                Icon(icon,
                    size: 20,
                    color:
                        selected ? context.kaede.coral : context.kaede.muted),
                SizedBox(width: 8),
                Expanded(
                  child: Text(label,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontWeight: FontWeight.w800,
                        color: enabled || selected ? null : context.kaede.muted,
                      )),
                ),
                if (selected) Icon(Icons.check_rounded, size: 17),
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
    this.nsfw = false,
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
    this.bitrate,
    this.userLimit,
    this.rtcRegion,
    this.videoQualityMode,
  });

  final String name;
  final String topic;
  final bool nsfw;
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
  final int? bitrate;
  final int? userLimit;
  final String? rtcRegion;
  final int? videoQualityMode;

  Map<String, Object?> get json => {
        'name': name,
        'type': _channelNumber(type),
        'topic': type == ChannelType.category || topic.isEmpty ? null : topic,
        'nsfw': type == ChannelType.text ||
                type == ChannelType.announcement ||
                type == ChannelType.forum
            ? nsfw
            : false,
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
        if (type.isVoiceLike) 'bitrate': bitrate ?? 64000,
        if (type.isVoiceLike) 'user_limit': userLimit ?? 0,
        if (type.isVoiceLike) 'rtc_region': rtcRegion,
        if (type.isVoiceLike) 'video_quality_mode': videoQualityMode ?? 1,
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
  const _RoleEditor({this.role, required this.heldPermissions});
  final KaedeRole? role;
  final BigInt heldPermissions;
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
                ? L10n.of(context).ui_create_role_b4d57cd3
                : L10n.of(context)
                    .ui_edit_value0_8e92dac2((widget.role!.name).toString())),
            actions: [
              if (widget.role != null && !widget.role!.managed)
                IconButton(
                    tooltip: L10n.of(context).ui_delete_role_bdde90c6,
                    style: IconButton.styleFrom(
                      foregroundColor: context.kaede.danger,
                    ),
                    onPressed: _confirmDelete,
                    icon: Icon(Icons.delete_outline_rounded))
            ]),
        body: ListView(padding: EdgeInsets.all(16), children: [
          if (widget.role?.managed ?? false)
            SettingsInfo(
              'This role is managed by a bot integration. Its permissions are editable, '
              'but its membership cannot be changed manually. Remove the integration '
              'to remove this role. API scopes, event access, channel restrictions, '
              'and encrypted-channel consent also apply.',
            ),
          TextField(
              controller: _name,
              decoration: InputDecoration(
                  labelText: L10n.of(context).ui_role_name_275452c0)),
          SizedBox(height: 18),
          Text(L10n.of(context).ui_role_colour_9152d88b,
              style: Theme.of(context).textTheme.titleMedium),
          SizedBox(height: 4),
          Text(
            L10n.of(context)
                .ui_members_show_their_highest_coloured_role_fc60b70c,
            style: TextStyle(color: context.kaede.muted, fontSize: 12.5),
          ),
          SizedBox(height: 12),
          Wrap(spacing: 10, runSpacing: 10, children: [
            for (final color in _colors)
              Tooltip(
                message: color == 0
                    ? L10n.of(context).ui_no_colour_2e52ed02
                    : L10n.of(context).ui_custom_colour_f6e243be,
                child: InkWell(
                  onTap: () => setState(() => _color = color),
                  borderRadius: BorderRadius.circular(KaedeRadius.medium),
                  child: Container(
                    width: 42,
                    height: 42,
                    decoration: BoxDecoration(
                      color: color == 0
                          ? context.kaede.raised
                          : Color(0xFF000000 | color),
                      borderRadius: BorderRadius.circular(KaedeRadius.medium),
                      border: Border.all(
                        color: _color == color
                            ? context.kaede.text
                            : context.kaede.border,
                        width: _color == color ? 2.5 : 1,
                      ),
                    ),
                    child: color == 0
                        ? Icon(Icons.format_color_reset_rounded,
                            size: 17, color: context.kaede.muted)
                        : _color == color
                            ? Icon(
                                Icons.check_rounded,
                                size: 20,
                                color: readableForeground(
                                  Color(0xFF000000 | color),
                                ),
                              )
                            : null,
                  ),
                ),
              ),
          ]),
          SizedBox(height: 18),
          Text(L10n.of(context).ui_role_icon_73cd9e0a,
              style: Theme.of(context).textTheme.titleMedium),
          SizedBox(height: 4),
          Text(
            L10n.of(context)
                .ui_shown_beside_names_in_chat_a_member_uses_thei_e0ff5023,
            style: TextStyle(color: context.kaede.muted, fontSize: 12.5),
          ),
          SizedBox(height: 12),
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
              SizedBox.square(
                dimension: 44,
                child: DecoratedBox(
                  decoration: BoxDecoration(
                    color: context.kaede.raised,
                    borderRadius: BorderRadius.all(Radius.circular(12)),
                  ),
                  child:
                      Icon(Icons.shield_outlined, color: context.kaede.muted),
                ),
              ),
            SizedBox(width: 12),
            OutlinedButton.icon(
              onPressed: _pickRoleIcon,
              icon: Icon(Icons.image_outlined),
              label: Text(_iconFile == null && widget.role?.iconHash == null
                  ? L10n.of(context).ui_choose_icon_0bf69081
                  : L10n.of(context).ui_change_icon_ca1b1c46),
            ),
            if (_iconFile != null ||
                (!_removeIcon && widget.role?.iconHash != null)) ...[
              SizedBox(width: 8),
              TextButton(
                onPressed: () => setState(() {
                  _iconFile = null;
                  _removeIcon = true;
                }),
                child: Text(L10n.of(context).ui_remove_21a5901d),
              ),
            ],
          ]),
          SizedBox(height: 18),
          _Panel(
            title: L10n.of(context).ui_display_5a05d9b5,
            child: Column(
              children: [
                SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text(L10n.of(context)
                        .ui_show_separately_in_the_member_list_1948a204),
                    subtitle: Text(
                      L10n.of(context)
                          .ui_members_with_this_role_get_their_own_section_89e8a92f,
                    ),
                    value: _hoist,
                    onChanged: (value) => setState(() => _hoist = value)),
                SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text(L10n.of(context)
                        .ui_allow_anyone_to_mention_this_role_d963c24f),
                    value: _mentionable,
                    onChanged: (value) => setState(() => _mentionable = value)),
              ],
            ),
          ),
          Text(L10n.of(context).ui_permissions_8700fd83,
              style: Theme.of(context).textTheme.titleMedium),
          SizedBox(height: 4),
          Text(
            L10n.of(context)
                .ui_roles_grant_guild_wide_abilities_and_channel__f491df60,
            style: TextStyle(color: context.kaede.muted, fontSize: 12.5),
          ),
          SizedBox(height: 14),
          TextField(
            controller: _permissionSearch,
            decoration: InputDecoration(
              hintText: L10n.of(context).ui_search_permissions_6eb9399d,
              isDense: true,
              prefixIcon: Icon(Icons.search_rounded, size: 19),
              suffixIcon: _permissionSearch.text.isEmpty
                  ? null
                  : IconButton(
                      tooltip: L10n.of(context).ui_clear_search_5a13e946,
                      onPressed: _permissionSearch.clear,
                      icon: Icon(Icons.close_rounded, size: 18),
                    ),
            ),
          ),
          SizedBox(height: 14),
          if (_permissions & BigInt.from(Permission.administrator) !=
              BigInt.zero)
            Card(
              color: context.kaede.warningSoft,
              child: ListTile(
                leading: Icon(Icons.warning_amber_rounded,
                    color: context.kaede.warning),
                title: Text(L10n.of(context)
                    .ui_administrator_bypasses_every_channel_restrict_35ffa5ee),
                subtitle: Text(L10n.of(context)
                    .ui_only_grant_it_to_people_who_should_have_unres_56724462),
              ),
            ),
          for (final group in groups)
            if (visiblePermissions
                    .where((item) => item.group == group)
                    .toList(growable: false)
                case final permissions when permissions.isNotEmpty)
              Card(
                margin: EdgeInsets.only(bottom: 12),
                clipBehavior: Clip.antiAlias,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Padding(
                      padding: EdgeInsets.fromLTRB(16, 13, 16, 2),
                      child: Text(group,
                          style: Theme.of(context).textTheme.titleMedium),
                    ),
                    for (final permission in permissions)
                      SwitchListTile(
                          contentPadding: EdgeInsets.symmetric(horizontal: 16),
                          title: Text(permission.label),
                          subtitle: Text(rolePermissionCanChange(
                                  widget.heldPermissions, permission.bit)
                              ? permission.description
                              : L10n.of(context)
                                  .ui_value0_you_can_only_change_permissions_you_cu_a77e0a05(
                                      (permission.description).toString())),
                          value: _permissions & BigInt.from(permission.bit) !=
                              BigInt.zero,
                          onChanged: !rolePermissionCanChange(
                                  widget.heldPermissions, permission.bit)
                              ? null
                              : (value) => setState(() {
                                    final bit = BigInt.from(permission.bit);
                                    value
                                        ? _permissions |= bit
                                        : _permissions &= ~bit;
                                  })),
                  ],
                ),
              ),
          SizedBox(height: 90),
        ]),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: _name.text.trim().isEmpty ? null : _submit,
          icon: Icon(
              widget.role == null ? Icons.add_rounded : Icons.save_outlined),
          label: Text(widget.role == null
              ? L10n.of(context).ui_create_role_b4d57cd3
              : L10n.of(context).ui_save_role_c76e36e8),
        ));
  }

  void _submit() {
    if (!rolePermissionChangesWithinCeiling(
      widget.role?.permissions ?? BigInt.zero,
      _permissions,
      widget.heldPermissions,
    )) {
      _tabError(context, 'Could not update role',
          'You can only change permissions you currently hold.');
      return;
    }
    Navigator.pop(
      context,
      _RoleDraft({
        'name': _name.text.trim(),
        'color': _color,
        'permissions': '$_permissions',
        'hoist': _hoist,
        'mentionable': _mentionable
      }, iconFile: _iconFile, removeIcon: _removeIcon),
    );
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
          title: Text(L10n.of(context).ui_roles_for_value0_0c4d969a(
              (widget.member.user.name).toString())),
          content: SizedBox(
              width: 420,
              child: ListView(shrinkWrap: true, children: [
                for (final role in widget.roles)
                  CheckboxListTile(
                      title: Text(role.name),
                      value: selected.contains(role.ref.id.value),
                      onChanged: role.managed
                          ? null
                          : (value) => setState(() {
                                value == true
                                    ? selected.add(role.ref.id.value)
                                    : selected.remove(role.ref.id.value);
                              }))
              ])),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context),
                child: Text(L10n.of(context).ui_cancel_35afca3b)),
            FilledButton(
                onPressed: () => Navigator.pop(context, selected),
                child: Text(L10n.of(context).ui_save_4d2d5d68))
          ]);
}

final class _PageList extends StatelessWidget {
  const _PageList({required this.children});
  final List<Widget> children;
  @override
  Widget build(BuildContext context) => ColoredBox(
        color: settingsSurface(context),
        child: ListView(padding: EdgeInsets.all(14), children: children),
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
        padding: EdgeInsets.only(bottom: 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SettingsSectionHeader(title, top: 10),
            if (subtitle != null)
              SettingsInfo(subtitle!, padding: EdgeInsets.fromLTRB(4, 0, 4, 12))
            else
              SizedBox(height: 12),
            Padding(
              padding: EdgeInsets.symmetric(horizontal: 4),
              child: child,
            ),
          ],
        ),
      );
}

final class ModerationOptions {
  ModerationOptions({
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
                  SizedBox(height: 12),
                ],
                DropdownButtonFormField<int>(
                  initialValue: duration,
                  decoration: InputDecoration(
                    labelText: timeout
                        ? L10n.of(context).ui_timeout_duration_e54242e8
                        : L10n.of(context).ui_ban_duration_19e60b8e,
                  ),
                  items: timeout
                      ? [
                          DropdownMenuItem(
                              value: 600,
                              child: Text(
                                  L10n.of(context).ui_10_minutes_5da9f7ad)),
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
                              value: -1,
                              child: Text(
                                  L10n.of(context).ui_indefinite_cc65e072)),
                        ]
                      : [
                          DropdownMenuItem(
                              value: 0,
                              child: Text(
                                  L10n.of(context).ui_until_removed_d2938883)),
                          DropdownMenuItem(
                              value: 86400,
                              child: Text(L10n.of(context).ui_1_day_db83588e)),
                          DropdownMenuItem(
                              value: 604800,
                              child: Text(L10n.of(context).ui_7_days_ba4b8279)),
                          DropdownMenuItem(
                              value: 2592000,
                              child:
                                  Text(L10n.of(context).ui_30_days_7a572af9)),
                        ],
                  onChanged: (value) =>
                      setDialogState(() => duration = value ?? duration),
                ),
                if (includeDeleteHistory) ...[
                  SizedBox(height: 12),
                  DropdownButtonFormField<int>(
                    initialValue: deleteSeconds,
                    decoration: InputDecoration(
                        labelText: L10n.of(context)
                            .ui_delete_message_history_8633b9d9),
                    items: [
                      DropdownMenuItem(
                          value: 0,
                          child:
                              Text(L10n.of(context).ui_do_not_delete_a97967f4)),
                      DropdownMenuItem(
                          value: 3600,
                          child: Text(L10n.of(context).ui_past_hour_3ba56051)),
                      DropdownMenuItem(
                          value: 86400,
                          child: Text(L10n.of(context).ui_past_day_65bc6323)),
                      DropdownMenuItem(
                          value: 604800,
                          child:
                              Text(L10n.of(context).ui_past_7_days_8c882b6b)),
                    ],
                    onChanged: (value) => setDialogState(
                        () => deleteSeconds = value ?? deleteSeconds),
                  ),
                ],
                SizedBox(height: 12),
                TextField(
                  controller: reason,
                  maxLength: 512,
                  maxLines: 3,
                  decoration: InputDecoration(
                    labelText:
                        L10n.of(context).ui_audit_reason_optional_2fa64eeb,
                    alignLabelWithHint: true,
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              onPressed: () {
                if (leadingField != null && leadingField.text.trim().isEmpty) {
                  ScaffoldMessenger.of(dialogContext).showSnackBar(
                    SnackBar(
                        content: Text(L10n.of(context).ui_enter_value0_49bbb7b0(
                            (leadingLabel ?? 'a value').toString()))),
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
              child: Text(timeout
                  ? L10n.of(context).ui_apply_timeout_9a1bf1bc
                  : L10n.of(context).ui_ban_bfbc51a6),
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
                      padding: EdgeInsets.only(bottom: 12),
                      child: Text(warning,
                          style: TextStyle(color: context.kaede.warning))),
                TextField(
                    controller: input,
                    autofocus: true,
                    decoration: InputDecoration(labelText: label))
              ]),
              actions: [
                TextButton(
                    onPressed: () => Navigator.pop(context),
                    child: Text(L10n.of(context).ui_cancel_35afca3b)),
                FilledButton(
                    onPressed: () => Navigator.pop(context, input.text.trim()),
                    child: Text(L10n.of(context).ui_continue_ab43d664))
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
                  child: Text(L10n.of(context).ui_cancel_35afca3b)),
              FilledButton(
                  onPressed: () => Navigator.pop(context, true),
                  style: destructive
                      ? FilledButton.styleFrom(
                          backgroundColor: context.kaede.danger)
                      : null,
                  child: Text(destructive
                      ? L10n.of(context).ui_delete_5797ea6a
                      : L10n.of(context).ui_confirm_a183c9ef))
            ])) ??
    false;

void _tabError(BuildContext context, String title, Object error) {
  if (!context.mounted) return;
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(
    content: Text(userFacingError(error, summary: title)),
    backgroundColor: context.kaede.danger,
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
      ChannelType.groupDm => 3,
      ChannelType.voice => 2,
      ChannelType.stage => 13,
      ChannelType.category => 4,
      ChannelType.announcement => 5,
      ChannelType.announcementThread => 10,
      ChannelType.publicThread => 11,
      ChannelType.privateThread => 12,
      ChannelType.forum => 15,
      ChannelType.tracker => 17,
      ChannelType.unknown => 0
    };
