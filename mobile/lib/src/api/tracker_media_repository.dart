import 'dart:io';

import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/privacy_metadata.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

extension TrackerMediaRepository on KaedeRepository {
  Future<Json> trackerMedia(EntityRef channel, String action, Json data) =>
      api.sendJson('POST',
          '/api/v1/channels/${channel.wire}/tracker/attachments/$action',
          data: data);

  Future<Json> uploadTrackerFile(
    EntityRef channel,
    File file,
    String filename, {
    required String contentType,
    required bool Function() isActive,
    void Function(int, int)? onProgress,
    void Function()? onProcessing,
  }) async {
    void checkActive() {
      if (!isActive()) throw const UserInputException('Upload cancelled.');
    }

    checkActive();
    final prepared = await prepareImageFile(file, contentType);
    try {
      checkActive();
      final ticket = await trackerMedia(channel, 'ticket', {
        'filename': filename,
        'content_type': contentType,
        'size': await prepared.file.length(),
      });
      checkActive();
      final url = ticket['upload_url'];
      final id = ticket['id'];
      final domain = ticket['origin_domain'];
      if (url is! String || id == null || domain is! String) {
        throw const UserInputException(
            'The server returned an invalid upload ticket.');
      }
      await api.putPresignedFile(url, prepared.file, contentType: contentType,
          onProgress: (sent, total) {
        checkActive();
        onProgress?.call(sent, total);
      });
      checkActive();
      onProcessing?.call();
      return await completeScannedMediaResource<Json>(
        commit: () {
          checkActive();
          return trackerMedia(
              channel, 'commit', {'attachment_id': '$id@$domain'});
        },
        isComplete: (json) =>
            json['id'] is String &&
            json['name'] is String &&
            json['type'] is String,
        parse: (json) => json,
      );
    } finally {
      await prepared.dispose();
    }
  }
}
