import 'dart:async';
import 'dart:io';

import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_navigation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:uuid/uuid.dart';

List<String> messageAttachmentIds(Iterable<EntityRef> attachments) =>
    attachments.map((reference) => reference.id.value).toList(growable: false);

/// Completes the two-phase binding required for scanned profile media.
///
/// The first commit queues processing and commonly returns a pending attachment.
/// Once the attachment is clean, the commit must be repeated to bind its digest
/// to the user, guild, or emoji record.
Future<Map<String, Object?>> commitScannedMedia({
  required Future<Map<String, Object?>> Function() commit,
  required Future<Map<String, Object?>> Function() status,
  Duration pollInterval = const Duration(seconds: 1),
  int maxPollAttempts = 30,
}) async {
  if (maxPollAttempts < 1) {
    throw ArgumentError.value(maxPollAttempts, 'maxPollAttempts');
  }
  final initial = await commit();
  final initialStatus = '${initial['scan_status'] ?? 'pending'}';
  if (initialStatus == 'clean') return initial;
  _throwForTerminalMediaStatus(initialStatus);

  for (var attempt = 0; attempt < maxPollAttempts; attempt += 1) {
    final attachment = await status();
    final scanStatus = '${attachment['scan_status'] ?? 'pending'}';
    if (scanStatus == 'clean') return commit();
    _throwForTerminalMediaStatus(scanStatus);
    if (attempt + 1 < maxPollAttempts && pollInterval > Duration.zero) {
      await Future<void>.delayed(pollInterval);
    }
  }
  throw const KaedeException(
    code: 'MEDIA_PROCESSING_TIMEOUT',
    message:
        'Media processing is taking longer than expected. Try again shortly.',
    status: 504,
  );
}

void _throwForTerminalMediaStatus(String status) {
  if (status != 'infected' && status != 'failed') return;
  throw KaedeException(
    code: 'MEDIA_PROCESSING_REJECTED',
    message: status == 'infected'
        ? 'The image did not pass the server’s safety scan. Choose a different file.'
        : 'The server could not process the image. Try another file or try again later.',
    status: 422,
  );
}

final class KaedeRepository {
  const KaedeRepository(this.api);

  final KaedeApiClient api;

  Future<Map<String, Object?>> authConfig(Domain instance) async {
    api.selectInstance(instance);
    return api.getJson('/api/v1/auth/config');
  }

  Future<SessionTokens> login({
    required Domain instance,
    required String identifier,
    required String password,
    String? turnstileToken,
    String? deviceName,
  }) async {
    api.selectInstance(instance);
    final response = await api
        .sendJson('POST', '/api/v1/auth/login', data: <String, Object?>{
      'identifier': identifier,
      'password': password,
      'device_name': deviceName ?? '${Platform.operatingSystem} mobile',
      if (turnstileToken != null) 'turnstile_token': turnstileToken,
    });
    if (response['mfa_required'] == true) {
      throw MfaRequired(response['mfa_ticket']! as String);
    }
    final tokens = SessionTokens(
      instance: instance,
      accessToken: response['access_token']! as String,
      refreshToken: response['refresh_token']! as String,
    );
    await api.useTokens(tokens);
    return tokens;
  }

  Future<SessionTokens> finishMfa(
      Domain instance, String ticket, String code) async {
    api.selectInstance(instance);
    final response =
        await api.sendJson('POST', '/api/v1/auth/mfa', data: <String, Object?>{
      'ticket': ticket,
      'code': code,
      'device_name': '${Platform.operatingSystem} mobile',
    });
    final tokens = SessionTokens(
      instance: instance,
      accessToken: response['access_token']! as String,
      refreshToken: response['refresh_token']! as String,
    );
    await api.useTokens(tokens);
    return tokens;
  }

  Future<Map<String, Object?>> register({
    required Domain instance,
    required String username,
    required String password,
    String? email,
    String? turnstileToken,
  }) async {
    api.selectInstance(instance);
    return api
        .sendJson('POST', '/api/v1/auth/register', data: <String, Object?>{
      'username': username,
      'password': password,
      if (email?.isNotEmpty == true) 'email': email,
      if (turnstileToken != null) 'turnstile_token': turnstileToken,
    });
  }

