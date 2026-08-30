import 'dart:io';

import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/network_json.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_admin.dart';

extension GuildAdminRepository on KaedeRepository {
  static String _webhookPath(EntityRef webhook, [String suffix = '']) =>
      '/api/v1/webhooks/${webhook.pathSegment}$suffix';

  static Map<String, Object?> _webhookGuildQuery(EntityRef guild) =>
      <String, Object?>{'guild_ref': guild.wire};

  Future<Map<String, Object?>> updateWebhook(
    EntityRef guild,
    EntityRef webhook,
    Map<String, Object?> patch,
  ) =>
      api.sendJson(
        'PATCH',
        _webhookPath(webhook),
        query: _webhookGuildQuery(guild),
        data: patch,
      );

  Future<Map<String, Object?>> rotateWebhook(
    EntityRef guild,
    EntityRef webhook,
  ) =>
      api.sendJson(
        'POST',
        _webhookPath(webhook, '/rotate'),
        query: _webhookGuildQuery(guild),
      );

  Future<void> deleteWebhook(EntityRef guild, EntityRef webhook) =>
      api.sendJson(
        'DELETE',
        _webhookPath(webhook),
        query: _webhookGuildQuery(guild),
      );

  Future<Map<String, Object?>> createWebhookAvatarTicket({
    required EntityRef guild,
    required EntityRef webhook,
    required String filename,
    required String contentType,
    required int size,
  }) =>
      api.sendJson(
        'POST',
        _webhookPath(webhook, '/avatar/tickets'),
        query: _webhookGuildQuery(guild),
        data: <String, Object?>{
          'filename': filename,
          'content_type': contentType,
          'size': size,
        },
      );

  Future<Map<String, Object?>> commitWebhookAvatar(
    EntityRef guild,
    EntityRef webhook,
    String attachmentId,
  ) =>
      api.sendJson(
        'PUT',
        _webhookPath(webhook, '/avatar'),
        query: _webhookGuildQuery(guild),
        data: <String, Object?>{'attachment_id': attachmentId},
      );

  Future<Map<String, Object?>> clearWebhookAvatar(
    EntityRef guild,
    EntityRef webhook,
  ) =>
      api.sendJson(
        'DELETE',
        _webhookPath(webhook, '/avatar'),
        query: _webhookGuildQuery(guild),
      );

  Future<Map<String, Object?>> uploadWebhookAvatar({
    required EntityRef guild,
    required EntityRef webhook,
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
    if (size < 1) {
      throw const UserInputException('The selected webhook avatar is empty.');
    }
    if (normalizedContentType == null) {
      throw const UserInputException(
        'Choose a PNG, JPEG, GIF, or WebP webhook avatar.',
      );
    }
    final ticket = await createWebhookAvatarTicket(
      guild: guild,
      webhook: webhook,
      filename: filename,
      contentType: normalizedContentType,
      size: size,
    );
    final attachmentId = ticket['id'];
    final uploadUrl = ticket['upload_url'];
    if (attachmentId == null || uploadUrl is! String) {
      throw const KaedeException(
        code: 'INVALID_UPLOAD_TICKET',
        message:
            'The server returned an invalid webhook avatar upload authorization.',
        status: 502,
      );
    }
    await api.putPresignedFile(
      uploadUrl,
      file,
      contentType: normalizedContentType,
      onProgress: onProgress,
    );
    return completeScannedMediaResource<Map<String, Object?>>(
      commit: () => commitWebhookAvatar(guild, webhook, '$attachmentId'),
      isComplete: (json) =>
          json['channel_id'] != null && json['avatar_hash'] is String,
      parse: (json) => json,
      pollInterval: pollInterval,
      maxPollAttempts: maxPollAttempts,
    );
  }

  Future<List<AutoModRule>> autoModRules(EntityRef guild) async =>
      (await api.getList(
        '/api/v1/guilds/${guild.wire}/auto-moderation/rules',
      ))
          .map(AutoModRule.fromJson)
          .toList(growable: false);

  Future<AutoModRule> createAutoModRule(
    EntityRef guild,
    AutoModRuleDraft draft, {
    String? reason,
  }) async =>
      AutoModRule.fromJson(await api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/auto-moderation/rules',
        data: draft.toJson(),
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      ));

