import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

/// Wording shared by the member list, profile sheets and the account bar.
String presenceLabel(PresenceStatus status) => switch (status) {
      PresenceStatus.online => 'Online',
      PresenceStatus.idle => 'Idle',
      PresenceStatus.dnd => 'Do not disturb',
      PresenceStatus.invisible => 'Invisible',
      PresenceStatus.offline => 'Offline',
    };

Color presenceColor(PresenceStatus status) => switch (status) {
      PresenceStatus.online => KaedeColors.mint,
      PresenceStatus.idle => KaedeColors.warning,
      PresenceStatus.dnd => KaedeColors.danger,
      PresenceStatus.invisible || PresenceStatus.offline => KaedeColors.muted,
    };

IconData presenceIcon(PresenceStatus status) => switch (status) {
      PresenceStatus.online => Icons.circle,
      PresenceStatus.idle => Icons.nightlight_round,
      PresenceStatus.dnd => Icons.do_not_disturb_on_rounded,
      PresenceStatus.invisible => Icons.radio_button_unchecked_rounded,
      PresenceStatus.offline => Icons.circle_outlined,
    };

/// Round avatar with an optional presence dot punched out of its edge, the way
/// the web client renders it.
final class UserAvatar extends StatelessWidget {
  const UserAvatar({
    super.key,
    required this.user,
    this.radius = 22,
    this.presence,
    this.ringColor = KaedeColors.sidebar,
  });

  final KaedeUser user;
  final double radius;

  /// When set, a status dot is drawn over the bottom-right of the avatar.
  final PresenceStatus? presence;

  /// Colour behind the presence dot, normally the surface the avatar sits on.
  final Color ringColor;

  @override
  Widget build(BuildContext context) {
    final url = publicAssetUri(
      user.ref.domain,
      user.avatarHash,
      variant: 'thumbnail_128',
    );
    final avatar = CircleAvatar(
      radius: radius,
      backgroundColor: KaedeColors.raised,
      foregroundColor: KaedeColors.textSoft,
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
              ringColor: ringColor,
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
    this.ringColor = KaedeColors.sidebar,
  });

  final PresenceStatus status;
  final double size;
  final Color ringColor;