  Future<void> verifyEmail(String token) => api.sendJson(
        'POST',
        '/api/v1/auth/verify-email',
        data: <String, Object?>{'token': token},
      );

  Future<void> resendVerification(String email) => api.sendJson(
        'POST',
        '/api/v1/auth/verify-email/resend',
        data: <String, Object?>{'email': email},
      );

  Future<void> forgotPassword(String email) => api.sendJson(
        'POST',
        '/api/v1/auth/password/forgot',
        data: <String, Object?>{'email': email},
      );

  Future<void> resetPassword(String token, String password) => api.sendJson(
        'POST',
        '/api/v1/auth/password/reset',
        data: <String, Object?>{'token': token, 'password': password},
      );

  Future<void> requestEmailChange(String email, String password) =>
      api.sendJson(
        'POST',
        '/api/v1/auth/email/change',
        data: <String, Object?>{'email': email, 'password': password},
      );

  Future<void> confirmEmailChange(String token) => api.sendJson(
        'POST',
        '/api/v1/auth/email/change/confirm',
        data: <String, Object?>{'token': token},
      );

  Future<Map<String, Object?>> setupMfa(String password, {String? code}) =>
      api.sendJson(
        'POST',
        '/api/v1/auth/mfa/setup',
        data: <String, Object?>{
          'password': password,
          if (code?.isNotEmpty == true) 'current_code': code,
        },
      );

  Future<Map<String, Object?>> enableMfa(String code) => api.sendJson(
        'POST',
        '/api/v1/auth/mfa/enable',
        data: <String, Object?>{'code': code},
      );

  Future<void> disableMfa(String code, String password) => api.sendJson(
        'POST',
        '/api/v1/auth/mfa/disable',
        data: <String, Object?>{'code': code, 'password': password},
      );

  Future<KaedeUser> me() async {
    final user = KaedeUser.fromJson(await api.getJson('/api/v1/users/@me'));
    if (api.tokens case final tokens?) {
      await api.useTokens(tokens.copyWith(userRef: user.ref));
    }
    return user;
  }

  Future<KaedeUser> lookupUser(String handle) async => KaedeUser.fromJson(
        await api.getJson('/api/v1/users/lookup',
            query: <String, Object?>{'handle': handle}),
      );

  Future<Map<String, Object?>> settings() =>
      api.getJson('/api/v1/users/@me/settings');
  Future<GuildNavigation> guildNavigation() async => GuildNavigation.fromJson(
        await api.getJson('/api/v1/users/@me/guild-navigation'),
      );
  Future<GuildNavigation> updateGuildNavigation(
          GuildNavigation navigation) async =>
      GuildNavigation.fromJson(
        await api.sendJson(
          'PUT',
          '/api/v1/users/@me/guild-navigation',
          data: navigation.toJson(),
        ),
      );
  Future<List<Map<String, Object?>>> readStates() =>
      api.getList('/api/v1/users/@me/read-states');
  Future<Map<String, Object?>> updateSettings(Map<String, Object?> patch) =>
      api.sendJson('PATCH', '/api/v1/users/@me/settings', data: patch);
  Future<KaedeUser> updateProfile(Map<String, Object?> patch) async =>
      KaedeUser.fromJson(
          await api.sendJson('PATCH', '/api/v1/users/@me', data: patch));
  Future<List<Map<String, Object?>>> sessions() =>
      api.getList('/api/v1/auth/sessions');
  Future<void> revokeSession(String id) =>
      api.sendJson('DELETE', '/api/v1/auth/sessions/$id');

