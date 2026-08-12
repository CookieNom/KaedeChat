import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class UserAvatar extends StatelessWidget {
  const UserAvatar({super.key, required this.user, this.radius = 22});

  final KaedeUser user;
  final double radius;

  @override
  Widget build(BuildContext context) {
    final url = publicAssetUri(user.ref.domain, user.avatarHash,
        variant: 'thumbnail_128');
    return CircleAvatar(
      radius: radius,
      backgroundColor: KaedeColors.mint,
      foregroundColor: KaedeColors.canvas,
      backgroundImage: url == null ? null : CachedNetworkImageProvider('$url'),
      child: url == null
          ? Text(
              user.name.characters.firstOrNull?.toUpperCase() ?? '?',
              style: TextStyle(
                fontSize: radius * .75,
                fontWeight: FontWeight.w900,
              ),
            )
          : null,
    );
  }
}

final class GuildIcon extends StatelessWidget {
  const GuildIcon({super.key, required this.guild, this.size = 54});

  final KaedeGuild guild;
  final double size;

  @override
  Widget build(BuildContext context) {
    final url = publicAssetUri(guild.ref.domain, guild.iconHash,
        variant: 'thumbnail_128');
    return ClipRRect(
      borderRadius: BorderRadius.circular(size * .3),
      child: ColoredBox(
        color: KaedeColors.raised,
        child: SizedBox.square(
          dimension: size,
          child: url == null
              ? Center(
                  child: Text(
                    guild.name.characters.take(2).toString().toUpperCase(),
                    style: const TextStyle(fontWeight: FontWeight.w900),
                  ),
                )
              : CachedNetworkImage(
                  imageUrl: '$url',
                  fit: BoxFit.cover,
                  errorWidget: (_, __, ___) => Center(
                    child: Text(
                      guild.name.characters.take(2).toString().toUpperCase(),
                      style: const TextStyle(fontWeight: FontWeight.w900),
                    ),
                  ),
                ),
        ),
      ),
    );
  }
}

extension _FirstCharacter on Characters {
  String? get firstOrNull => isEmpty ? null : first;
}

Future<void> showUserProfile(
  BuildContext context,
  KaedeUser user,
  PresenceStatus presence,
) =>
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  UserAvatar(user: user, radius: 36),
                  const SizedBox(width: 16),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(user.name,
                            style: Theme.of(context).textTheme.headlineSmall),
                        Text(
                          user.profileResolved
                              ? user.handle
                              : 'Profile unavailable · refreshes automatically',
                          style: const TextStyle(color: KaedeColors.muted),
                        ),
                        Text(
                          presence == PresenceStatus.dnd
                              ? 'Do not disturb'
                              : presence == PresenceStatus.idle
                                  ? 'Idle'
                                  : presence == PresenceStatus.online
                                      ? 'Online'
                                      : 'Offline',
                          style: const TextStyle(color: KaedeColors.mint),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              if (user.customStatus?.trim().isNotEmpty == true) ...[
                const SizedBox(height: 18),
                Text(user.customStatus!),
              ],
              if (user.bio?.trim().isNotEmpty == true) ...[
                const SizedBox(height: 18),
                const Text('ABOUT ME',
                    style: TextStyle(
                        color: KaedeColors.muted,
                        fontWeight: FontWeight.w800,
                        fontSize: 11)),
                const SizedBox(height: 5),
                Text(user.bio!),
              ],
              const SizedBox(height: 16),
              SelectableText(user.ref.wire,
                  style:
                      const TextStyle(color: KaedeColors.muted, fontSize: 12)),
            ],
          ),
        ),
      ),
    );