  @override
  Widget build(BuildContext context) {
    final idle = status == PresenceStatus.idle;
    final hollow =
        status == PresenceStatus.offline || status == PresenceStatus.invisible;
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: ringColor,
        shape: BoxShape.circle,
      ),
      child: Center(
        child: Container(
          width: size - 4,
          height: size - 4,
          decoration: BoxDecoration(
            color: hollow ? Colors.transparent : presenceColor(status),
            shape: BoxShape.circle,
            border: hollow
                ? Border.all(color: KaedeColors.muted, width: 1.6)
                : null,
          ),
          child: idle
              ? Center(
                  child: Container(
                    width: (size - 4) * .55,
                    height: (size - 4) * .55,
                    decoration: BoxDecoration(
                      color: ringColor,
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
          color: KaedeColors.textSoft,
          letterSpacing: -.3,
        ),
      ),
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(borderRadius ?? size * .3),
      child: ColoredBox(
        color: KaedeColors.raised,
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

final class UserProfileSheet extends StatelessWidget {
  const UserProfileSheet({
    super.key,
    required this.user,
    required this.presence,
    this.actions = const <Widget>[],
    this.memberOf,
  });

  final KaedeUser user;
  final PresenceStatus presence;
  final List<Widget> actions;
  final String? memberOf;

  @override
  Widget build(BuildContext context) {
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
                        const DecoratedBox(
                          decoration: BoxDecoration(
                            gradient: LinearGradient(
                              begin: Alignment.topLeft,
                              end: Alignment.bottomRight,
                              colors: [
                                KaedeColors.coralSoft,
                                KaedeColors.purpleSoft,
                              ],
                            ),
                          ),
                        )
                      else
                        CachedNetworkImage(
                          imageUrl: '$banner',
                          fit: BoxFit.cover,
                          errorWidget: (_, __, ___) => const ColoredBox(
                            color: KaedeColors.coralSoft,
                          ),
                        ),
                      Positioned(
                        top: 8,
                        right: 8,
                        child: Material(
                          color: Colors.black38,
                          shape: const CircleBorder(),
                          child: InkWell(
                            customBorder: const CircleBorder(),
                            onTap: () => Navigator.pop(context),
                            child: const Padding(
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
                  padding: const EdgeInsets.fromLTRB(20, 0, 20, 0),
                  child: Transform.translate(
                    offset: const Offset(0, -34),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Container(
                          padding: const EdgeInsets.all(4),
                          decoration: const BoxDecoration(
                            color: KaedeColors.panel,
                            shape: BoxShape.circle,
                          ),
                          child: UserAvatar(
                            user: user,
                            radius: 36,
                            presence: presence,
                            ringColor: KaedeColors.panel,
                          ),
                        ),
                        const SizedBox(height: 10),
                        Text(
                          user.name,
                          style: Theme.of(context).textTheme.headlineSmall,
                        ),
                        const SizedBox(height: 2),
                        Row(
                          children: [
                            Expanded(
                              child: Text(
                                user.profileResolved
                                    ? user.handle
                                    : 'Profile unavailable · refreshes '
                                        'automatically',
                                style: const TextStyle(
                                  color: KaedeColors.muted,
                                  fontSize: 13,
                                ),
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 8),
                        Row(
                          children: [
                            PresenceDot(
                              status: presence,
                              size: 11,
                              ringColor: KaedeColors.panel,
                            ),
                            const SizedBox(width: 6),
                            Text(
                              presenceLabel(presence),
                              style: const TextStyle(
                                color: KaedeColors.textSoft,
                                fontSize: 12.5,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                            if (memberOf != null) ...[
                              const SizedBox(width: 8),
                              const Text('·',
                                  style: TextStyle(color: KaedeColors.muted)),
                              const SizedBox(width: 8),
                              Expanded(
                                child: Text(
                                  memberOf!,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: KaedeColors.muted,
                                    fontSize: 12.5,
                                  ),
                                ),
                              ),
                            ],
                          ],
                        ),
                        if (user.customStatus?.trim().isNotEmpty == true) ...[
                          const SizedBox(height: 14),
                          _ProfileCard(
                            child: Text(
                              user.customStatus!.trim(),
                              style: const TextStyle(height: 1.35),
                            ),
                          ),
                        ],
                        if (user.bio?.trim().isNotEmpty == true) ...[
                          const SizedBox(height: 10),
                          _ProfileCard(
                            label: 'About me',
                            child: Text(
                              user.bio!.trim(),
                              style: const TextStyle(height: 1.4),
                            ),
                          ),
                        ],
                        const SizedBox(height: 10),
                        _ProfileCard(
                          label: 'Kaede address',
                          child: SelectableText(
                            user.ref.wire,
                            style: const TextStyle(
                              color: KaedeColors.textSoft,
                              fontSize: 12.5,
                            ),
                          ),
                        ),
                        const SizedBox(height: 16),
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
                decoration: const BoxDecoration(
                  border: Border(top: BorderSide(color: KaedeColors.border)),
                ),
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      for (var index = 0; index < actions.length; index++) ...[
                        if (index > 0) const SizedBox(height: 8),
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

final class _ProfileCard extends StatelessWidget {
  const _ProfileCard({required this.child, this.label});

  final Widget child;
  final String? label;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 13),
        decoration: BoxDecoration(
          color: KaedeColors.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          border: Border.all(color: KaedeColors.border),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (label != null) ...[
              Text(
                label!.toUpperCase(),
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 10.5,
                  fontWeight: FontWeight.w800,
                  letterSpacing: .9,
                ),
              ),
              const SizedBox(height: 6),
            ],
            child,
          ],
        ),
      );
}