  Future<List<KaedeGuild>> guilds() async =>
      (await api.getList('/api/v1/users/@me/guilds'))
          .map(KaedeGuild.fromJson)
          .toList();
  Future<KaedeGuild> guild(EntityRef ref) async =>
      KaedeGuild.fromJson(await api.getJson('/api/v1/guilds/${ref.wire}'));
  Future<GuildSelfModerationStatus> selfModerationStatus(
          EntityRef guild) async =>
      GuildSelfModerationStatus.fromJson(await api.getJson(
          '/api/v1/guilds/${guild.wire}/members/@me/moderation-status'));
  Future<KaedeGuild> createGuild(String name) async =>
      KaedeGuild.fromJson(await api.sendJson('POST', '/api/v1/guilds',
          data: <String, Object?>{'name': name}));
  Future<KaedeGuild> updateGuild(
          EntityRef guild, String version, Map<String, Object?> patch) async =>
      KaedeGuild.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}',
        data: patch,
        headers: <String, String>{'If-Match': version},
      ));
  Future<void> leaveGuild(EntityRef guild) =>
      api.sendJson('DELETE', '/api/v1/guilds/${guild.wire}/members/@me');
  Future<void> deleteGuild(EntityRef guild, String version) => api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}',
        headers: <String, String>{'If-Match': version},
      );
  Future<void> transferGuild(EntityRef guild, EntityRef user, String version) =>
      api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/owner',
        data: <String, Object?>{'owner_id': user.wire},
        headers: <String, String>{'If-Match': version},
      );

  Future<KaedeChannel> createChannel(
          EntityRef guild, Map<String, Object?> request) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/channels',
        data: request,
      ));
  Future<KaedeChannel> updateChannel(
    EntityRef guild,
    EntityRef channel,
    String version,
    Map<String, Object?> patch,
  ) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}',
        data: patch,
        headers: <String, String>{'If-Match': version},
      ));
  Future<void> reorderChannels(
          EntityRef guild, List<Map<String, Object?>> positions) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/channels',
        data: <String, Object?>{'channels': positions},
      );
  Future<void> deleteChannel(
          EntityRef guild, EntityRef channel, String version) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}',
        headers: <String, String>{'If-Match': version},
      );

  Future<List<KaedeMessage>> messages(
    EntityRef channel, {
    EntityRef? before,
    EntityRef? after,
    EntityRef? around,
    int limit = 50,
  }) async {
    final payload = await api.getList(
        '/api/v1/channels/${channel.wire}/messages',
        query: <String, Object?>{
          'limit': limit,
          if (before != null) 'before': before.wire,
          if (after != null) 'after': after.wire,
          if (around != null) 'around': around.wire,
        });
    return payload.map(KaedeMessage.fromJson).toList();
  }

  Future<MessageSearchPage> searchMessages({
    required String query,
    required String scope,
    EntityRef? scopeRef,
    String sort = 'relevance',
    List<EntityRef> authors = const <EntityRef>[],
    List<EntityRef> mentions = const <EntityRef>[],
    List<String> has = const <String>[],
    bool? pinned,
    String? authorType,
    DateTime? before,
    DateTime? after,
    String? cursor,
  }) async =>
      MessageSearchPage.fromJson(await api.sendJson(
        'POST',
        '/api/v1/search/messages',
        data: <String, Object?>{
          'query': query,
          'scope': scope,
          if (scopeRef != null) 'scope_ref': scopeRef.wire,
          'sort': sort,
          if (cursor != null) 'cursor': cursor,
          'limit': 25,
          'filters': <String, Object?>{
            'authors': authors.map((item) => item.wire).toList(),
            'mentions': mentions.map((item) => item.wire).toList(),
            'has': has,
            if (pinned != null) 'pinned': pinned,
            if (authorType != null) 'author_type': authorType,
            if (before != null) 'before': before.toUtc().toIso8601String(),
            if (after != null) 'after': after.toUtc().toIso8601String(),
          },
        },
      ));

  Future<Map<String, Object?>> sendMessage(
    EntityRef channel, {
    String? content,
    List<EntityRef> attachments = const <EntityRef>[],
    EntityRef? replyTo,
    EntityRef? replyAuthor,
    List<EntityRef> mentionUsers = const <EntityRef>[],
    String? nonce,
  }) =>
      api.sendJson('POST', '/api/v1/channels/${channel.wire}/messages',
          data: <String, Object?>{
            if (content?.isNotEmpty == true) 'content': content,
            // Attachments are local to the channel's home instance. Unlike
            // user/message references, MessageCreate expects bare snowflakes.
            'attachment_ids': messageAttachmentIds(attachments),
            if (replyTo != null) 'referenced_message_id': replyTo.wire,
            'mention_user_ids': <String>{
              ...mentionUsers.map((ref) => ref.wire),
              if (replyAuthor != null) replyAuthor.wire,
            }.toList(),
            'client_nonce': nonce ?? const Uuid().v4(),
          });
  Future<KaedeMessage> editMessage(
          EntityRef channel, EntityRef message, String content) async =>
      KaedeMessage.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/channels/${channel.wire}/messages/${message.wire}',
        data: <String, Object?>{'content': content},
      ));
  Future<void> deleteMessage(EntityRef channel, EntityRef message) =>
      api.sendJson('DELETE',
          '/api/v1/channels/${channel.wire}/messages/${message.wire}');
  Future<void> react(EntityRef channel, EntityRef message, String emoji) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/messages/${message.wire}/reactions',
        data: <String, Object?>{'emoji': emoji},
      );
  Future<void> removeReaction(
          EntityRef channel, EntityRef message, String emoji) =>
      api.sendJson('DELETE',
          '/api/v1/channels/${channel.wire}/messages/${message.wire}/reactions/${Uri.encodeComponent(emoji)}');
  Future<void> pin(EntityRef channel, EntityRef message) => api.sendJson(
      'PUT', '/api/v1/channels/${channel.wire}/pins/${message.wire}');
  Future<void> unpin(EntityRef channel, EntityRef message) => api.sendJson(
      'DELETE', '/api/v1/channels/${channel.wire}/pins/${message.wire}');
  Future<List<KaedeMessage>> pins(EntityRef channel) async =>
      (await api.getList('/api/v1/channels/${channel.wire}/pins'))
          .map(KaedeMessage.fromJson)
          .toList();
  Future<void> acknowledge(EntityRef channel, EntityRef message) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/ack',
        data: <String, Object?>{'message_id': message.wire},
      );
  Future<void> typing(EntityRef channel) =>
      api.sendJson('POST', '/api/v1/channels/${channel.wire}/typing');

  Future<List<KaedeChannel>> dms() async =>
      (await api.getList('/api/v1/users/@me/channels'))
          .map(KaedeChannel.fromJson)
          .toList();
  Future<KaedeChannel> openDm(String handle) async => KaedeChannel.fromJson(
        await api.sendJson('POST', '/api/v1/users/@me/channels',
            data: <String, Object?>{'handle': handle}),
      );
  Future<List<Map<String, Object?>>> relationships() =>
      api.getList('/api/v1/users/@me/relationships');
  Future<Map<String, Object?>> requestFriend(String handle) =>
      api.sendJson('POST', '/api/v1/users/@me/relationships',
          data: <String, Object?>{'handle': handle});
  Future<Map<String, Object?>> acceptFriend(EntityRef user) =>
      api.sendJson('PUT', '/api/v1/users/@me/relationships/${user.wire}');
  Future<void> removeRelationship(EntityRef user) =>
      api.sendJson('DELETE', '/api/v1/users/@me/relationships/${user.wire}');
  Future<void> block(EntityRef user) =>
      api.sendJson('PUT', '/api/v1/users/@me/relationships/${user.wire}/block');
  Future<void> unblock(EntityRef user) => api.sendJson(
      'DELETE', '/api/v1/users/@me/relationships/${user.wire}/block');

  Future<List<GuildMember>> members(EntityRef guild,
          {String? query, EntityRef? after}) async =>
      (await api.getList('/api/v1/guilds/${guild.wire}/members',
              query: <String, Object?>{
            'limit': 100,
            if (query?.isNotEmpty == true) 'query': query,
            if (after != null) 'after': after.wire,
          }))
          .map(GuildMember.fromJson)
          .toList();
  Future<void> updateMember(
          EntityRef guild, EntityRef user, Map<String, Object?> patch) =>
      api.sendJson('PATCH', '/api/v1/guilds/${guild.wire}/members/${user.wire}',
          data: patch);
  Future<void> kick(EntityRef guild, EntityRef user, {String? reason}) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}',
        headers: <String, String>{
          if (reason != null) 'X-Audit-Log-Reason': reason
        },
      );
  Future<void> ban(EntityRef guild, EntityRef user,
          {DateTime? expiresAt, String? reason}) =>
      api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/bans/${user.wire}',
        data: <String, Object?>{
          if (reason?.isNotEmpty == true) 'reason': reason,
          if (expiresAt != null)
            'expires_at': expiresAt.toUtc().toIso8601String(),
        },
        headers: <String, String>{
          if (reason != null) 'X-Audit-Log-Reason': reason
        },
      );
  Future<void> unban(EntityRef guild, EntityRef user, {String? reason}) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/bans/${user.wire}',
        headers: <String, String>{
          if (reason != null) 'X-Audit-Log-Reason': reason
        },
      );
  Future<List<Map<String, Object?>>> bans(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/bans');
  Future<List<Map<String, Object?>>> instanceBans(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/instance-bans');
  Future<void> banInstance(EntityRef guild, Domain domain,
          {DateTime? expiresAt, String? reason}) =>
      api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/instance-bans/${domain.value}',
        data: <String, Object?>{
          if (reason?.isNotEmpty == true) 'reason': reason,
          if (expiresAt != null)
            'expires_at': expiresAt.toUtc().toIso8601String(),
        },
        headers: <String, String>{
          if (reason != null) 'X-Audit-Log-Reason': reason
        },
      );
  Future<void> unbanInstance(EntityRef guild, Domain domain,
          {String? reason}) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/instance-bans/${domain.value}',
        headers: <String, String>{
          if (reason != null) 'X-Audit-Log-Reason': reason
        },
      );
  Future<List<Map<String, Object?>>> auditLog(EntityRef guild,
          {String? before}) =>
      api.getList('/api/v1/guilds/${guild.wire}/audit-logs',
          query: <String, Object?>{
            if (before != null) 'before': before,
          });

  Future<KaedeRole> createRole(
          EntityRef guild, Map<String, Object?> request) async =>
      KaedeRole.fromJson(await api.sendJson(
          'POST', '/api/v1/guilds/${guild.wire}/roles',
          data: request));
  Future<KaedeRole> updateRole(
          EntityRef guild, KaedeRole role, Map<String, Object?> patch) async =>
      KaedeRole.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/roles/${role.ref.wire}',
        data: patch,
        headers: <String, String>{
          if (role.version != null) 'If-Match': role.version!
        },
      ));
  Future<void> assignRole(EntityRef guild, EntityRef user, EntityRef role) =>
      api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}/roles/${role.wire}',
      );
  Future<void> removeRole(EntityRef guild, EntityRef user, EntityRef role) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}/roles/${role.wire}',
      );
  Future<GuildMember> replaceMemberRoles(
          EntityRef guild, EntityRef user, Iterable<String> roleIds) async =>
      GuildMember.fromJson(await api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}/roles',
        data: <String, Object?>{'role_ids': roleIds.toList(growable: false)},
      ));
  Future<void> deleteRole(EntityRef guild, KaedeRole role) => api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/roles/${role.ref.wire}',
        headers: <String, String>{
          if (role.version != null) 'If-Match': role.version!
        },
      );
  Future<void> reorderRoles(EntityRef guild, List<KaedeRole> roles) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/roles',
        data: <String, Object?>{
          'roles': [
            for (var index = 0; index < roles.length; index++)
              <String, Object?>{
                'id': roles[index].ref.wire,
                'position': index + 1,
                if (roles[index].version != null)
                  'version': roles[index].version,
              },
          ],
        },
      );
  Future<List<Map<String, Object?>>> overwrites(
          EntityRef guild, EntityRef channel) =>
      api.getList(
          '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/overwrites');
  Future<void> setOverwrite(
          EntityRef guild, EntityRef channel, Map<String, Object?> overwrite) =>
      api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/overwrites',
        data: overwrite,
      );
  Future<void> deleteOverwrite(
          EntityRef guild, EntityRef channel, EntityRef target, String type) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/overwrites/$type/${target.wire}',
      );
  Future<void> syncChannelPermissions(EntityRef guild, EntityRef channel) =>
      api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/permissions/sync',
      );

  Future<List<Map<String, Object?>>> invites(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/invites');
  Future<Map<String, Object?>> createInvite(
          EntityRef guild, Map<String, Object?> request) =>
      api.sendJson('POST', '/api/v1/guilds/${guild.wire}/invites',
          data: request);
  Future<Map<String, Object?>> previewInvite(String code) =>
      api.getJson('/api/v1/invites/$code');
  Future<Map<String, Object?>> acceptInvite(String code) =>
      api.sendJson('POST', '/api/v1/invites/$code');
  Future<void> revokeInvite(String code) =>
      api.sendJson('DELETE', '/api/v1/invites/$code');

  Future<List<Map<String, Object?>>> emojis() =>
      api.getList('/api/v1/users/@me/emojis');
  Future<void> deleteEmoji(EntityRef guild, EntityRef emoji) => api.sendJson(
      'DELETE', '/api/v1/guilds/${guild.wire}/emojis/${emoji.wire}');

  Future<Map<String, Object?>> uploadUserAsset({
    required String kind,
    required String filename,
    required String contentType,
    required File file,
  }) async {
    final path = '/api/v1/users/@me/assets/$kind';
    final size = await file.length();
    final ticket = await api.sendJson('POST', path, data: <String, Object?>{
      'filename': filename,
      'content_type': contentType,
      'size': size,
    });
    await api.putPresignedFile(ticket['upload_url']! as String, file,
        contentType: contentType);
    final attachmentId = '${ticket['id']}';
    return commitScannedMedia(
      commit: () => api.sendJson('PUT', path,
          data: <String, Object?>{'attachment_id': attachmentId}),
      status: () => api.getJson('/api/v1/attachments/$attachmentId'),
    );
  }

  Future<Map<String, Object?>> uploadGuildAsset({
    required EntityRef guild,
    required String kind,
    required String filename,
    required String contentType,
    required File file,
  }) async {
    final path = '/api/v1/guilds/${guild.wire}/assets/$kind';
    final size = await file.length();
    final ticket = await api.sendJson('POST', path, data: <String, Object?>{
      'filename': filename,
      'content_type': contentType,
      'size': size,
    });
    await api.putPresignedFile(ticket['upload_url']! as String, file,
        contentType: contentType);
    final attachmentId = '${ticket['id']}';
    return commitScannedMedia(
      commit: () => api.sendJson('PUT', path,
          data: <String, Object?>{'attachment_id': attachmentId}),
      status: () => api.getJson('/api/v1/attachments/$attachmentId'),
    );
  }

  Future<Map<String, Object?>> uploadEmoji({
    required EntityRef guild,
    required String name,
    required String filename,
    required String contentType,
    required File file,
  }) async {
    final size = await file.length();
    final ticket = await api.sendJson(
      'POST',
      '/api/v1/guilds/${guild.wire}/emojis/tickets',
      data: <String, Object?>{
        'filename': filename,
        'content_type': contentType,
        'size': size,
      },
    );
    await api.putPresignedFile(ticket['upload_url']! as String, file,
        contentType: contentType);
    final attachmentId = '${ticket['id']}';
    return commitScannedMedia(
      commit: () => api.sendJson('POST', '/api/v1/guilds/${guild.wire}/emojis',
          data: <String, Object?>{
            'attachment_id': attachmentId,
            'name': name,
          }),
      status: () => api.getJson('/api/v1/attachments/$attachmentId'),
    );
  }

  Future<List<Map<String, Object?>>> webhooks(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/webhooks');
  Future<Map<String, Object?>> createWebhook(
          EntityRef guild, EntityRef channel, String name) =>
      api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/webhooks',
        data: <String, Object?>{'name': name},
      );
  Future<Map<String, Object?>> updateWebhook(
          String id, Map<String, Object?> patch) =>
      api.sendJson('PATCH', '/api/v1/webhooks/$id', data: patch);
  Future<Map<String, Object?>> rotateWebhook(String id) =>
      api.sendJson('POST', '/api/v1/webhooks/$id/rotate');
  Future<void> deleteWebhook(String id) =>
      api.sendJson('DELETE', '/api/v1/webhooks/$id');

  Future<Map<String, Object?>> guildNotificationSettings(EntityRef guild) =>
      api.getJson('/api/v1/guilds/${guild.wire}/notification-settings');
  Future<List<Map<String, Object?>>> guildNotificationSettingsList() =>
      api.getList('/api/v1/users/@me/guild-notification-settings');
  Future<Map<String, Object?>> updateGuildNotificationSettings(
          EntityRef guild, String level) =>
      api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/notification-settings',
        data: <String, Object?>{'level': level},
      );

  Future<Map<String, Object?>> voiceToken(EntityRef channel) =>
      api.sendJson('POST', '/api/v1/channels/${channel.wire}/voice/token');
  Future<Map<String, Object?>> voiceOccupancy(EntityRef channel) =>
      api.getJson('/api/v1/channels/${channel.wire}/voice/occupancy');
  Future<void> updateMemberVoice(
    EntityRef guild,
    EntityRef user, {
    bool? serverMute,
    bool? serverDeaf,
    String? reason,
  }) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}/voice',
        data: <String, Object?>{
          if (serverMute != null) 'server_mute': serverMute,
          if (serverDeaf != null) 'server_deaf': serverDeaf,
        },
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      );
  Future<void> disconnectMemberVoice(
    EntityRef guild,
    EntityRef user, {
    String? reason,
  }) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}/voice',
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      );
  Future<void> moveMemberVoice(
    EntityRef guild,
    EntityRef user,
    EntityRef channel, {
    String? reason,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}/voice/move',
        data: <String, Object?>{'channel_id': channel.wire},
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      );
  Future<Map<String, Object?>> startCall(EntityRef channel) =>
      api.sendJson('POST', '/api/v1/channels/${channel.wire}/calls');
  Future<Map<String, Object?>> callAction(EntityRef call, String action) =>
      api.sendJson('POST', '/api/v1/calls/${call.wire}',
          data: <String, Object?>{'action': action});

  Future<Map<String, Object?>> createAttachmentTicket({
    required EntityRef channel,
    required String filename,
    required String contentType,
    required int size,
  }) =>
      api.sendJson('POST', '/api/v1/channels/${channel.wire}/attachments',
          data: <String, Object?>{
            'filename': filename,
            'content_type': contentType,
            'size': size,
          });

  Future<EntityRef> uploadAttachment({
    required EntityRef channel,
    required String filename,
    required String contentType,
    required List<int> bytes,
  }) async {
    final ticket = await createAttachmentTicket(
      channel: channel,
      filename: filename,
      contentType: contentType,
      size: bytes.length,
    );
    await api.putPresigned(
      ticket['upload_url']! as String,
      bytes,
      contentType: contentType,
    );
    return EntityRef(
      Snowflake('${ticket['id']}'),
      Domain('${ticket['origin_domain']}'),
    );
  }

  Future<EntityRef> uploadAttachmentFile({
    required EntityRef channel,
    required String filename,
    required String contentType,
    required File file,
    void Function(int sent, int total)? onProgress,
  }) async {
    final size = await file.length();
    final ticket = await createAttachmentTicket(
      channel: channel,
      filename: filename,
      contentType: contentType,
      size: size,
    );
    await api.putPresignedFile(
      ticket['upload_url']! as String,
      file,
      contentType: contentType,
      onProgress: onProgress,
    );
    return EntityRef(
      Snowflake('${ticket['id']}'),
      Domain('${ticket['origin_domain']}'),
    );
  }

  Future<List<int>> attachmentBytes(KaedeAttachment attachment) =>
      api.getBytes(attachmentStatusPath(attachment.ref));

  Future<KaedeAttachment> attachmentStatus(KaedeAttachment attachment) async =>
      KaedeAttachment.fromJson(
        await api.getJson(attachmentStatusPath(attachment.ref)),
      );

  Future<File> downloadAttachment(
    KaedeAttachment attachment,
    File destination,
  ) =>
      api.downloadToFile(
        attachmentMediaPath(
          attachment.ref,
          historyMediaUrl: attachment.historyMediaUrl,
        ),
        destination,
      );

  Future<Map<String, Object?>> linkPreview(String url) =>
      api.sendJson('POST', '/api/v1/link-previews',
          data: <String, Object?>{'url': url});
  Future<Map<String, Object?>> gifs({String? query, int page = 1}) =>
      api.getJson('/api/v1/gifs', query: <String, Object?>{
        if (query?.isNotEmpty == true) 'query': query,
        'page': page,
        'limit': 30,
      });

  Future<Map<String, Object?>> registerPushDevice({
    required String installationId,
    required String token,
    required String platform,
    String? deviceName,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/users/@me/push-devices',
        data: <String, Object?>{
          'installation_id': installationId,
          'token': token,
          'platform': platform,
          if (deviceName != null) 'device_name': deviceName,
        },
      );

  Future<void> unregisterPushDevice(String deviceId) => api.sendJson(
        'DELETE',
        '/api/v1/users/@me/push-devices/$deviceId',
      );

  Future<void> logout() async {
    try {
      await api.sendJson('POST', '/api/v1/auth/logout');
    } finally {
      await api.clearTokens();
    }
  }
}

final class MfaRequired implements Exception {
  const MfaRequired(this.ticket);
  final String ticket;
}
