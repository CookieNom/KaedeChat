import 'dart:async';
import 'dart:math' as math;
import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_directory.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/settings/settings_screen.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/developer_mode.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:url_launcher/url_launcher.dart';

/// Wording shared by the member list, profile sheets and the account bar.
String presenceLabel(PresenceStatus status) => switch (status) {
      PresenceStatus.online => 'Online',
      PresenceStatus.idle => 'Idle',
      PresenceStatus.dnd => 'Do not disturb',
      PresenceStatus.invisible => 'Invisible',
      PresenceStatus.offline => 'Offline',
    };

Color presenceColor(BuildContext context, PresenceStatus status) =>
    switch (status) {
      PresenceStatus.online => context.kaede.mint,
      PresenceStatus.idle => context.kaede.warning,
      PresenceStatus.dnd => context.kaede.danger,
      PresenceStatus.invisible || PresenceStatus.offline => context.kaede.muted,
    };

bool userProfileSupportsFriendshipActions(KaedeUser user) =>
    !user.isApplication && !user.isSystem;

IconData presenceIcon(PresenceStatus status) => switch (status) {
      PresenceStatus.online => Icons.circle,
      PresenceStatus.idle => Icons.nightlight_round,
      PresenceStatus.dnd => Icons.do_not_disturb_on_rounded,
      PresenceStatus.invisible => Icons.radio_button_unchecked_rounded,
      PresenceStatus.offline => Icons.circle_outlined,
    };

/// Trusted application-account marker. It is rendered only from the server's
/// account discriminator; display names, nicknames, and webhook labels cannot
/// opt into it.
final class ApplicationTag extends StatelessWidget {
  const ApplicationTag({super.key, this.compact = false, this.system = false});

  final bool compact;
  final bool system;

  @override
  Widget build(BuildContext context) => Semantics(
        label: system
            ? 'Official account of this instance'
            : L10n.of(context).ui_application_account_c4c86f4a,
        child: ExcludeSemantics(
          child: Container(
            padding: EdgeInsets.symmetric(
              horizontal: compact ? 4 : 5,
              vertical: compact ? 1 : 2,
            ),
            decoration: BoxDecoration(
              color: context.kaede.purple,
              borderRadius: BorderRadius.circular(4),
            ),
            child: Text(
              system ? 'SYSTEM' : L10n.of(context).ui_app_61a6c98c,
              style: TextStyle(
                color: context.kaede.onPurple,
                fontSize: compact ? 8 : 9,
                height: 1,
                fontWeight: FontWeight.w800,
                letterSpacing: .3,
              ),
            ),
          ),
        ),
      );
}

/// Round avatar with an optional presence dot punched out of its edge, the way
/// the web client renders it.
final class UserAvatar extends StatelessWidget {
  const UserAvatar({
    super.key,
    required this.user,
    this.radius = 22,
    this.presence,
    this.ringColor,
  });

  final KaedeUser user;
  final double radius;

  /// When set, a status dot is drawn over the bottom-right of the avatar.
  final PresenceStatus? presence;

  /// Colour behind the presence dot, normally the surface the avatar sits on.
  final Color? ringColor;

  @override
  Widget build(BuildContext context) {
    final url = publicAssetUri(
      user.ref.domain,
      user.avatarHash,
      variant: 'thumbnail_128',
    );
    final avatar = CircleAvatar(
      radius: radius,
      backgroundColor: context.kaede.raised,
      foregroundColor: context.kaede.textSoft,
      backgroundImage: url == null ? null : CachedNetworkImageProvider('$url'),
      child: url == null
          ? Text(
              user.name.characters.firstOrNull?.toUpperCase() ?? '?',
              style: TextStyle(
                fontSize: radius * .8,
                fontWeight: FontWeight.w700,
                height: 1,
              ),
            )
          : null,
    );
    final status = presence;
    if (status == null) return avatar;
    final dot = (radius * .62).clamp(9.0, 15.0);
    return SizedBox.square(
      dimension: radius * 2,
      child: Stack(
        children: [
          Positioned.fill(child: avatar),
          Positioned(
            right: -1,
            bottom: -1,
            child: PresenceDot(
              status: status,
              size: dot,
              ringColor: ringColor ?? context.kaede.sidebar,
            ),
          ),
        ],
      ),
    );
  }
}

