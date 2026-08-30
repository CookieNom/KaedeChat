import 'dart:async';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_directory.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/developer_mode.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

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
    !user.isApplication;

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
  const ApplicationTag({super.key, this.compact = false});

  final bool compact;

  @override
  Widget build(BuildContext context) => Semantics(
        label: 'Application account',
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
              'APP',
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
  String? memberOf,
}) =>
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (sheetContext) => UserProfileSheet(
        user: user,
        presence: presence,
        actions: actions,
        memberOf: memberOf,
      ),
    );

final class UserProfileSheet extends StatefulWidget {
  const UserProfileSheet({
    super.key,
    required this.user,
    required this.presence,
    this.actions = const <Widget>[],
    this.memberOf,
    this.applicationLookup,
    this.onAddApplication,
  });

  final KaedeUser user;
  final PresenceStatus presence;
  final List<Widget> actions;
  final String? memberOf;
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
  Widget build(BuildContext context) {
    final user = widget.user;
    final presence = widget.presence;
    final memberOf = widget.memberOf;
    final actions = <Widget>[
      if (_application case final application?)
        FilledButton.icon(
          key: const ValueKey('bot-profile-add-app'),
          onPressed: () => _addApplication(application),
          icon: const Icon(Icons.add_rounded),
          label: const Text('Add App'),
        ),
      ...widget.actions,
    ];
    final banner = publicAssetUri(
      user.ref.domain,
      user.bannerHash,
      variant: 'thumbnail_1024',
    );
    return DraggableScrollableSheet(
      initialChildSize: .68,
      minChildSize: .42,
      maxChildSize: .94,
      expand: false,
      builder: (context, scrollController) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Expanded(
            child: ListView(
              controller: scrollController,
              padding: EdgeInsets.zero,
              children: [
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
                            if (user.isApplication) ...[
                              const SizedBox(width: 7),
                              const ApplicationTag(),
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
                                    : 'Profile unavailable · refreshes '
                                        'automatically',
                                style: TextStyle(
                                  color: context.kaede.muted,
                                  fontSize: 13,
                                ),
                              ),
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
                            label: 'About me',
                            child: Text(
                              user.bio!.trim(),
                              style: TextStyle(height: 1.4),
                            ),
                          ),
                        ],
                        if (_developerMode(context)) ...[
                          SizedBox(height: 10),
                          _ProfileCard(
                            label: 'Developer mode',
                            child: ListTile(
                              contentPadding: EdgeInsets.zero,
                              dense: true,
                              leading: Icon(Icons.badge_outlined),
                              title: Text('Copy user ID'),
                              subtitle: Text(user.ref.wire),
                              onTap: () => copyDeveloperId(
                                context,
                                value: user.ref.wire,
                                label: 'User',
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
            SafeArea(
              top: false,
              child: DecoratedBox(
                decoration: BoxDecoration(
                  border: Border(top: BorderSide(color: context.kaede.border)),
                ),
                child: Padding(
                  padding: EdgeInsets.fromLTRB(16, 12, 16, 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      for (var index = 0; index < actions.length; index++) ...[
                        if (index > 0) SizedBox(height: 8),
                        actions[index],
                      ],
                    ],
                  ),
                ),
              ),
            ),
        ],
      ),
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
