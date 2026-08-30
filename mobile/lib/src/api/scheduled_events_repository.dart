import 'dart:io';

import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/scheduled_events.dart';

extension ScheduledEventsRepository on KaedeRepository {
  String _scheduledEventCollection(EntityRef guild) =>
      '/api/v1/guilds/${guild.wire}/scheduled-events';

  String _scheduledEventPath(EntityRef guild, EntityRef event) =>
      '${_scheduledEventCollection(guild)}/${event.wire}';

  Future<List<GuildScheduledEvent>> scheduledEvents(EntityRef guild) async =>
      (await api.getList(
        _scheduledEventCollection(guild),
        query: const <String, Object?>{'with_user_count': true},
      ))
          .map(GuildScheduledEvent.fromJson)
          .toList(growable: false);

  Future<GuildScheduledEvent> createScheduledEvent(
    EntityRef guild,
    ScheduledEventDraft draft,
  ) async =>
      GuildScheduledEvent.fromJson(await api.sendJson(
        'POST',
        _scheduledEventCollection(guild),
        data: draft.toCreateJson(),
      ));

  Future<GuildScheduledEvent> updateScheduledEvent(
    EntityRef guild,
    GuildScheduledEvent event,
    ScheduledEventDraft draft,
  ) async {
    final patch = draft.patchFor(event);
    if (patch.isEmpty) return event;
    return GuildScheduledEvent.fromJson(await api.sendJson(
      'PATCH',
      _scheduledEventPath(guild, event.ref),
      data: patch,
    ));
  }

  Future<GuildScheduledEvent> transitionScheduledEvent(
    EntityRef guild,
    GuildScheduledEvent event,
    ScheduledEventStatus status,
  ) async =>
      GuildScheduledEvent.fromJson(await api.sendJson(
        'PATCH',
        _scheduledEventPath(guild, event.ref),
        data: <String, Object?>{'status': status.wire},
      ));

  Future<void> deleteScheduledEvent(
    EntityRef guild,
    GuildScheduledEvent event,
  ) =>
      api.sendJson(
        'DELETE',
        _scheduledEventPath(guild, event.ref),
      );

  Future<GuildScheduledEvent> uploadScheduledEventImage({
    required EntityRef guild,
    required GuildScheduledEvent event,
    required String filename,
    required String? contentType,
    required File file,
    void Function(int sent, int total)? onProgress,
    Duration pollInterval = const Duration(seconds: 1),
    int maxPollAttempts = 45,
  }) async {
    final size = await file.length();
    final normalizedContentType = imageUploadContentType(
      filename,
      reportedType: contentType,
    );
    if (size < 1 || size > 10 * 1024 * 1024) {
      throw const UserInputException(
        'Scheduled event cover images must be between 1 byte and 10 MiB.',
      );
    }
    if (normalizedContentType == null) {
      throw const UserInputException(
        'Choose a PNG, JPEG, GIF, or WebP event cover.',
      );
    }
    final path = '${_scheduledEventPath(guild, event.ref)}/image';
    final ticket = await api.sendJson(
      'POST',
      '$path/tickets',
      data: <String, Object?>{
        'filename': filename,
        'content_type': normalizedContentType,
        'size': size,
      },
    );
    final uploadUrl = ticket['upload_url'];
    final attachmentId = ticket['id'];
    if (uploadUrl is! String || attachmentId == null) {
      throw const KaedeException(
        code: 'INVALID_UPLOAD_TICKET',
        message:
            'The server returned an invalid event cover upload authorization.',
        status: 502,
      );
    }
    await api.putPresignedFile(
      uploadUrl,
      file,
      contentType: normalizedContentType,
      onProgress: onProgress,
    );
    return completeScannedMediaResource(
      commit: () => api.sendJson(
        'PUT',
        path,
        data: <String, Object?>{'attachment_id': '$attachmentId'},
      ),
      isComplete: (json) => json['id'] != null && json['image'] is String,
      parse: GuildScheduledEvent.fromJson,
      pollInterval: pollInterval,
      maxPollAttempts: maxPollAttempts,
    );
  }

  Future<GuildScheduledEvent> deleteScheduledEventImage(
    EntityRef guild,
    GuildScheduledEvent event,
  ) async =>
      GuildScheduledEvent.fromJson(await api.sendJson(
        'DELETE',
        '${_scheduledEventPath(guild, event.ref)}/image',
      ));

  Future<List<ScheduledEventSubscriber>> scheduledEventSubscribers(
    EntityRef guild,
    GuildScheduledEvent event, {
    int limit = 100,
    EntityRef? after,
  }) async =>
      (await api.getList(
        '${_scheduledEventPath(guild, event.ref)}/users',
        query: <String, Object?>{
          'limit': limit,
          'with_member': true,
          if (after != null) 'after': after.wire,
        },
      ))
          .map(ScheduledEventSubscriber.fromJson)
          .toList(growable: false);

  Future<void> setScheduledEventSubscription(
    EntityRef guild,
    GuildScheduledEvent event, {
    required bool subscribed,
  }) =>
      api.sendJson(
        subscribed ? 'PUT' : 'DELETE',
        '${_scheduledEventPath(guild, event.ref)}/users/@me',
      );
}