/// Standalone presence dot, also used next to names outside avatars.
final class PresenceDot extends StatelessWidget {
  const PresenceDot({
    super.key,
    required this.status,
    this.size = 12,
    this.ringColor,
  });

  final PresenceStatus status;
  final double size;
  final Color? ringColor;

  @override
  Widget build(BuildContext context) {
    final idle = status == PresenceStatus.idle;
    final hollow =
        status == PresenceStatus.offline || status == PresenceStatus.invisible;
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: ringColor ?? context.kaede.sidebar,
        shape: BoxShape.circle,
      ),
      child: Center(
        child: Container(
          width: size - 4,
          height: size - 4,
          decoration: BoxDecoration(
            color: hollow ? Colors.transparent : presenceColor(context, status),
            shape: BoxShape.circle,
            border: hollow
                ? Border.all(color: context.kaede.muted, width: 1.6)
                : null,
          ),
          child: idle
              ? Center(
                  child: Container(
                    width: (size - 4) * .55,
                    height: (size - 4) * .55,
                    decoration: BoxDecoration(
                      color: ringColor ?? context.kaede.sidebar,
                      shape: BoxShape.circle,
                    ),
                  ),
                )
              : null,
        ),
      ),
    );
  }
}

final class GuildIcon extends StatelessWidget {
  const GuildIcon({
    super.key,
    required this.guild,
    this.size = 54,
    this.borderRadius,
  });

  final KaedeGuild guild;
  final double size;
  final double? borderRadius;

  @override
  Widget build(BuildContext context) {
    final url = publicAssetUri(
      guild.ref.domain,
      guild.iconHash,
      variant: 'thumbnail_128',
    );
    final initials = guildInitials(guild.name);
    final label = Center(
      child: Text(
        initials,
        style: TextStyle(
          fontWeight: FontWeight.w700,
          fontSize: size * .34,
          color: context.kaede.textSoft,
          letterSpacing: -.3,
        ),
      ),
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(borderRadius ?? size * .3),
      child: ColoredBox(
        color: context.kaede.raised,
        child: SizedBox.square(
          dimension: size,
          child: url == null
              ? label
              : CachedNetworkImage(
                  imageUrl: '$url',
                  fit: BoxFit.cover,
                  placeholder: (_, __) => label,
                  errorWidget: (_, __, ___) => label,
                ),
        ),
      ),
    );
  }
}

/// Conversation artwork shared by forwarding and command destination pickers.
final class ConversationAvatar extends StatelessWidget {
  const ConversationAvatar({
    super.key,
    required this.channel,
    this.guild,
    this.size = 40,
  });

  final KaedeChannel channel;
  final KaedeGuild? guild;
  final double size;