  Future<AutoModRule> updateAutoModRule(
    EntityRef guild,
    EntityRef rule,
    AutoModRuleDraft draft, {
    String? reason,
  }) async =>
      AutoModRule.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/auto-moderation/rules/${rule.id.value}',
        data: draft.toJson(),
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      ));

  Future<void> deleteAutoModRule(
    EntityRef guild,
    EntityRef rule, {
    String? reason,
  }) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/auto-moderation/rules/${rule.id.value}',
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      );

  Future<int> estimatePrune(
    EntityRef guild, {
    required int days,
    Iterable<EntityRef> includeRoles = const [],
  }) async {
    final response = await api.getJson(
      '/api/v1/guilds/${guild.wire}/prune/estimate',
      query: <String, Object?>{
        'days': days,
        if (includeRoles.isNotEmpty)
          'include_roles': includeRoles.map((item) => item.wire).toList(),
      },
    );
    return (response['pruned'] as num?)?.toInt() ?? 0;
  }

  Future<PruneResult> pruneMembers(
    EntityRef guild, {
    required int days,
    Iterable<EntityRef> includeRoles = const [],
    bool computePruneCount = true,
    String? reason,
  }) async =>
      PruneResult.fromJson(
        await api.sendJson(
          'POST',
          '/api/v1/guilds/${guild.wire}/prune',
          data: <String, Object?>{
            'days': days,
            'include_roles': includeRoles.map((item) => item.wire).toList(),
            'compute_prune_count': computePruneCount,
          },
          headers: <String, String>{
            if (reason?.trim().isNotEmpty == true)
              'X-Audit-Log-Reason': reason!.trim(),
          },
        ),
        fallbackDomain: guild.domain,
      );

  Future<BulkBanResult> bulkBanMembers(
    EntityRef guild,
    Iterable<EntityRef> users, {
    int deleteMessageSeconds = 0,
    String? reason,
  }) async =>
      BulkBanResult.fromJson(
        await api.sendJson(
          'POST',
          '/api/v1/guilds/${guild.wire}/bulk-bans',
          data: <String, Object?>{
            'user_ids': users.map((item) => item.wire).toList(),
            'delete_message_seconds': deleteMessageSeconds,
            if (reason?.trim().isNotEmpty == true) 'reason': reason!.trim(),
          },
          headers: <String, String>{
            if (reason?.trim().isNotEmpty == true)
              'X-Audit-Log-Reason': reason!.trim(),
          },
        ),
        fallbackDomain: guild.domain,
      );

  Future<List<Map<String, Object?>>> guildEmojis(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/emojis');

  Future<Map<String, Object?>> updateGuildEmoji(
    EntityRef guild,
    EntityRef emoji,
    Map<String, Object?> patch,
  ) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/emojis/${emoji.id.value}',
        data: patch,
      );

  Future<List<Map<String, Object?>>> guildStickers(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/stickers');

  Future<Map<String, Object?>> updateGuildSticker(
    EntityRef guild,
    EntityRef sticker,
    Map<String, Object?> patch,
  ) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/stickers/${sticker.id.value}',
        data: patch,
      );

  Future<List<SoundboardSound>> soundboardSounds(EntityRef guild) async {
    final response = await api.getJson(
      '/api/v1/guilds/${guild.wire}/soundboard-sounds',
    );
    return strictNetworkObjectList(
      response['items'],
      label: 'Soundboard sounds',
    ).map(SoundboardSound.fromJson).toList(growable: false);
  }

  Future<List<SoundboardSound>> defaultSoundboardSounds() async =>
      (await api.getList('/api/v1/soundboard-default-sounds'))
          .map(SoundboardSound.fromJson)
          .toList(growable: false);

  Future<SoundboardSound> uploadSoundboardSound({
    required EntityRef guild,
    required String name,
    required String filename,
    required String contentType,
    required File file,
    double volume = 1,
    EntityRef? emojiRef,
    String? emojiName,
  }) async {
    final size = await file.length();
    if (size < 1 || size > 512 * 1024) {
      throw ArgumentError.value(
        size,
        'file',
        'Soundboard audio must be between 1 byte and 512 KiB.',
      );
    }
    if (contentType != 'audio/mpeg' && contentType != 'audio/ogg') {
      throw ArgumentError.value(
        contentType,
        'contentType',
        'Choose an MP3 or Ogg soundboard file.',
      );
    }
    if (emojiRef != null && emojiName?.trim().isNotEmpty == true) {
      throw const UserInputException(
        'Choose either a custom guild emoji or a Unicode emoji, not both.',
      );
    }
    if (emojiRef != null && emojiRef.domain != guild.domain) {
      throw const UserInputException(
        'The selected custom emoji does not belong to this guild.',
      );
    }
    final path = '/api/v1/guilds/${guild.wire}/soundboard-sounds';
    final ticket = await api.sendJson(
      'POST',
      '$path/tickets',
      data: <String, Object?>{
        'filename': filename,
        'content_type': contentType,
        'size': size,
        'encryption_mode': 'plaintext',
      },
    );
    final uploadUrl = ticket['upload_url'];
    final rawAttachmentId = ticket['id'];
    if (uploadUrl is! String || rawAttachmentId == null) {
      throw const KaedeException(
        code: 'INVALID_UPLOAD_TICKET',
        message:
            'The server returned an invalid soundboard upload authorization.',
        status: 502,
      );
    }
    await api.putPresignedFile(
      uploadUrl,
      file,
      contentType: contentType,
    );
    final attachmentId = '$rawAttachmentId';
    final result = await completeScannedMediaResource<Map<String, Object?>>(
      commit: () => api.sendJson(
        'POST',
        path,
        data: <String, Object?>{
          'attachment_id': attachmentId,
          'name': name.trim(),
          'volume': volume.clamp(0, 1),
          if (emojiRef != null) 'emoji_id': emojiRef.id.value,
          if (emojiName?.trim().isNotEmpty == true)
            'emoji_name': emojiName!.trim(),
        },
      ),
      isComplete: (json) =>
          json['guild_id'] != null && json['media_hash'] != null,
      parse: (json) => json,
    );
    return SoundboardSound.fromJson(result);
  }

  Future<SoundboardSound> updateSoundboardSound(
    EntityRef guild,
    EntityRef sound,
    Map<String, Object?> patch,
  ) async =>
      SoundboardSound.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/soundboard-sounds/${sound.wire}',
        data: patch,
      ));

  Future<void> deleteSoundboardSound(EntityRef guild, EntityRef sound) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/soundboard-sounds/${sound.wire}',
      );

  Future<void> playSoundboardSound(
    EntityRef channel,
    EntityRef sound,
    EntityRef? sourceGuild, {
    required int soundVersion,
    double? volume,
  }) async {
    await api.sendJson(
      'POST',
      '/api/v1/channels/${channel.wire}/send-soundboard-sound',
      data: <String, Object?>{
        'sound_id': sound.wire,
        'sound_version': '$soundVersion',
        if (sourceGuild != null) 'source_guild_id': sourceGuild.wire,
        if (volume != null) 'volume': volume.clamp(0, 1),
      },
    );
  }
}
