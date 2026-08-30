import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/announcements.dart';
import 'package:kaede_mobile/src/domain/models.dart';

extension AnnouncementRepository on KaedeRepository {
  Future<List<AnnouncementFollow>> announcementFollowers(
    EntityRef sourceChannel,
  ) async {
    final follows =
        (await api.getList('/api/v1/channels/${sourceChannel.wire}/followers'))
            .map(
              (item) => AnnouncementFollow.fromJson(
                item,
                expectedSource: sourceChannel,
              ),
            )
            .toList(growable: false);
    final refs = <EntityRef>{};
    for (var index = 0; index < follows.length; index++) {
      final follow = follows[index];
      if (!follow.active || !refs.add(follow.ref)) {
        throw const FormatException('Announcement follow page is invalid.');
      }
      if (index > 0) {
        final previous = follows[index - 1];
        final idOrder =
            BigInt.parse(previous.id).compareTo(BigInt.parse(follow.id));
        if (idOrder > 0 ||
            idOrder == 0 &&
                previous.ref.domain.value.compareTo(follow.ref.domain.value) >=
                    0) {
          throw const FormatException('Announcement follow page is unordered.');
        }
      }
    }
    return follows;
  }

  Future<AnnouncementFollow> followAnnouncement(
    EntityRef sourceChannel,
    EntityRef targetChannel,
  ) async =>
      AnnouncementFollow.fromJson(
        await api.sendJson(
          'POST',
          '/api/v1/channels/${sourceChannel.wire}/followers',
          data: <String, Object?>{'target_channel_id': targetChannel.wire},
        ),
        expectedSource: sourceChannel,
        expectedTarget: targetChannel,
      );

  Future<void> deleteAnnouncementFollow(
    EntityRef sourceChannel,
    EntityRef followRef,
  ) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/channels/${sourceChannel.wire}/followers/${Uri.encodeComponent(followRef.wire)}',
    );
  }

  Future<KaedeMessage> publishAnnouncement(
    EntityRef sourceChannel,
    EntityRef message,
  ) async {
    final published = KaedeMessage.fromJson(
      await api.sendJson(
        'POST',
        '/api/v1/channels/${sourceChannel.wire}/messages/${message.wire}/crosspost',
      ),
    );
    if (published.channelRef != sourceChannel ||
        published.ref != message ||
        !isPublishedAnnouncement(published)) {
      throw const FormatException('Announcement publish lineage is invalid.');
    }
    return published;
  }
}