  @override
  Widget build(BuildContext context) {
    final isDm = channel.guildRef == null;
    final group = channel.type == ChannelType.groupDm ||
        channel.conversationType == 'group';
    final recipients = channel.recipients;
    final badge = isDm
        ? (group ? Icons.group_rounded : Icons.chat_bubble_outline_rounded)
        : channel.isThread
            ? Icons.forum_outlined
            : channel.type == ChannelType.voice ||
                    channel.type == ChannelType.stage
                ? Icons.volume_up_rounded
                : Icons.tag_rounded;
    final fallback = CircleAvatar(
      radius: size / 2,
      backgroundColor: context.kaede.raised,
      child: Icon(
          isDm
              ? (group ? Icons.group_rounded : Icons.person_rounded)
              : Icons.dns_rounded,
          size: size * .55),
    );
    return ExcludeSemantics(
      child: SizedBox.square(
        dimension: size + 4,
        child: Stack(
          children: [
            if (!isDm && guild != null)
              GuildIcon(guild: guild!, size: size, borderRadius: size / 2)
            else if (isDm && group && recipients.length >= 2) ...[
              Positioned(
                  left: 0,
                  top: 0,
                  child: UserAvatar(user: recipients[0], radius: size * .35)),
              Positioned(
                  right: 2,
                  bottom: 2,
                  child: UserAvatar(user: recipients[1], radius: size * .35)),
            ] else if (isDm && recipients.isNotEmpty)
              UserAvatar(user: recipients.first, radius: size / 2)
            else
              fallback,
            Positioned(
              right: 0,
              bottom: 0,
              child: Container(
                padding: const EdgeInsets.all(2),
                decoration: BoxDecoration(
                  color: context.kaede.panel,
                  shape: BoxShape.circle,
                ),
                child:
                    Icon(badge, size: size * .4, color: context.kaede.textSoft),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Up to two letters: initials for multi-word names, otherwise the first two
/// characters, so rail icons stay legible at 48px.
String guildInitials(String name) {
  final words = name
      .trim()
      .split(RegExp(r'[\s_-]+'))
      .where((word) => word.isNotEmpty)
      .toList();
  if (words.isEmpty) return '?';
  if (words.length == 1) {
    return words.first.characters.take(2).toString().toUpperCase();
  }
  return (words.first.characters.first + words[1].characters.first)
      .toUpperCase();
}

extension _FirstCharacter on Characters {
  String? get firstOrNull => isEmpty ? null : first;
}

/// The one profile sheet in the app. Callers add whatever actions make sense
/// for where the profile was opened from.
Future<void> showUserProfile(
  BuildContext context,
  KaedeUser user,
  PresenceStatus presence, {
  List<Widget> actions = const <Widget>[],
  bool offerMessage = false,
  VoidCallback? onOpenChat,
  String? memberOf,
  Widget? guildMemberSection,
  EntityRef? guildRef,
  DateTime? joinedAt,
}) {
  final controller = ProviderScope.containerOf(context)
      .read(mobileControllerProvider.notifier);
  final profile = user.profileResolved
      ? controller.repository.lookupUser(user.handle).then((resolved) {
          if (resolved.ref != user.ref) return user;
          return resolved;
        })
      : null;
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (sheetContext) => Consumer(
      builder: (context, ref, child) {
        final state = ref.watch(mobileControllerProvider);
        DateTime? guildJoinedAt = joinedAt;
        for (final member
            in state.guildMembers[guildRef] ?? const <GuildMember>[]) {
          if (member.user.ref == user.ref) {
            guildJoinedAt = member.joinedAt ?? guildJoinedAt;
          }
        }
        return UserProfileSheet(
          user: state.userProfiles[user.ref] ?? user,
          profile: profile,
          presence: state.presenceByUser[user.ref] ?? presence,
          actions: actions,
          viewer: state.user?.ref,
          offerMessage: offerMessage || onOpenChat != null,
          onOpenChat: onOpenChat,
          memberOf: memberOf,
          guildMemberSection: guildMemberSection,
          joinedAt: guildJoinedAt,
          friendsSince: guildRef == null &&
                  memberOf == null &&
                  user.ref != state.user?.ref
              ? friendshipStartedAt(user, state.relationships)
              : null,
        );
      },
    ),
  );
}

final class UserProfileSheet extends StatefulWidget {
  const UserProfileSheet({
    super.key,
    required this.user,
    required this.presence,
    this.profile,
    this.actions = const <Widget>[],
    this.viewer,
    this.offerMessage = false,
    this.onOpenChat,
    this.memberOf,
    this.guildMemberSection,
    this.joinedAt,
    this.friendsSince,
    this.applicationLookup,
    this.onAddApplication,
  });

  final KaedeUser user;
  final Future<KaedeUser>? profile;
  final PresenceStatus presence;
  final List<Widget> actions;
  final EntityRef? viewer;
  final bool offerMessage;
  final VoidCallback? onOpenChat;
  final String? memberOf;
  final Widget? guildMemberSection;
  final DateTime? joinedAt;
  final DateTime? friendsSince;
  final Future<MobileBotProfileApplication?> Function(EntityRef bot)?
      applicationLookup;
  final ValueChanged<MobileBotProfileApplication>? onAddApplication;

  @override
  State<UserProfileSheet> createState() => _UserProfileSheetState();
}

final class _UserProfileSheetState extends State<UserProfileSheet> {
  MobileBotProfileApplication? _application;

  @override
  void initState() {
    super.initState();
    if (widget.user.isApplication) {
      WidgetsBinding.instance.addPostFrameCallback(
        (_) => unawaited(_loadApplication()),
      );
    }
  }

  Future<void> _loadApplication() async {
    try {
      final application =
          await (widget.applicationLookup?.call(widget.user.ref) ??
              ProviderScope.containerOf(context)
                  .read(mobileControllerProvider.notifier)
                  .repository
                  .botProfileApplication(widget.user.ref));
      if (mounted && widget.user.isApplication) {
        setState(() => _application = application);
      }
    } on Object {
      // Bot profiles remain fully usable when Add App discovery is unavailable.
    }
  }

  Future<void> _viewApplication(MobileBotProfileApplication application) async {
    final home = ProviderScope.containerOf(context)
        .read(mobileControllerProvider.notifier)
        .api
        .tokens
        ?.instance;
    if (home == null || !application.directoryListed) return;
    final uri = Uri.https(
        home.value, '/application-directory/${application.application.wire}');
    if (!await launchUrl(uri, mode: LaunchMode.externalApplication) &&
        mounted) {
      showActionFeedback(
          context, L10n.of(context).ui_app_details_are_unavailable_b97be5ac,
          error: true);
    }
  }

  void _addApplication(MobileBotProfileApplication application) {
    if (widget.onAddApplication case final callback?) {
      callback(application);
      return;
    }
    try {
      final controller = ProviderScope.containerOf(context)
          .read(mobileControllerProvider.notifier);
      final home = controller.api.tokens?.instance;
      if (home == null) return;
      final router = GoRouter.of(context);
      Navigator.pop(context);
      unawaited(
        router.push<void>(mobileBotApplicationInstallPath(application, home)),
      );
    } on StateError {
      // Standalone profile previews may not have an authenticated app router.
    }
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<KaedeUser>(
        future: widget.profile,
        builder: (context, snapshot) => _buildProfile(
          context,
          snapshot.data ?? widget.user,
          loading: snapshot.connectionState == ConnectionState.waiting,
          failed: snapshot.hasError,
        ),
      );

  Widget _buildProfile(BuildContext context, KaedeUser user,
      {required bool loading, required bool failed}) {
    final presence = widget.presence;
    final memberOf = widget.memberOf;
    final createdAt = user.createdAt;
    final membershipDate = widget.joinedAt ?? widget.friendsSince;
    final friendsSince = widget.friendsSince;
    final friendshipDays = friendsSince == null
        ? 0
        : math.max(0, DateTime.now().difference(friendsSince).inDays);
    final l10n = L10n.of(context);
    Widget dateEntry(String label, DateTime date, {String? duration}) => Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label,
                style: TextStyle(color: context.kaede.muted, fontSize: 12)),
            const SizedBox(height: 4),
            Text(MaterialLocalizations.of(context)
                .formatShortDate(date.toLocal())),
            if (duration != null) ...[
              const SizedBox(height: 4),
              Text(duration,
                  style: TextStyle(color: context.kaede.muted, fontSize: 12)),
            ],
          ],
        );
    final actions = <Widget>[
      if (_application case final application?) ...[
        ActionButton(
          key: const ValueKey('bot-profile-add-app'),
          onPressed: () => _addApplication(application),
          icon: const Icon(Icons.add_rounded),
          label: Text(
              application.installTemplate.installTypes.contains('guild_install')
                  ? l10n.ui_add_bot_to_a_guild_f3f28629
                  : l10n.ui_add_app_57813d5f),
        ),
        if (application.directoryListed)
          ActionButton(
            kind: ActionButtonKind.outlined,
            onPressed: () => _viewApplication(application),
            icon: const Icon(Icons.open_in_new_rounded),
            label: Text(l10n.ui_view_app_profile_d6423864),
          ),
      ],
      ...widget.actions,
      if (widget.viewer != null && widget.viewer == user.ref)
        ActionButton(
          icon: const Icon(Icons.edit_outlined),
          label: Text(l10n.profile_edit),
          onPressed: () {
            final navigator = Navigator.of(context);
            navigator.pop();
            navigator.push(MaterialPageRoute<void>(
              builder: (context) => Scaffold(
                appBar: AppBar(title: Text(L10n.of(context).profile_edit)),
                body: const SafeArea(child: SettingsScreen()),
              ),
            ));
          },
        )
      else if (widget.viewer != null && user.profileResolved && !user.isSystem)
        _ProfileContactActions(
          key: ValueKey(widget.viewer),
          user: user,
          offerMessage: widget.offerMessage,
          onOpenChat: widget.onOpenChat,
        ),
    ];
    final banner = publicAssetUri(
      user.ref.domain,
      user.bannerHash,
      variant: 'thumbnail_1024',
    );
    Widget sheetContent(ScrollController scrollController, double height) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Expanded(
            child: ListView(
              controller: scrollController,
              padding: EdgeInsets.zero,
              children: [
                if (loading) const LinearProgressIndicator(),
                if (failed)
                  Padding(
                    padding: const EdgeInsets.all(16),
                    child: Text(L10n.of(context).profile_load_failed),
                  ),
                SizedBox(
                  height: 124,
                  child: Stack(
                    fit: StackFit.expand,
                    children: [
                      if (banner == null)
                        DecoratedBox(
                          decoration: BoxDecoration(
                            gradient: LinearGradient(
                              begin: Alignment.topLeft,
                              end: Alignment.bottomRight,
                              colors: [
                                context.kaede.coralSoft,
                                context.kaede.purpleSoft,
                              ],
                            ),
                          ),
                        )
                      else
                        CachedNetworkImage(
                          imageUrl: '$banner',
                          fit: BoxFit.cover,
                          errorWidget: (_, __, ___) => ColoredBox(
                            color: context.kaede.coralSoft,
                          ),
                        ),
                      Positioned(
                        top: 8,
                        right: 8,
                        child: Material(
                          color: Colors.black38,
                          shape: CircleBorder(),
                          child: InkWell(
                            customBorder: CircleBorder(),
                            onTap: () => Navigator.pop(context),
                            child: Padding(
                              padding: EdgeInsets.all(6),
                              child: Icon(Icons.close_rounded,
                                  size: 18, color: Colors.white),
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                Padding(
                  padding: EdgeInsets.fromLTRB(20, 0, 20, 0),
                  child: Transform.translate(
                    offset: Offset(0, -34),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Container(
                          padding: EdgeInsets.all(4),
                          decoration: BoxDecoration(
                            color: context.kaede.panel,
                            shape: BoxShape.circle,
                          ),
                          child: UserAvatar(
                            user: user,
                            radius: 36,
                            presence: presence,
                            ringColor: context.kaede.panel,
                          ),
                        ),
                        SizedBox(height: 10),
                        Row(
                          children: [
                            Flexible(
                              child: Text(
                                user.name,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style:
                                    Theme.of(context).textTheme.headlineSmall,
                              ),
                            ),
                            if (user.isApplication || user.isSystem) ...[
                              const SizedBox(width: 7),
                              ApplicationTag(system: user.isSystem),
                            ],
                          ],
                        ),
                        SizedBox(height: 2),
                        Row(
                          children: [
                            Expanded(
                              child: Text(
                                user.profileResolved
                                    ? user.handle
                                    : L10n.of(context)
                                        .ui_profile_unavailable_refreshes_automatically_9ef31289,
                                style: TextStyle(
                                  color: context.kaede.muted,
                                  fontSize: 13,
                                ),
                              ),
                            ),
                            if (user.profileResolved && user.handle.isNotEmpty)
                              ActionButton(
                                kind: ActionButtonKind.icon,
                                icon: const Icon(Icons.copy_outlined, size: 18),
                                tooltip: l10n.profile_copy_username,
                                onPressed: () async {
                                  final handle = user.handle;
                                  await Clipboard.setData(ClipboardData(
                                    text: handle.startsWith('@')
                                        ? handle
                                        : '@$handle',
                                  ));
                                  if (context.mounted) {
                                    showActionFeedback(
                                        context, l10n.profile_username_copied);
                                  }
                                },
                              ),
                          ],
                        ),
                        SizedBox(height: 8),
                        Row(
                          children: [
                            PresenceDot(
                              status: presence,
                              size: 11,
                              ringColor: context.kaede.panel,
                            ),
                            SizedBox(width: 6),
                            Text(
                              presenceLabel(presence),
                              style: TextStyle(
                                color: context.kaede.textSoft,
                                fontSize: 12.5,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                            if (memberOf != null) ...[
                              SizedBox(width: 8),
                              Text('·',
                                  style: TextStyle(color: context.kaede.muted)),
                              SizedBox(width: 8),
                              Expanded(
                                child: Text(
                                  memberOf,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    color: context.kaede.muted,
                                    fontSize: 12.5,
                                  ),
                                ),
                              ),
                            ],
                          ],
                        ),
                        if (user.customStatus?.trim().isNotEmpty == true) ...[
                          SizedBox(height: 14),
                          _ProfileCard(
                            child: Text(
                              user.customStatus!.trim(),
                              style: TextStyle(height: 1.35),
                            ),
                          ),
                        ],
                        if (user.bio?.trim().isNotEmpty == true) ...[
                          SizedBox(height: 10),
                          _ProfileCard(
                            label: L10n.of(context).ui_about_me_683da950,
                            child: Text(
                              user.bio!.trim(),
                              style: TextStyle(height: 1.4),
                            ),
                          ),
                        ],
                        if (createdAt != null || membershipDate != null) ...[
                          const SizedBox(height: 10),
                          _ProfileCard(
                            label: l10n.profile_member_since,
                            child: Wrap(
                              spacing: 24,
                              runSpacing: 12,
                              children: [
                                if (createdAt != null)
                                  dateEntry(
                                      l10n.profile_account_created, createdAt),
                                if (membershipDate != null)
                                  dateEntry(
                                    widget.joinedAt != null
                                        ? l10n.profile_joined_guild
                                        : l10n.profile_friends_since,
                                    membershipDate,
                                    duration: widget.joinedAt == null &&
                                            friendsSince != null
                                        ? l10n.profile_friends_days(
                                            friendshipDays)
                                        : null,
                                  ),
                              ],
                            ),
                          ),
                        ],
                        if (widget.guildMemberSection case final section?)
                          section,
                        if (_developerMode(context)) ...[
                          SizedBox(height: 10),
                          _ProfileCard(
                            label: L10n.of(context).ui_developer_mode_80d8dbd0,
                            child: ListTile(
                              contentPadding: EdgeInsets.zero,
                              dense: true,
                              leading: Icon(Icons.badge_outlined),
                              title: Text(
                                  L10n.of(context).ui_copy_user_id_b36e9128),
                              subtitle: Text(user.ref.wire),
                              onTap: () => copyDeveloperId(
                                context,
                                value: user.ref.wire,
                                label: L10n.of(context).ui_user_e0a63e12,
                              ),
                            ),
                          ),
                        ],
                        SizedBox(height: 16),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
          if (actions.isNotEmpty)
            ConstrainedBox(
              constraints: BoxConstraints(maxHeight: height * .55),
              child: SingleChildScrollView(
                child: SafeArea(
                  top: false,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      border:
                          Border(top: BorderSide(color: context.kaede.border)),
                    ),
                    child: Padding(
                      padding: EdgeInsets.fromLTRB(16, 12, 16, 12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          for (var index = 0;
                              index < actions.length;
                              index++) ...[
                            if (index > 0) SizedBox(height: 8),
                            actions[index],
                          ],
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
        ],
      );
    }

    return DraggableScrollableSheet(
      initialChildSize: .68,
      minChildSize: .42,
      maxChildSize: .94,
      expand: false,
      builder: (context, scrollController) => LayoutBuilder(
        builder: (context, constraints) =>
            sheetContent(scrollController, constraints.maxHeight),
      ),
    );
  }
}

final class _ProfileContactActions extends ConsumerStatefulWidget {
  const _ProfileContactActions({
    super.key,
    required this.user,
    required this.offerMessage,
    this.onOpenChat,
  });

  final KaedeUser user;
  final bool offerMessage;
  final VoidCallback? onOpenChat;

  @override
  ConsumerState<_ProfileContactActions> createState() =>
      _ProfileContactActionsState();
}

final class _ProfileContactActionsState
    extends ConsumerState<_ProfileContactActions> {
  bool _busy = false;

  Future<void> _run(
      String? expectedType, Future<void> Function() action) async {
    if (_busy ||
        ref.read(mobileControllerProvider).relationshipType(widget.user.ref) !=
            expectedType) {
      return;
    }
    setState(() => _busy = true);
    try {
      await action();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _message() async {
    final controller = ref.read(mobileControllerProvider.notifier);
    final account = ref.read(mobileControllerProvider).user?.ref;
    final dm = await controller.repository.openDm(widget.user.handle);
    if (!mounted || account != ref.read(mobileControllerProvider).user?.ref) {
      return;
    }
    await controller.refreshNavigation();
    if (!mounted || account != ref.read(mobileControllerProvider).user?.ref) {
      return;
    }
    await controller.selectDm(dm);
    if (mounted && ModalRoute.of(context)?.isCurrent == true) {
      Navigator.pop(context);
      widget.onOpenChat?.call();
    }
  }

  @override
  Widget build(BuildContext context) {
    final user = widget.user;
    final type = ref.watch(
        mobileControllerProvider.select((s) => s.relationshipType(user.ref)));
    final controller = ref.read(mobileControllerProvider.notifier);
    final l10n = L10n.of(context);
    ActionButton button(
            String label, IconData icon, Future<void> Function() action,
            {ActionButtonKind kind = ActionButtonKind.outlined,
            bool danger = false}) =>
        ActionButton(
          kind: kind,
          style: danger
              ? OutlinedButton.styleFrom(foregroundColor: context.kaede.danger)
              : null,
          icon: Icon(icon),
          label: Text(label),
          onPressed: _busy ? null : () => _run(type, action),
        );
    final actions = <Widget>[
      if (widget.offerMessage && type != 'blocked')
        button(l10n.ui_message_ae0ed984, Icons.chat_bubble_outline_rounded,
            _message,
            kind: ActionButtonKind.filled),
      if (userProfileSupportsFriendshipActions(user)) ...[
        if (type == 'pending_out') Text(l10n.profile_request_sent),
        if (type == 'friend') Text(l10n.ui_friends_63cc7b7a),
        if (type == null)
          button(
              l10n.ui_send_friend_request_719d5c00,
              Icons.person_add_alt_1_rounded,
              () => controller.requestFriend(user.handle),
              kind: ActionButtonKind.filled)
        else if (type == 'pending_in') ...[
          Text(l10n.profile_incoming_friend_request,
              style: Theme.of(context).textTheme.labelLarge),
          Row(children: [
            Expanded(
                child: button(l10n.ui_accept_50b26149, Icons.check_rounded,
                    () => controller.acceptFriend(user.ref),
                    kind: ActionButtonKind.filled)),
            const SizedBox(width: 8),
            Expanded(
                child: button(l10n.ui_decline_58eaef39, Icons.close_rounded,
                    () => controller.removeRelationship(user.ref))),
          ]),
        ] else if (type == 'pending_out')
          button(l10n.ui_cancel_request_28568fb6, Icons.person_remove_outlined,
              () => controller.removeRelationship(user.ref))
        else if (type == 'friend')
          button(
              l10n.ui_remove_friend_1b6055cb,
              Icons.person_remove_alt_1_rounded,
              () => controller.removeRelationship(user.ref),
              danger: true),
      ],
      button(
          type == 'blocked' ? l10n.ui_unblock_987c7d5f : l10n.profile_block,
          type == 'blocked' ? Icons.lock_open_rounded : Icons.block,
          () => controller.setUserBlocked(user, type != 'blocked')),
      if (type != 'blocked')
        Text(l10n.profile_block_help,
            style: Theme.of(context).textTheme.bodySmall),
    ];
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (var i = 0; i < actions.length; i++) ...[
          if (i > 0) const SizedBox(height: 8),
          actions[i],
        ],
      ],
    );
  }
}

bool _developerMode(BuildContext context) {
  try {
    return ProviderScope.containerOf(context)
        .read(mobileControllerProvider)
        .developerMode;
  } on StateError {
    // Reusable profile previews (including golden/widget tests) may be hosted
    // without the signed-in application scope. Technical IDs stay hidden.
    return false;
  }
}

final class _ProfileCard extends StatelessWidget {
  const _ProfileCard({required this.child, this.label});

  final Widget child;
  final String? label;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: EdgeInsets.fromLTRB(14, 12, 14, 13),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          border: Border.all(color: context.kaede.border),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (label != null) ...[
              Text(
                label!.toUpperCase(),
                style: TextStyle(
                  color: context.kaede.muted,
                  fontSize: 10.5,
                  fontWeight: FontWeight.w800,
                  letterSpacing: .9,
                ),
              ),
              SizedBox(height: 6),
            ],
            child,
          ],
        ),
      );
}
