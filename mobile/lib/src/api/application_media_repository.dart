import 'dart:io';

import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_media.dart';

extension ApplicationMediaRepository on KaedeRepository {
  Future<List<DeveloperApplication>> developerApplications() async =>
      (await api.getList('/api/v1/applications'))
          .map(DeveloperApplication.fromJson)
          .toList(growable: false);

  Future<List<ApplicationAsset>> applicationAssets(
    EntityRef application,
  ) async =>
      (await api.getList('/api/v1/applications/${application.wire}/assets'))
          .map(ApplicationAsset.fromJson)
          .toList(growable: false);

  Future<List<ApplicationEmoji>> applicationEmojis(
    EntityRef application,
  ) async =>
      (await api.getList('/api/v1/applications/${application.wire}/emojis'))
          .map(ApplicationEmoji.fromJson)
          .toList(growable: false);

  Future<ApplicationMediaJson> createApplicationMediaTicket({
    required EntityRef application,
    required String collection,
    required String filename,
    required String contentType,
    required int size,
  }) {
    if (collection != 'assets' && collection != 'emojis') {
      throw ArgumentError.value(collection, 'collection');
    }
    return api.sendJson(
      'POST',
      '/api/v1/applications/${application.wire}/$collection/tickets',
      data: applicationMediaTicketPayload(
        filename: filename,
        contentType: contentType,
        size: size,
      ),
    );
  }

  Future<ApplicationAsset> uploadApplicationAsset({
    required EntityRef application,
    required ApplicationAssetDraft draft,
    required String filename,
    required String contentType,
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
    final validation = applicationImageValidation(
      filename: filename,
      contentType: normalizedContentType,
      size: size,
    );
    if (validation != null) throw UserInputException(validation);
    if (draft.validationMessage case final message?) {
      throw UserInputException(message);
    }
    final ticket = await createApplicationMediaTicket(
      application: application,
      collection: 'assets',
      filename: filename,
      contentType: normalizedContentType!,
      size: size,
    );
    final uploadUrl = ticket['upload_url'];
    final attachmentId = ticket['id'];
    if (uploadUrl is! String || attachmentId == null) {
      throw const KaedeException(
        code: 'INVALID_UPLOAD_TICKET',
        message: 'The server returned an invalid image upload authorization.',
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
        'POST',
        '/api/v1/applications/${application.wire}/assets',
        data: draft.createPayload('$attachmentId'),
      ),
      isComplete: (json) => json['application_ref'] != null,
      parse: ApplicationAsset.fromJson,
      pollInterval: pollInterval,
      maxPollAttempts: maxPollAttempts,
    );
  }

  Future<ApplicationEmoji> uploadApplicationEmoji({
    required EntityRef application,
    required ApplicationEmojiDraft draft,
    required String filename,
    required String contentType,
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
    final validation = applicationImageValidation(
      filename: filename,
      contentType: normalizedContentType,
      size: size,
    );
    if (validation != null) throw UserInputException(validation);
    if (draft.validationMessage case final message?) {
      throw UserInputException(message);
    }
    final ticket = await createApplicationMediaTicket(
      application: application,
      collection: 'emojis',
      filename: filename,
      contentType: normalizedContentType!,
      size: size,
    );
    final uploadUrl = ticket['upload_url'];
    final attachmentId = ticket['id'];
    if (uploadUrl is! String || attachmentId == null) {
      throw const KaedeException(
        code: 'INVALID_UPLOAD_TICKET',
        message: 'The server returned an invalid image upload authorization.',
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
        'POST',
        '/api/v1/applications/${application.wire}/emojis',
        data: draft.createPayload('$attachmentId'),
      ),
      isComplete: (json) => json['application_ref'] != null,
      parse: ApplicationEmoji.fromJson,
      pollInterval: pollInterval,
      maxPollAttempts: maxPollAttempts,
    );
  }

  Future<ApplicationAsset> updateApplicationAsset(
    EntityRef application,
    Snowflake asset,
    ApplicationAssetDraft draft,
  ) async {
    if (draft.validationMessage case final message?) {
      throw UserInputException(message);
    }
    return ApplicationAsset.fromJson(await api.sendJson(
      'PATCH',
      '/api/v1/applications/${application.wire}/assets/${asset.value}',
      data: draft.patchPayload,
    ));
  }

  Future<void> deleteApplicationAsset(
    EntityRef application,
    Snowflake asset,
  ) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/applications/${application.wire}/assets/${asset.value}',
    );
  }

  Future<ApplicationEmoji> updateApplicationEmoji(
    EntityRef application,
    Snowflake emoji,
    ApplicationEmojiDraft draft,
  ) async {
    if (draft.validationMessage case final message?) {
      throw UserInputException(message);
    }
    return ApplicationEmoji.fromJson(await api.sendJson(
      'PATCH',
      '/api/v1/applications/${application.wire}/emojis/${emoji.value}',
      data: draft.patchPayload,
    ));
  }

  Future<void> deleteApplicationEmoji(
    EntityRef application,
    Snowflake emoji,
  ) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/applications/${application.wire}/emojis/${emoji.value}',
    );
  }
}
