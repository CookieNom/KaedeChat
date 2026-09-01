import 'dart:async';
import 'dart:io';

import 'package:cryptography/cryptography.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/auth/password_kdf.dart';
import 'package:kaede_mobile/src/auth/password_vault.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/network_json.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_command_permissions.dart';
import 'package:kaede_mobile/src/domain/application_directory.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/bot_e2ee_participation.dart';
import 'package:kaede_mobile/src/domain/guild_navigation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/reaction_emoji.dart';
import 'package:kaede_mobile/src/domain/reaction_management.dart';
import 'package:kaede_mobile/src/domain/rich_content.dart';
import 'package:uuid/uuid.dart';

List<String> messageAttachmentIds(Iterable<EntityRef> attachments) =>
    attachments.map((reference) => reference.id.value).toList(growable: false);

final class EncryptedForumThreadReservation {
  const EncryptedForumThreadReservation({
    required this.channel,
    required this.clientNonce,
    required this.claimed,
  });

  final KaedeChannel channel;
  final String clientNonce;
  final bool claimed;
}

Map<String, Object?> interactionRequestData(
  Map<String, Object?> plaintext, {
  Map<String, Object?>? encryptedPayload,
  List<String> attachmentIds = const <String>[],
}) {
  if (encryptedPayload == null) {
    if (attachmentIds.isNotEmpty) {
      throw ArgumentError(
        'Opaque interaction attachments require an encrypted payload.',
      );
    }
    return plaintext;
  }
  return <String, Object?>{
    ...plaintext,
    'options': const <String, Object?>{},
    'values': const <String>[],
    'components': const <Map<String, Object?>>[],
    'encrypted_payload': encryptedPayload,
    'attachment_ids': attachmentIds,
  };
}

({List<KaedeMessage> items, bool hasMore}) parsePinnedMessagePage(
  Map<String, Object?> payload, {
  required EntityRef channel,
  DateTime? before,
}) {
  final rawItems = strictNetworkObjectList(
    payload['items'],
    label: 'Pinned message items',
  );
  if (rawItems.length > 50 || payload['has_more'] is! bool) {
    throw const FormatException('Pinned message page is invalid.');
  }
  final items = <KaedeMessage>[];
  final seen = <EntityRef>{};
  DateTime? previous;
  for (final rawItem in rawItems) {
    final rawPinnedAt = rawItem['pinned_at'];
    final rawMessage = rawItem['message'];
    if (rawPinnedAt is! String || rawMessage is! Map) {
      throw const FormatException('Pinned message entry is invalid.');
    }
    final pinnedAt = DateTime.tryParse(rawPinnedAt);
    if (pinnedAt == null ||
        !rawPinnedAt.contains(RegExp(r'(Z|[+-]\d\d:\d\d)$'))) {
      throw const FormatException('Pinned message timestamp is invalid.');
    }
    final utcPinnedAt = pinnedAt.toUtc();
    if ((before != null && !utcPinnedAt.isBefore(before.toUtc())) ||
        (previous != null && utcPinnedAt.isAfter(previous))) {
      throw const FormatException('Pinned message page is out of order.');
    }
    final normalized = Map<String, Object?>.from(rawMessage);
    normalized['pinned'] = true;
    normalized['pinned_at'] = utcPinnedAt.toIso8601String();
    final message = KaedeMessage.fromJson(normalized);
    if (message.channelRef != channel || !seen.add(message.ref)) {
      throw const FormatException('Pinned message page linkage is invalid.');
    }
    items.add(message);
    previous = utcPinnedAt;
  }
  return (
    items: List<KaedeMessage>.unmodifiable(items),
    hasMore: payload['has_more']! as bool,
  );
}

Map<String, Object?> _applicationCommandLineageData({
  required String integrationType,
  String? dmCapabilityId,
  String? dmCapabilityRevision,
}) {
  final capabilityBound = integrationType == 'dm_capability';
  if (capabilityBound !=
          (dmCapabilityId != null && dmCapabilityRevision != null) ||
      !capabilityBound &&
          (dmCapabilityId != null || dmCapabilityRevision != null) ||
      capabilityBound &&
          (!RegExp(r'^kbdg_[A-Za-z0-9_-]{43}$').hasMatch(dmCapabilityId!) ||
              !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(dmCapabilityRevision!) ||
              BigInt.parse(dmCapabilityRevision) >
                  BigInt.parse('9223372036854775807'))) {
    throw ArgumentError(
      'Bot-DM command capability identity and revision must be supplied together.',
    );
  }
  return <String, Object?>{
    'integration_type': integrationType,
    if (dmCapabilityId != null) 'dm_capability_id': dmCapabilityId,
    if (dmCapabilityRevision != null)
      'dm_capability_revision': dmCapabilityRevision,
  };
}

final class MessageForwardSuccess {
  const MessageForwardSuccess(
      {required this.destination, required this.message});

  final EntityRef destination;
  final KaedeMessage message;
}

final class MessageForwardFailure {
  const MessageForwardFailure({
    required this.destination,
    required this.status,
    required this.error,
  });

  final EntityRef destination;
  final int status;
  final Map<String, Object?> error;
}

final class MessageForwardResult {
  const MessageForwardResult({required this.forwards, required this.failures});

  final List<MessageForwardSuccess> forwards;
  final List<MessageForwardFailure> failures;
}

Map<String, Object?> messageReportRequestData(
  EntityRef message, {
  required String category,
  EntityRef? focusedAttachment,
  String? description,
  String? disclosedContent,
  bool disclosureAcknowledged = false,
}) =>
    <String, Object?>{
      'target_type': 'message',
      'target_ref': message.wire,
      'message_ref': message.wire,
      if (focusedAttachment != null)
        'focused_attachment_ref': focusedAttachment.wire,
      'category': category,
      if (description?.trim().isNotEmpty == true)
        'description': description!.trim(),
      // Preserve exact empty text: it represents successful decryption of an
      // attachment-only E2EE message. Null means decryption was unavailable.
      if (disclosedContent != null) 'disclosed_content': disclosedContent,
      if (disclosedContent != null)
        'disclosure_acknowledged': disclosureAcknowledged,
    };

final RegExp _e2eeRecoveryAuthorizationPattern =
    RegExp(r'^ker_[A-Za-z0-9_-]{43}$');

String e2eeRecoveryAuthorizationFromReset(
  Map<String, Object?> response,
  String accountRef,
) {
  final authorization = response['recovery_authorization'];
  if (response.length != 4 ||
      response['status'] != 'encryption_reset' ||
      response['account_ref'] != accountRef ||
      authorization is! String ||
      !_e2eeRecoveryAuthorizationPattern.hasMatch(authorization) ||
      response['recovery_authorization_expires_in'] != 300) {
    throw const FormatException(
      'The server returned an invalid encryption-reset confirmation.',
    );
  }
  return authorization;
}

final class ReactionUserPage {
  const ReactionUserPage({
    required this.items,
    required this.total,
    required this.nextAfter,
  });

  final List<KaedeUser> items;
  final int total;
  final EntityRef? nextAfter;
}

final class PollVoterPage {
  const PollVoterPage({required this.items, required this.nextAfter});

  final List<KaedeUser> items;
  final EntityRef? nextAfter;
}

/// Completes the two-phase binding required for scanned profile media.
///
/// The first commit queues processing and commonly returns a pending attachment.
/// Once the attachment is clean, the commit must be repeated to bind its digest
/// to the user, guild, or emoji record.
Future<Map<String, Object?>> commitScannedMedia({
  required Future<Map<String, Object?>> Function() commit,
  Duration pollInterval = const Duration(seconds: 1),
  int maxPollAttempts = 30,
}) async {
  if (maxPollAttempts < 1) {
    throw ArgumentError.value(maxPollAttempts, 'maxPollAttempts');
  }
  for (var attempt = 0; attempt < maxPollAttempts; attempt += 1) {
    final result = await commit();
    final rawStatus = result['scan_status'];
    if (rawStatus == null || rawStatus == 'clean') return result;
    final scanStatus = '$rawStatus';
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
  if (status != 'rejected' && status != 'infected' && status != 'failed') {
    return;
  }
  throw KaedeException(
    code: 'MEDIA_PROCESSING_REJECTED',
    message: status == 'rejected' || status == 'infected'
        ? 'The server rejected this image during processing. Choose a different file.'
        : 'The server could not process the image. Try another file or try again later.',
    status: 422,
  );
}

final class KaedeRepository {
  KaedeRepository(
    this.api, {
    this.passwordVault = const MobilePasswordVault(),
  });

  final KaedeApiClient api;
  final MobilePasswordVault passwordVault;
  SecretKeyData? _pendingVaultKey;
  Domain? _pendingVaultInstance;
  String? _pendingE2eeRecoveryAuthorization;

  Future<Map<String, Object?>> authConfig(Domain instance) async {
    api.selectInstance(instance);
    return api.getJson('/api/v1/auth/config');
  }

  Future<MobilePasswordKdfContext> _passwordKdfContext(
    String identifier,
  ) async {
    final response = await api.sendJson(
      'POST',
      '/api/v1/auth/key-derivation',
      data: <String, Object?>{'identifier': identifier},
    );
    return MobilePasswordKdfContext.fromJson(response);
  }

  void _replacePendingVaultKey(SecretKeyData key, Domain instance) {
    _clearPendingVaultKey();
    _pendingVaultKey = key;
    _pendingVaultInstance = instance;
  }

  void _clearPendingVaultKey() {
    _pendingVaultKey?.destroy();
    _pendingVaultKey = null;
    _pendingVaultInstance = null;
  }

  /// Drops password-derived material retained only while an MFA login is in
  /// progress. Call when that flow is cancelled or this repository is disposed.
  void discardPendingPasswordKey() => _clearPendingVaultKey();

  void stageE2eeRecoveryAuthorization(String authorization) {
    if (!_e2eeRecoveryAuthorizationPattern.hasMatch(authorization)) {
      throw const FormatException(
          'The E2EE recovery authorization is invalid.');
    }
    _pendingE2eeRecoveryAuthorization = authorization;
  }

  void discardPendingE2eeRecoveryAuthorization() {
    _pendingE2eeRecoveryAuthorization = null;
  }

  void dispose() => _clearPendingVaultKey();

  Future<void> _persistPendingVaultKey(KaedeUser user) async {
    final key = _pendingVaultKey;
    final instance = _pendingVaultInstance;
    if (key == null || instance == null) return;
    try {
      if (user.ref.domain != instance) {
        throw StateError(
          'The password-derived encryption key belongs to another instance.',
        );
      }
      await passwordVault.write(user.ref.wire, key);
    } finally {
      if (identical(_pendingVaultKey, key)) _clearPendingVaultKey();
    }
  }

  Future<Map<String, Object?>> _currentPasswordPayload(
    String password,
  ) async {
    final user = await me();
    final prepared = await prepareMobilePassword(
      password,
      await _passwordKdfContext(user.username),
      api.tokens?.instance ??
          (throw StateError('No active instance is selected.')),
    );
    try {
      return <String, Object?>{
        'password': prepared.authenticationSecret,
        'password_kdf_version': prepared.context.version,
      };
    } finally {
      prepared.destroy();
    }
  }

  Future<SessionTokens> login({
    required Domain instance,
    required String identifier,
    required String password,
    String? turnstileToken,
    String? deviceName,
  }) async {
    api.selectInstance(instance);
    final prepared = await prepareMobilePassword(
      password,
      await _passwordKdfContext(identifier),
      instance,
    );
    _replacePendingVaultKey(prepared.vaultKey, instance);
    try {
      final response = await api
          .sendJson('POST', '/api/v1/auth/login', data: <String, Object?>{
        'identifier': identifier,
        'password': prepared.authenticationSecret,
        'password_kdf_version': prepared.context.version,
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
    } on MfaRequired {
      rethrow;
    } on Object {
      _clearPendingVaultKey();
      rethrow;
    }
  }

  Future<SessionTokens> finishMfa(
      Domain instance, String ticket, String code) async {
    api.selectInstance(instance);
    try {
      final response = await api
          .sendJson('POST', '/api/v1/auth/mfa', data: <String, Object?>{
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
    } on Object {
      _clearPendingVaultKey();
      rethrow;
    }
  }

  Future<Map<String, Object?>> register({
    required Domain instance,
    required String username,
    required String password,
    String? email,
    String? turnstileToken,
  }) async {
    api.selectInstance(instance);
    final prepared = await prepareMobileRegistrationPassword(
      password,
      instance,
    );
    try {
      final context = prepared.context;
      if (context is! ModernMobilePasswordKdfContext) {
        throw StateError(
            'Registration did not create a modern password context.');
      }
      return api
          .sendJson('POST', '/api/v1/auth/register', data: <String, Object?>{
        'username': username,
        'password': prepared.authenticationSecret,
        'password_kdf': context.toJson(),
        if (email?.isNotEmpty == true) 'email': email,
        if (turnstileToken != null) 'turnstile_token': turnstileToken,
      });
    } finally {
      prepared.destroy();
    }
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

  Future<String> resetPassword(
    Domain instance,
    String token,
    String password,
  ) async {
    api.selectInstance(instance);
    final prepared = await prepareMobileResetPassword(password, instance);
    final response = await api.sendJson(
      'POST',
      '/api/v1/auth/password/reset',
      data: <String, Object?>{
        'token': token,
        'password': prepared.authenticationSecret,
        'password_kdf': prepared.passwordKdf,
      },
    );
    final accountRef = response['account_ref'];
    if (accountRef is! String) {
      throw const FormatException(
        'The server did not confirm which encrypted account was reset.',
      );
    }
    final parsed = EntityRef.parse(accountRef);
    if (parsed.wire != accountRef || parsed.domain != instance) {
      throw const FormatException(
        'The server confirmed a different encrypted account reset.',
      );
    }
    return accountRef;
  }

  Future<void> requestEmailChange(String email, String password) async {
    final passwordPayload = await _currentPasswordPayload(password);
    await api.sendJson(
      'POST',
      '/api/v1/auth/email/change',
      data: <String, Object?>{'email': email, ...passwordPayload},
    );
  }

  Future<void> confirmEmailChange(String token) => api.sendJson(
        'POST',
        '/api/v1/auth/email/change/confirm',
        data: <String, Object?>{'token': token},
      );

  Future<Map<String, Object?>> setupMfa(String password, {String? code}) async {
    final passwordPayload = await _currentPasswordPayload(password);
    return api.sendJson(
      'POST',
      '/api/v1/auth/mfa/setup',
      data: <String, Object?>{
        ...passwordPayload,
        if (code?.isNotEmpty == true) 'current_code': code,
      },
    );
  }

  Future<Map<String, Object?>> enableMfa(String code) => api.sendJson(
        'POST',
        '/api/v1/auth/mfa/enable',
        data: <String, Object?>{'code': code},
      );

  Future<void> disableMfa(String code, String password) async {
    final passwordPayload = await _currentPasswordPayload(password);
    await api.sendJson(
      'POST',
      '/api/v1/auth/mfa/disable',
      data: <String, Object?>{'code': code, ...passwordPayload},
    );
  }

  Future<KaedeUser> me() async {
    final user = KaedeUser.fromJson(await api.getJson('/api/v1/users/@me'));
    if (api.tokens case final tokens?) {
      await api.useTokens(tokens.copyWith(userRef: user.ref));
    }
    await _persistPendingVaultKey(user);
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
  Future<List<VoiceRegion>> voiceRegions(EntityRef guild) async =>
      (await api.getList(
        '/api/v1/voice/regions',
        query: <String, Object?>{'guild_ref': guild.wire},
      ))
          .map(VoiceRegion.fromJson)
          .toList(growable: false);
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
      EntityRef guild, List<Map<String, Object?>> positions) async {
    await api.sendJson(
      'PATCH',
      '/api/v1/guilds/${guild.wire}/channels',
      data: <String, Object?>{'channels': positions},
    );
  }

  Future<void> deleteChannel(
          EntityRef guild, EntityRef channel, String version) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}',
        headers: <String, String>{'If-Match': version},
      );

  Future<KaedeChannel> channel(EntityRef channel) async =>
      KaedeChannel.fromJson(
        await api.getJson('/api/v1/channels/${channel.wire}'),
      );

  Future<TrackerBoard> trackerBoard(EntityRef channel) async =>
      TrackerBoard.fromJson(
        await api.getJson('/api/v1/channels/${channel.wire}/tracker'),
      );

  Future<TrackerBoard> updateTrackerBoard(
    EntityRef channel,
    String version, {
    required String keyPrefix,
  }) async =>
      TrackerBoard.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/channels/${channel.wire}/tracker',
        data: <String, Object?>{'key_prefix': keyPrefix.trim().toUpperCase()},
        headers: <String, String>{'If-Match': version},
      ));

  Future<TrackerLane> createTrackerLane(
    EntityRef channel, {
    required String name,
    int color = 0,
    TrackerLaneKind kind = TrackerLaneKind.custom,
    bool completed = false,
    int? position,
  }) async =>
      TrackerLane.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/tracker/lanes',
        data: <String, Object?>{
          'name': name.trim(),
          'color': color.clamp(0, 0xFFFFFF),
          'kind': kind.wire,
          'completed': completed,
          if (position != null) 'position': position,
        },
      ));

  Future<TrackerLane> updateTrackerLane(
    EntityRef channel,
    EntityRef lane,
    String version, {
    String? name,
    int? color,
    TrackerLaneKind? kind,
    bool? completed,
  }) async =>
      TrackerLane.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/channels/${channel.wire}/tracker/lanes/${lane.wire}',
        data: <String, Object?>{
          if (name != null) 'name': name.trim(),
          if (color != null) 'color': color.clamp(0, 0xFFFFFF),
          if (kind != null) 'kind': kind.wire,
          if (completed != null) 'completed': completed,
        },
        headers: <String, String>{'If-Match': version},
      ));

  Future<TrackerLane> moveTrackerLane(
    EntityRef channel,
    EntityRef lane,
    String version,
    int position,
  ) async =>
      TrackerLane.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/tracker/lanes/${lane.wire}/move',
        data: <String, Object?>{'position': position},
        headers: <String, String>{'If-Match': version},
      ));

  Future<void> deleteTrackerLane(
    EntityRef channel,
    EntityRef lane,
    String version,
  ) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/channels/${channel.wire}/tracker/lanes/${lane.wire}',
      headers: <String, String>{'If-Match': version},
    );
  }

  Future<TrackerTask> createTrackerTask(
    EntityRef channel, {
    required EntityRef lane,
    required String title,
    String? description,
    TrackerPriority priority = TrackerPriority.none,
    int? position,
    DateTime? dueAt,
    EntityRef? assignee,
    String? clientNonce,
  }) async =>
      TrackerTask.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/tracker/tasks',
        data: <String, Object?>{
          'lane_id': lane.wire,
          'title': title.trim(),
          if (description != null) 'description': description,
          'priority': priority.name,
          if (position != null) 'position': position,
          if (dueAt != null) 'due_at': dueAt.toUtc().toIso8601String(),
          if (assignee != null) 'assignee_id': assignee.wire,
          'client_nonce': clientNonce ?? const Uuid().v4(),
        },
      ));

  Future<TrackerTask> updateTrackerTask(
    EntityRef channel,
    EntityRef task,
    String version, {
    String? title,
    String? description,
    bool clearDescription = false,
    TrackerPriority? priority,
    DateTime? dueAt,
    bool clearDueAt = false,
    EntityRef? assignee,
    bool clearAssignee = false,
  }) async =>
      TrackerTask.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/channels/${channel.wire}/tracker/tasks/${task.wire}',
        data: <String, Object?>{
          if (title != null) 'title': title.trim(),
          if (clearDescription)
            'description': null
          else if (description != null)
            'description': description,
          if (priority != null) 'priority': priority.name,
          if (clearDueAt)
            'due_at': null
          else if (dueAt != null)
            'due_at': dueAt.toUtc().toIso8601String(),
          if (clearAssignee)
            'assignee_id': null
          else if (assignee != null)
            'assignee_id': assignee.wire,
        },
        headers: <String, String>{'If-Match': version},
      ));

  Future<TrackerTask> moveTrackerTask(
    EntityRef channel,
    EntityRef task,
    String version, {
    required EntityRef lane,
    required int position,
  }) async =>
      TrackerTask.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/tracker/tasks/${task.wire}/move',
        data: <String, Object?>{
          'lane_id': lane.wire,
          'position': position,
        },
        headers: <String, String>{'If-Match': version},
      ));

  Future<void> deleteTrackerTask(
    EntityRef channel,
    EntityRef task,
    String version,
  ) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/channels/${channel.wire}/tracker/tasks/${task.wire}',
      headers: <String, String>{'If-Match': version},
    );
  }

  Future<KaedeChannel> createThread({
    required EntityRef parent,
    required String name,
    String? content,
    int? type,
    int? autoArchiveDuration,
    int? rateLimitPerUser,
    bool? invitable,
    List<String> appliedTagIds = const <String>[],
    List<EntityRef> attachments = const <EntityRef>[],
    List<EntityRef> mentionUsers = const <EntityRef>[],
    String? nonce,
  }) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${parent.wire}/threads',
        data: <String, Object?>{
          'name': name.trim(),
          if (type != null) 'type': type,
          if (autoArchiveDuration != null)
            'auto_archive_duration': autoArchiveDuration,
          if (rateLimitPerUser != null) 'rate_limit_per_user': rateLimitPerUser,
          if (invitable != null) 'invitable': invitable,
          if (appliedTagIds.isNotEmpty) 'applied_tag_ids': appliedTagIds,
          if (content?.trim().isNotEmpty == true || attachments.isNotEmpty)
            'message': <String, Object?>{
              if (content?.trim().isNotEmpty == true)
                'content': content!.trim(),
              'attachment_ids': messageAttachmentIds(attachments),
              'mention_user_ids': mentionUsers
                  .map((reference) => reference.wire)
                  .toSet()
                  .toList(growable: false),
              'client_nonce': nonce ?? const Uuid().v4(),
            },
        },
      ));

  Future<EncryptedForumThreadReservation> reserveEncryptedForumThread({
    required EntityRef parent,
    required String name,
    required String clientNonce,
    int? autoArchiveDuration,
    List<String> appliedTagIds = const <String>[],
  }) async {
    if (!RegExp(r'^[A-Za-z0-9._:-]{1,64}$').hasMatch(clientNonce)) {
      throw ArgumentError(
          'Encrypted forum starter reservation nonce is invalid.');
    }
    final response = await api.sendJson(
      'POST',
      '/api/v1/channels/${parent.wire}/threads',
      data: <String, Object?>{
        'name': name.trim(),
        if (autoArchiveDuration != null)
          'auto_archive_duration': autoArchiveDuration,
        if (appliedTagIds.isNotEmpty) 'applied_tag_ids': appliedTagIds,
        'starter_reservation_nonce': clientNonce,
      },
    );
    if (response['starter_message'] != null ||
        response['message'] != null ||
        response['starter_reservation'] is! Map) {
      throw const FormatException(
        'Encrypted forum starter reservation response is invalid.',
      );
    }
    final raw = Map<String, Object?>.from(response);
    final reservation = Map<String, Object?>.from(
      raw['starter_reservation']! as Map,
    );
    if (reservation.length != 2 ||
        reservation['client_nonce'] != clientNonce ||
        reservation['claimed'] is! bool) {
      throw const FormatException(
        'Encrypted forum starter reservation response is invalid.',
      );
    }
    return EncryptedForumThreadReservation(
      channel: KaedeChannel.fromJson(raw),
      clientNonce: clientNonce,
      claimed: reservation['claimed']! as bool,
    );
  }

  Future<KaedeMessage> claimEncryptedForumStarter({
    required EntityRef thread,
    required String clientNonce,
    required Map<String, Object?> e2ee,
    List<EntityRef> attachments = const <EntityRef>[],
    List<EntityRef> mentionUsers = const <EntityRef>[],
  }) async =>
      KaedeMessage.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${thread.wire}/starter',
        data: <String, Object?>{
          'content': null,
          'e2ee': e2ee,
          'client_nonce': clientNonce,
          'attachment_ids': messageAttachmentIds(attachments),
          'mention_user_ids': mentionUsers
              .map((reference) => reference.wire)
              .toSet()
              .toList(growable: false),
        },
      ));

  Future<KaedeChannel> createThreadFromMessage({
    required EntityRef parent,
    required EntityRef message,
    required String name,
    int? autoArchiveDuration,
    int? rateLimitPerUser,
  }) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${parent.wire}/messages/${message.wire}/threads',
        data: <String, Object?>{
          'name': name.trim(),
          if (autoArchiveDuration != null)
            'auto_archive_duration': autoArchiveDuration,
          if (rateLimitPerUser != null) 'rate_limit_per_user': rateLimitPerUser,
        },
      ));

  Future<ThreadPage> threads(
    EntityRef parent, {
    bool archived = false,
    bool includeArchived = false,
    DateTime? before,
    String? cursor,
    int limit = 50,
    String? tagId,
    List<String> tagIds = const <String>[],
    String? query,
    int? sortOrder,
  }) async =>
      ThreadPage.fromJson(await api.getJson(
        '/api/v1/channels/${parent.wire}/threads',
        query: <String, Object?>{
          if (includeArchived)
            'include_archived': true
          else
            'archived': archived,
          'limit': limit,
          if (cursor != null)
            'cursor': cursor
          else if (before != null)
            'before': before.toUtc().toIso8601String(),
          // Dio serializes a list as repeated query keys (`tag_id=1&tag_id=2`),
          // which is the forum API's OR-matching contract.
          if (tagIds.isNotEmpty) 'tag_id': tagIds,
          if (tagIds.isEmpty && tagId != null) 'tag_id': tagId,
          if (query?.trim().isNotEmpty == true) 'query': query!.trim(),
          if (sortOrder != null) 'sort_order': sortOrder,
        },
      ));

  Future<ThreadPage> activeThreads(EntityRef guild) async =>
      ThreadPage.fromJson(await api.getJson(
        '/api/v1/guilds/${guild.wire}/threads/active',
      ));

  Future<KaedeChannel> updateThread(
    EntityRef thread,
    Map<String, Object?> patch,
  ) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/channels/${thread.wire}',
        data: patch,
      ));

  Future<void> deleteThread(EntityRef thread) =>
      api.sendJson('DELETE', '/api/v1/channels/${thread.wire}');

  Future<List<ThreadMember>> threadMembers(EntityRef thread) async {
    final payload = await api.getList(
      '/api/v1/channels/${thread.wire}/thread-members',
    );
    return payload
        .map((item) => ThreadMember.fromJson(item, thread: thread))
        .toList(growable: false);
  }

  Future<void> joinThread(
    EntityRef thread, {
    String notificationLevel = 'inherit',
  }) =>
      api.sendJson(
        'PUT',
        '/api/v1/channels/${thread.wire}/thread-members/@me',
        data: <String, Object?>{
          'flags': 0,
          'notification_level': notificationLevel,
        },
      );

  Future<void> leaveThread(EntityRef thread) => api.sendJson(
        'DELETE',
        '/api/v1/channels/${thread.wire}/thread-members/@me',
      );

  Future<void> addThreadMember(EntityRef thread, EntityRef user) =>
      api.sendJson(
        'PUT',
        '/api/v1/channels/${thread.wire}/thread-members/${user.wire}',
      );

  Future<void> removeThreadMember(EntityRef thread, EntityRef user) =>
      api.sendJson(
        'DELETE',
        '/api/v1/channels/${thread.wire}/thread-members/${user.wire}',
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
    Map<String, Object?>? e2ee,
    List<EntityRef> attachments = const <EntityRef>[],
    EntityRef? replyTo,
    EntityRef? replyAuthor,
    List<EntityRef> mentionUsers = const <EntityRef>[],
    List<EntityRef> stickerIds = const <EntityRef>[],
    RichPollDraft? poll,
    EntityRef? forwardedMessage,
    String? nonce,
    bool tts = false,
    bool voiceMessage = false,
  }) =>
      api.sendJson('POST', '/api/v1/channels/${channel.wire}/messages',
          data: <String, Object?>{
            if (content?.isNotEmpty == true) 'content': content,
            if (e2ee != null) 'e2ee': e2ee,
            if (tts) 'tts': true,
            if (voiceMessage) 'voice_message': true,
            if (stickerIds.isNotEmpty)
              'sticker_ids': stickerIds.map((item) => item.wire).toList(),
            // Attachments are local to the channel's home instance. Unlike
            // user/message references, MessageCreate expects bare snowflakes.
            'attachment_ids': messageAttachmentIds(attachments),
            if (replyTo != null) 'referenced_message_id': replyTo.wire,
            if (poll != null) 'poll': poll.toJson(),
            if (forwardedMessage != null)
              'forwarded_message_id': forwardedMessage.wire,
            'mention_user_ids': <String>{
              ...mentionUsers.map((ref) => ref.wire),
              if (replyAuthor != null) replyAuthor.wire,
            }.toList(),
            'client_nonce': nonce ?? const Uuid().v4(),
          });

  Future<KaedeMessage> createPollMessage(
    EntityRef channel,
    RichPollDraft poll,
  ) async =>
      KaedeMessage.fromJson(await sendMessage(channel, poll: poll));

  Future<MessageForwardResult> forwardMessage({
    required EntityRef sourceChannel,
    required EntityRef sourceMessage,
    required List<EntityRef> destinationChannels,
    String? content,
  }) async {
    final response = await api.sendJson(
      'POST',
      '/api/v1/channels/${sourceChannel.wire}/messages/${sourceMessage.wire}/forward',
      data: <String, Object?>{
        'destination_channel_ids': destinationChannels
            .map((item) => item.wire)
            .toList(growable: false),
        if (content?.trim().isNotEmpty == true) 'content': content!.trim(),
      },
    );
    return _messageForwardResult(
      response,
      sourceMessage: sourceMessage,
      expectedDestinations: destinationChannels.toSet(),
    );
  }

  Future<Map<String, Object?>> prepareMessageForward({
    required EntityRef sourceChannel,
    required EntityRef sourceMessage,
    required List<({EntityRef channel, String nonce})> destinations,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${sourceChannel.wire}/messages/${sourceMessage.wire}/forward/prepare',
        data: <String, Object?>{
          'destinations': destinations
              .map((item) => <String, Object?>{
                    'channel_id': item.channel.wire,
                    'client_nonce': item.nonce,
                  })
              .toList(growable: false),
        },
      );

  Future<MessageForwardResult> submitPreparedMessageForward({
    required EntityRef sourceChannel,
    required EntityRef sourceMessage,
    required List<({EntityRef channel, Map<String, Object?> message})>
        destinations,
  }) async =>
      _messageForwardResult(
        await api.sendJson(
          'POST',
          '/api/v1/channels/${sourceChannel.wire}/messages/${sourceMessage.wire}/forward',
          data: <String, Object?>{
            'destinations': destinations
                .map((item) => <String, Object?>{
                      'destination_channel_id': item.channel.wire,
                      'message': item.message,
                    })
                .toList(growable: false),
          },
        ),
        sourceMessage: sourceMessage,
        expectedDestinations: destinations.map((item) => item.channel).toSet(),
      );

  MessageForwardResult _messageForwardResult(
    Map<String, Object?> response, {
    required EntityRef sourceMessage,
    required Set<EntityRef> expectedDestinations,
  }) {
    final forwards = response['forwards'];
    final failures = response['failures'];
    if (response.length != 2 ||
        forwards is! List ||
        failures is! List ||
        forwards.any((item) => item is! Map) ||
        failures.any((item) => item is! Map)) {
      throw const FormatException('Forward response is invalid.');
    }
    final observed = <EntityRef>{};
    final parsedForwards = forwards.map((raw) {
      final item = Map<String, Object?>.from(raw as Map);
      if (item.length != 2 || item['message'] is! Map) {
        throw const FormatException('Forward response lineage is invalid.');
      }
      final destination = EntityRef.fromJson(item['destination_channel_ref']);
      final message = KaedeMessage.fromJson(
        Map<String, Object?>.from(item['message']! as Map),
      );
      if (!expectedDestinations.contains(destination) ||
          !observed.add(destination) ||
          message.channelRef != destination ||
          message.ref.domain != destination.domain ||
          message.forwardedMessageRef != sourceMessage) {
        throw const FormatException('Forward response lineage is invalid.');
      }
      return MessageForwardSuccess(destination: destination, message: message);
    }).toList(growable: false);
    final parsedFailures = failures.map((raw) {
      final item = Map<String, Object?>.from(raw as Map);
      final error = item['error'];
      final destination = EntityRef.fromJson(item['destination_channel_ref']);
      final status = item['status'];
      if (item.length != 3 ||
          status is! int ||
          status < 400 ||
          status > 599 ||
          error is! Map ||
          !expectedDestinations.contains(destination) ||
          !observed.add(destination)) {
        throw const FormatException('Forward response lineage is invalid.');
      }
      return MessageForwardFailure(
        destination: destination,
        status: status,
        error: Map<String, Object?>.from(error),
      );
    }).toList(growable: false);
    if (observed.length != expectedDestinations.length ||
        !observed.containsAll(expectedDestinations)) {
      throw const FormatException(
        'Forward response omitted a requested destination.',
      );
    }
    return MessageForwardResult(
      forwards: parsedForwards,
      failures: parsedFailures,
    );
  }

  Future<KaedeMessage> editMessage(
    EntityRef channel,
    EntityRef message,
    String? content, {
    Map<String, Object?>? e2ee,
  }) async =>
      KaedeMessage.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/channels/${channel.wire}/messages/${message.wire}',
        data: <String, Object?>{
          if (content != null) 'content': content,
          if (e2ee != null) 'e2ee': e2ee,
        },
      ));
  Future<void> deleteMessage(EntityRef channel, EntityRef message) =>
      api.sendJson('DELETE',
          '/api/v1/channels/${channel.wire}/messages/${message.wire}');
  Future<Map<String, Object?>> reportMessage(
    EntityRef message, {
    required String category,
    EntityRef? focusedAttachment,
    String? description,
    String? disclosedContent,
    bool disclosureAcknowledged = false,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/reports',
        data: messageReportRequestData(
          message,
          category: category,
          focusedAttachment: focusedAttachment,
          description: description,
          disclosedContent: disclosedContent,
          disclosureAcknowledged: disclosureAcknowledged,
        ),
      );

  Future<Map<String, Object?>> createReportAttachmentEvidenceTicket(
    String reportId, {
    required String filename,
    required String contentType,
    required int size,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/reports/${Uri.encodeComponent(reportId)}/attachment-evidence',
        data: <String, Object?>{
          'filename': filename,
          'content_type': contentType,
          'size': size,
          'disclosure_acknowledged': true,
        },
      );

  Future<void> uploadReportAttachmentEvidence(
    Map<String, Object?> ticket,
    File file, {
    required String contentType,
    void Function(int sent, int total)? onProgress,
  }) =>
      api.putPresignedFile(
        ticket['upload_url']! as String,
        file,
        contentType: contentType,
        onProgress: onProgress,
      );

  Future<Map<String, Object?>> commitReportAttachmentEvidence(
    String reportId, {
    required String attachmentId,
  }) =>
      api.sendJson(
        'PUT',
        '/api/v1/reports/${Uri.encodeComponent(reportId)}/attachment-evidence',
        data: <String, Object?>{
          'attachment_id': attachmentId,
          'disclosure_acknowledged': true,
        },
      );

  Future<List<Map<String, Object?>>> myReports() =>
      api.getList('/api/v1/reports/@me');
  Future<void> react(EntityRef channel, EntityRef message, String emoji) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/messages/${message.wire}/reactions',
        data: <String, Object?>{'emoji': canonicalReactionEmoji(emoji)},
      );
  Future<void> removeReaction(
          EntityRef channel, EntityRef message, String emoji) =>
      api.sendJson('DELETE',
          '/api/v1/channels/${channel.wire}/messages/${message.wire}/reactions/${Uri.encodeComponent(canonicalReactionEmoji(emoji))}/@me');
  Future<void> clearReactions(EntityRef channel, EntityRef message) =>
      api.sendJson('DELETE', reactionClearEndpoint(channel, message));
  Future<void> clearReactionGroup(
          EntityRef channel, EntityRef message, String emoji) =>
      api.sendJson(
        'DELETE',
        reactionClearEndpoint(channel, message, emoji: emoji),
      );
  Future<ReactionUserPage> reactionUsers(
    EntityRef channel,
    EntityRef message,
    String emoji, {
    EntityRef? after,
    int limit = 50,
  }) async {
    final canonicalEmoji = canonicalReactionEmoji(emoji);
    final payload = await api.getJson(
      '/api/v1/channels/${channel.wire}/messages/${message.wire}/reactions/${Uri.encodeComponent(canonicalEmoji)}',
      query: <String, Object?>{
        'limit': limit,
        if (after != null) 'after': after.wire,
      },
    );
    final items = strictNetworkObjectList(
      payload['items'],
      label: 'Reaction users',
    ).map(KaedeUser.fromJson).toList(growable: false);
    final rawNext = payload['next_after'];
    return ReactionUserPage(
      items: items,
      total: int.tryParse('${payload['total'] ?? 0}') ?? 0,
      nextAfter: rawNext is String && rawNext.isNotEmpty
          ? EntityRef.parse(rawNext)
          : null,
    );
  }

  Future<void> pin(EntityRef channel, EntityRef message) => api.sendJson(
      'PUT', '/api/v1/channels/${channel.wire}/messages/pins/${message.wire}');
  Future<void> unpin(EntityRef channel, EntityRef message) => api.sendJson(
      'DELETE',
      '/api/v1/channels/${channel.wire}/messages/pins/${message.wire}');
  Future<List<KaedeMessage>> pins(EntityRef channel) async {
    final result = <KaedeMessage>[];
    final seen = <EntityRef>{};
    DateTime? before;
    for (var pageIndex = 0; pageIndex < 5; pageIndex += 1) {
      final payload = await api.getJson(
        '/api/v1/channels/${channel.wire}/messages/pins',
        query: <String, Object?>{
          'limit': 50,
          if (before != null) 'before': before.toUtc().toIso8601String(),
        },
      );
      final page = parsePinnedMessagePage(
        payload,
        channel: channel,
        before: before,
      );
      for (final message in page.items) {
        if (!seen.add(message.ref)) {
          throw const FormatException(
            'Pinned message pagination repeated a message.',
          );
        }
        result.add(message);
      }
      if (!page.hasMore) return List<KaedeMessage>.unmodifiable(result);
      if (page.items.isEmpty) {
        throw const FormatException(
          'Pinned message pagination did not advance.',
        );
      }
      final nextBefore = page.items.last.pinnedAt!;
      if (before != null && !nextBefore.isBefore(before)) {
        throw const FormatException(
          'Pinned message pagination did not advance.',
        );
      }
      before = nextBefore;
    }
    throw const FormatException(
      'Pinned message response exceeds the 250-message channel limit.',
    );
  }

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
    EntityRef guild,
    EntityRef user,
    Map<String, Object?> patch, {
    String? reason,
  }) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}',
        data: patch,
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      );
  Future<void> kick(EntityRef guild, EntityRef user, {String? reason}) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/members/${user.wire}',
        headers: <String, String>{
          if (reason != null) 'X-Audit-Log-Reason': reason
        },
      );
  Future<void> ban(EntityRef guild, EntityRef user,
          {DateTime? expiresAt,
          String? reason,
          int deleteMessageSeconds = 0}) =>
      api.sendJson(
        'PUT',
        '/api/v1/guilds/${guild.wire}/bans/${user.wire}',
        data: <String, Object?>{
          if (reason?.isNotEmpty == true) 'reason': reason,
          if (expiresAt != null)
            'expires_at': expiresAt.toUtc().toIso8601String(),
          'delete_message_seconds': deleteMessageSeconds,
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
  Future<List<Map<String, Object?>>> auditLog(
    EntityRef guild, {
    String? before,
    EntityRef? userId,
    int? actionType,
    String? targetType,
    int limit = 50,
  }) =>
      api.getList('/api/v1/guilds/${guild.wire}/audit-logs',
          query: <String, Object?>{
            'limit': limit,
            if (before != null) 'before': before,
            if (userId != null) 'user_id': userId.wire,
            if (actionType != null) 'action_type': actionType,
            if (targetType?.isNotEmpty == true) 'target_type': targetType,
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
  Future<void> reorderRoles(EntityRef guild, List<KaedeRole> roles) async {
    await api.sendJsonList(
      'PATCH',
      '/api/v1/guilds/${guild.wire}/roles',
      data: <String, Object?>{
        'roles': guildRolePositionRequest(roles),
      },
    );
  }

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
  Future<List<Map<String, Object?>>> channelInvites(EntityRef channel) =>
      api.getList('/api/v1/channels/${channel.wire}/invites');
  Future<Map<String, Object?>> createInvite(
          EntityRef guild, Map<String, Object?> request) =>
      api.sendJson('POST', '/api/v1/guilds/${guild.wire}/invites',
          data: request);
  Future<Map<String, Object?>> previewInvite(String code) =>
      api.getJson('/api/v1/invites/$code');
  Future<Map<String, Object?>> acceptInvite(String code) =>
      api.sendJson('POST', '/api/v1/invites/$code');
  Future<void> revokeInvite(String code, {required EntityRef guild}) =>
      api.sendJson(
        'DELETE',
        '/api/v1/invites/${Uri.encodeComponent('$code@${guild.domain.value}')}',
        query: <String, Object?>{'guild_ref': guild.wire},
      );

  Future<List<Map<String, Object?>>> emojis() =>
      api.getList('/api/v1/users/@me/emojis');
  Future<void> deleteEmoji(EntityRef guild, EntityRef emoji) => api.sendJson(
      'DELETE', '/api/v1/guilds/${guild.wire}/emojis/${emoji.id.value}');
  Future<List<Map<String, Object?>>> stickers() =>
      api.getList('/api/v1/users/@me/stickers');
  Future<void> deleteSticker(EntityRef guild, EntityRef sticker) =>
      api.sendJson('DELETE',
          '/api/v1/guilds/${guild.wire}/stickers/${sticker.id.value}');

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
    );
  }

  Future<KaedeUser> removeUserAsset(String kind) async {
    if (kind != 'avatar' && kind != 'banner') {
      throw const UserInputException('Choose avatar or banner.');
    }
    return KaedeUser.fromJson(
      await api.sendJson('DELETE', '/api/v1/users/@me/assets/$kind'),
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
    );
  }

  Future<KaedeGuild> removeGuildAsset({
    required EntityRef guild,
    required String kind,
  }) async {
    if (kind != 'icon' && kind != 'banner') {
      throw const UserInputException('Choose guild icon or banner.');
    }
    return KaedeGuild.fromJson(
      await api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/assets/$kind',
      ),
    );
  }

  Future<KaedeRole> uploadRoleIcon({
    required EntityRef guild,
    required EntityRef role,
    required String filename,
    required String contentType,
    required File file,
  }) async {
    final path = '/api/v1/guilds/${guild.wire}/roles/${role.wire}/icon';
    final size = await file.length();
    final ticket = await api.sendJson('POST', path, data: <String, Object?>{
      'filename': filename,
      'content_type': contentType,
      'size': size,
    });
    await api.putPresignedFile(ticket['upload_url']! as String, file,
        contentType: contentType);
    final attachmentId = '${ticket['id']}';
    final result = await commitScannedMedia(
      commit: () => api.sendJson('PUT', path,
          data: <String, Object?>{'attachment_id': attachmentId}),
    );
    return KaedeRole.fromJson(result);
  }

  Future<KaedeRole> deleteRoleIcon(EntityRef guild, EntityRef role) async =>
      KaedeRole.fromJson(await api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/roles/${role.wire}/icon',
      ));

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
    );
  }

  Future<Map<String, Object?>> uploadSticker({
    required EntityRef guild,
    required String name,
    required String filename,
    required String contentType,
    required File file,
    String? description,
    double cropX = 0,
    double cropY = 0,
    double cropWidth = 1,
    double cropHeight = 1,
    bool removeBackground = false,
  }) async {
    final size = await file.length();
    final ticket = await api.sendJson(
      'POST',
      '/api/v1/guilds/${guild.wire}/stickers/tickets',
      data: <String, Object?>{
        'filename': filename,
        'content_type': contentType,
        'size': size,
        'crop': <String, Object?>{
          'x': cropX,
          'y': cropY,
          'width': cropWidth,
          'height': cropHeight,
        },
        'remove_background': removeBackground,
      },
    );
    await api.putPresignedFile(ticket['upload_url']! as String, file,
        contentType: contentType);
    final attachmentId = '${ticket['id']}';
    return commitScannedMedia(
      commit: () => api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/stickers',
        data: <String, Object?>{
          'attachment_id': attachmentId,
          'name': name,
          'description': description?.trim().isNotEmpty == true
              ? description!.trim()
              : null,
        },
      ),
    );
  }

  Future<List<Map<String, Object?>>> webhooks(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/webhooks');
  Future<List<Map<String, Object?>>> channelWebhooks(
          EntityRef guild, EntityRef channel) =>
      api.getList(
          '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/webhooks');
  Future<Map<String, Object?>> createWebhook(
          EntityRef guild, EntityRef channel, String name) =>
      api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/webhooks',
        data: <String, Object?>{'name': name},
      );
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

  Future<Map<String, Object?>> voiceToken(
    EntityRef channel, {
    String? senderDeviceId,
    required String connectionId,
    bool takeover = false,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/voice/token',
        data: <String, Object?>{
          'sender_device_id': senderDeviceId,
          'connection_id': connectionId,
          'takeover': takeover,
          'client_kind': 'mobile',
        },
      );
  Future<Map<String, Object?>> voiceOccupancy(EntityRef channel) =>
      api.getJson('/api/v1/channels/${channel.wire}/voice/occupancy');
  Future<String?> voiceChannelStatus(EntityRef channel) async {
    final response =
        await api.getJson('/api/v1/channels/${channel.wire}/voice-status');
    return response['status'] as String?;
  }

  Future<void> setVoiceChannelStatus(EntityRef channel, String? status) async {
    await api.sendJson(
      'PUT',
      '/api/v1/channels/${channel.wire}/voice-status',
      data: <String, Object?>{'status': status},
    );
  }

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
  Future<Map<String, Object?>> activeCall(EntityRef channel) =>
      api.getJson('/api/v1/channels/${channel.wire}/calls/active');
  Future<Map<String, Object?>> callAction(EntityRef call, String action) =>
      api.sendJson('POST', '/api/v1/calls/${call.wire}',
          data: <String, Object?>{'action': action});
  Future<Map<String, Object?>> callVoiceToken(
    EntityRef call, {
    String? senderDeviceId,
    required String connectionId,
    bool takeover = false,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/calls/${call.wire}/voice/token',
        data: <String, Object?>{
          'sender_device_id': senderDeviceId,
          'connection_id': connectionId,
          'takeover': takeover,
          'client_kind': 'mobile',
        },
      );
  Future<KaedeChannel> createGroupDm(List<String> handles,
          {String? name}) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'POST',
        '/api/v1/users/@me/channels/group',
        data: <String, Object?>{'handles': handles, 'name': name},
      ));
  Future<KaedeChannel> renameGroupDm(EntityRef channel, String? name) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/users/@me/channels/${channel.wire}/group',
        data: <String, Object?>{'name': name},
      ));
  Future<KaedeChannel> addGroupDmMember(
          EntityRef channel, String handle) async =>
      KaedeChannel.fromJson(await api.sendJson(
        'POST',
        '/api/v1/users/@me/channels/${channel.wire}/group/recipients',
        data: <String, Object?>{'handle': handle},
      ));
  Future<void> leaveGroupDm(EntityRef channel) => api.sendJson(
        'POST',
        '/api/v1/users/@me/channels/${channel.wire}/group/leave',
      );
  Future<void> removeGroupDmMember(EntityRef channel, EntityRef user) =>
      api.sendJson(
        'DELETE',
        '/api/v1/users/@me/channels/${channel.wire}/group/recipients/${user.wire}',
      );

  Future<Map<String, Object?>> createAttachmentTicket({
    required EntityRef channel,
    required String filename,
    required String contentType,
    required int size,
    String encryptionMode = 'plaintext',
    String? encryptionProtocol,
    double? durationSecs,
    String? waveform,
  }) =>
      api.sendJson('POST', '/api/v1/channels/${channel.wire}/attachments',
          data: <String, Object?>{
            'filename': filename,
            'content_type': contentType,
            'size': size,
            'encryption_mode': encryptionMode,
            if (encryptionProtocol != null)
              'encryption_protocol': encryptionProtocol,
            if (durationSecs != null) 'duration_secs': durationSecs,
            if (waveform != null) 'waveform': waveform,
          });

  Future<Map<String, Object?>> e2eeDeviceChallenge({
    required String identityKey,
    required String credentialDigest,
  }) =>
      api.sendJson('POST', '/api/v1/e2ee/devices/challenge', data: {
        'identity_key': identityKey,
        'credential_digest': credentialDigest,
      });

  Future<Map<String, Object?>> registerE2eeDevice({
    required String challengeId,
    required String identityKey,
    required String credential,
    required String signature,
    required String deviceName,
    required String platform,
  }) async {
    final authorization = _pendingE2eeRecoveryAuthorization;
    final registered = await api.sendJson(
      'POST',
      '/api/v1/e2ee/devices',
      data: <String, Object?>{
        'challenge_id': challengeId,
        'identity_key': identityKey,
        'credential': credential,
        'signature': signature,
        'device_name': deviceName,
        'platform': platform,
        'capabilities': const ['e2ee-mls/1', 'e2ee-media/1'],
        if (authorization != null) 'recovery_authorization': authorization,
      },
    );
    // A successful HTTP response means the backend committed registration and
    // atomically consumed (or cancelled for a fresh identity) the reset fence.
    _pendingE2eeRecoveryAuthorization = null;
    return registered;
  }

  Future<Map<String, Object?>> e2eeDevices() =>
      api.getJson('/api/v1/e2ee/devices');

  Future<Map<String, Object?>> acquireE2eeVaultLease() => api.sendJson(
        'POST',
        '/api/v1/e2ee/vault/lease',
        data: const <String, Object?>{},
      );

  Future<Map<String, Object?>> e2eeVault() => api.getJson('/api/v1/e2ee/vault');

  Future<Map<String, Object?>> e2eeVaultDigests({
    required String after,
    int limit = 256,
  }) =>
      api.getJson(
        '/api/v1/e2ee/vault/digests',
        query: <String, Object?>{'after': after, 'limit': limit},
      );

  Future<Map<String, Object?>> e2eeControlLog(
    EntityRef channel, {
    String? after,
  }) =>
      api.getJson(
        '/api/v1/e2ee/channels/${channel.wire}/control-log',
        query: <String, Object?>{
          'limit': 25,
          if (after != null) 'after': after,
        },
      );

  Future<Map<String, Object?>> updateE2eeVault({
    required String leaseToken,
    required String expectedRevision,
    required Map<String, Object?> envelope,
  }) =>
      api.sendJson(
        'PUT',
        '/api/v1/e2ee/vault',
        data: <String, Object?>{
          'lease_token': leaseToken,
          'expected_revision': expectedRevision,
          'envelope': envelope,
        },
      );

  Future<void> releaseE2eeVaultLease(String leaseToken) async {
    try {
      await api.sendJson(
        'POST',
        '/api/v1/e2ee/vault/lease/release',
        data: <String, Object?>{'lease_token': leaseToken},
      );
    } on Object {
      // The lease is short-lived and releases itself. Failure here must not
      // replace the result of the MLS operation that already completed.
    }
  }

  Future<void> revokeE2eeDevice(String deviceId) => api.sendJson(
        'DELETE',
        '/api/v1/e2ee/devices/${Uri.encodeComponent(deviceId)}',
      );

  Future<Map<String, Object?>> resetE2eeIdentity() => api.sendJson(
        'POST',
        '/api/v1/e2ee/reset',
        data: const <String, Object?>{
          'confirmation': 'RESET ENCRYPTED HISTORY',
        },
      );

  Future<void> uploadE2eeKeyPackages(
    String deviceId, {
    required String expiresAt,
    required List<String> packages,
    required String signature,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/e2ee/devices/${Uri.encodeComponent(deviceId)}/key-packages',
        data: {
          'cipher_suite': 'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519',
          'expires_at': expiresAt,
          'packages': packages,
          'signature': signature,
        },
      );

  Future<Map<String, Object?>> proposeE2eeRoom(
    EntityRef channel,
    String deviceId,
    String operationId,
  ) =>
      api.sendJson(
        'POST',
        '/api/v1/e2ee/channels/${channel.wire}/propose',
        data: {
          'operation_id': operationId,
          'sender_device_id': deviceId,
        },
      );

  Future<Map<String, Object?>> proposeE2eeRekey(
    EntityRef channel,
    String deviceId,
    String operationId,
  ) =>
      api.sendJson(
        'POST',
        '/api/v1/e2ee/channels/${channel.wire}/rekey/propose',
        data: {
          'operation_id': operationId,
          'sender_device_id': deviceId,
        },
      );

  Future<Map<String, Object?>> activateE2eeRoom(
    EntityRef channel, {
    required String operationId,
    required String deviceId,
    required String generation,
    required String groupId,
    required String commit,
    required String welcome,
    required String preparedVaultRevision,
    required String preparedVaultDigest,
    required String vaultLeaseToken,
    required bool rekey,
  }) =>
      api.sendJson(
        'POST',
        rekey
            ? '/api/v1/e2ee/channels/${channel.wire}/rekey/activate'
            : '/api/v1/e2ee/channels/${channel.wire}/activate',
        data: {
          'operation_id': operationId,
          'sender_device_id': deviceId,
          'policy_generation': generation,
          'epoch': '1',
          'group_id': groupId,
          'commit': commit,
          'welcome': welcome,
          'prepared_vault_revision': preparedVaultRevision,
          'prepared_vault_digest': preparedVaultDigest,
          'vault_lease_token': vaultLeaseToken,
        },
      );

  Future<Map<String, Object?>> e2eeRoomOperation(
    EntityRef channel,
    String operationId,
  ) =>
      api.getJson(
        '/api/v1/e2ee/channels/${channel.wire}/operations/${Uri.encodeComponent(operationId)}',
      );

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
    double? durationSecs,
    String? waveform,
    void Function(int sent, int total)? onProgress,
  }) async {
    final size = await file.length();
    final ticket = await createAttachmentTicket(
      channel: channel,
      filename: filename,
      contentType: contentType,
      size: size,
      durationSecs: durationSecs,
      waveform: waveform,
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
          privateMediaUrl: attachment.privateMediaUrl,
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

  Future<List<Map<String, Object?>>> applicationCommands(EntityRef channel) =>
      api.getList('/api/v1/channels/${channel.wire}/application-commands');

  Future<MobileDirectoryPage> applicationDirectory({
    String? query,
    String? collection,
    Domain? domain,
    int limit = 50,
  }) async {
    final home = api.tokens?.instance ?? api.selectedInstance;
    if (home == null) {
      throw StateError('Application discovery requires an authenticated home.');
    }
    final authority = domain ?? home;
    final normalizedQuery = query?.trim() ?? '';
    if (normalizedQuery.length > 100 || limit < 1 || limit > 50) {
      throw ArgumentError('Application Directory request is out of bounds.');
    }
    if (collection != null &&
        !mobileDirectoryCollectionSlugs.contains(collection)) {
      throw ArgumentError.value(collection, 'collection');
    }
    return MobileDirectoryPage.fromJson(
      await api.getJson(
        '/api/v1/application-directory',
        query: <String, Object?>{
          if (normalizedQuery.isNotEmpty) 'q': normalizedQuery,
          if (collection != null) 'collection': collection,
          'domain': authority.value,
          'limit': limit,
        },
      ),
      expectedOrigin: authority,
      expectedCollection: collection,
      requestedLimit: limit,
    );
  }

  Future<MobileBotProfileApplication?> botProfileApplication(
    EntityRef bot,
  ) async {
    try {
      return MobileBotProfileApplication.fromJson(
        await api.getJson(
          '/api/v1/application-directory/bot-profiles/${bot.pathSegment}',
        ),
        expectedBot: bot,
      );
    } on KaedeException catch (error) {
      if (error.status == 404) return null;
      rethrow;
    }
  }

  Future<List<UserApplicationInstallation>>
      userApplicationInstallations() async =>
          (await api.getList('/api/v1/users/@me/application-installations'))
              .map(UserApplicationInstallation.fromJson)
              .toList(growable: false);

  Future<ApplicationInstallInvite> resolveApplicationInstallInvite(
    EntityRef application,
    String templateSlug,
  ) async =>
      ApplicationInstallInvite.fromJson(
        await api.getJson(
          '/api/v1/bot-invites/${application.wire}/${Uri.encodeComponent(templateSlug)}',
        ),
      );

  Future<void> installGuildApplication(
    EntityRef guild,
    EntityRef application,
    String templateSlug,
  ) =>
      api.sendJson(
        'POST',
        '/api/v1/guilds/${guild.wire}/integrations/bots',
        query: <String, Object?>{
          'application_ref': application.wire,
          'template_slug': templateSlug,
        },
      );

  Future<UserApplicationInstallation> installUserApplication(
    EntityRef application, {
    required List<String> scopes,
    required List<String> contexts,
    List<String> intents = userApplicationIntents,
  }) async =>
      UserApplicationInstallation.fromJson(
        await api.sendJson(
          'POST',
          '/api/v1/users/@me/application-installations',
          data: <String, Object?>{
            'application_ref': application.wire,
            ...userApplicationGrantData(
              scopes: scopes,
              contexts: contexts,
              intents: intents,
            ),
          },
        ),
      );

  Future<UserApplicationInstallation> updateUserApplicationInstallation(
    String installationId, {
    List<String>? scopes,
    List<String>? contexts,
    List<String>? intents,
  }) async =>
      UserApplicationInstallation.fromJson(
        await api.sendJson(
          'PATCH',
          '/api/v1/users/@me/application-installations/${Uri.encodeComponent(installationId)}',
          data: <String, Object?>{
            if (scopes != null) 'scopes': scopes,
            if (contexts != null) 'contexts': contexts,
            if (intents != null) 'intents': intents,
          },
        ),
      );

  Future<void> revokeUserApplicationInstallation(String installationId) =>
      api.sendJson(
        'DELETE',
        '/api/v1/users/@me/application-installations/${Uri.encodeComponent(installationId)}',
      );

  Future<Map<String, Object?>> invokeApplicationCommand({
    required EntityRef channel,
    required EntityRef application,
    required String commandId,
    required String integrationType,
    String? dmCapabilityId,
    String? dmCapabilityRevision,
    required String name,
    required String type,
    Map<String, Object?> options = const <String, Object?>{},
    EntityRef? target,
    Map<String, Object?>? encryptedPayload,
    List<String> attachmentIds = const <String>[],
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/interactions',
        data: interactionRequestData(
          <String, Object?>{
            'application_ref': application.wire,
            'command_id': commandId,
            ..._applicationCommandLineageData(
              integrationType: integrationType,
              dmCapabilityId: dmCapabilityId,
              dmCapabilityRevision: dmCapabilityRevision,
            ),
            'command_name': name,
            'command_type': type,
            if (target != null) 'target_ref': target.wire,
            'options': options,
          },
          encryptedPayload: encryptedPayload,
          attachmentIds: attachmentIds,
        ),
      );

  Future<Map<String, Object?>> autocompleteApplicationCommand({
    required EntityRef channel,
    required EntityRef application,
    required String commandId,
    required String integrationType,
    String? dmCapabilityId,
    String? dmCapabilityRevision,
    required String name,
    required String type,
    required Map<String, Object?> options,
    required String focusedOption,
    required int generation,
    Map<String, Object?>? encryptedPayload,
    List<String> attachmentIds = const <String>[],
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/interactions',
        data: interactionRequestData(
          <String, Object?>{
            'application_ref': application.wire,
            'command_id': commandId,
            ..._applicationCommandLineageData(
              integrationType: integrationType,
              dmCapabilityId: dmCapabilityId,
              dmCapabilityRevision: dmCapabilityRevision,
            ),
            'interaction_type': 'autocomplete',
            'command_name': name,
            'command_type': type,
            'options': options,
            'focused_option': focusedOption,
            'autocomplete_generation': generation,
          },
          encryptedPayload: encryptedPayload,
          attachmentIds: attachmentIds,
        ),
      );

  Future<Map<String, Object?>> invokeMessageComponent({
    required EntityRef channel,
    required EntityRef message,
    required EntityRef application,
    required int viewVersion,
    required String customId,
    List<String> values = const <String>[],
    Map<String, Object?>? encryptedPayload,
    List<String> attachmentIds = const <String>[],
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/interactions',
        data: interactionRequestData(
          <String, Object?>{
            'application_ref': application.wire,
            'interaction_type': 'component',
            'message_ref': message.wire,
            if (viewVersion > 0) 'view_version': viewVersion,
            'custom_id': customId,
            'values': values,
          },
          encryptedPayload: encryptedPayload,
          attachmentIds: attachmentIds,
        ),
      );

  Future<Map<String, Object?>> invokeEphemeralComponent({
    required EntityRef channel,
    required EntityRef application,
    required String responseId,
    required int viewVersion,
    required String customId,
    List<String> values = const <String>[],
    Map<String, Object?>? encryptedPayload,
    List<String> attachmentIds = const <String>[],
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/interactions',
        data: interactionRequestData(
          <String, Object?>{
            'application_ref': application.wire,
            'interaction_type': 'component',
            'response_id': responseId,
            'view_version': viewVersion,
            'custom_id': customId,
            'values': values,
          },
          encryptedPayload: encryptedPayload,
          attachmentIds: attachmentIds,
        ),
      );

  Future<Map<String, Object?>> submitInteractionModal({
    required EntityRef channel,
    required EntityRef application,
    required String responseId,
    required String customId,
    required List<Map<String, Object?>> components,
    Map<String, Object?>? encryptedPayload,
    List<String> attachmentIds = const <String>[],
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/interactions',
        data: interactionRequestData(
          <String, Object?>{
            'application_ref': application.wire,
            'interaction_type': 'modal_submit',
            'response_id': responseId,
            'custom_id': customId,
            'components': components,
          },
          encryptedPayload: encryptedPayload,
          attachmentIds: attachmentIds,
        ),
      );

  Future<void> setPollVote({
    required EntityRef channel,
    required EntityRef message,
    required int answerId,
    required bool selected,
  }) =>
      api.sendJson(
        selected ? 'PUT' : 'DELETE',
        '/api/v1/channels/${channel.wire}/messages/${message.wire}/polls/answers/$answerId/@me',
      );

  Future<PollVoterPage> pollVoters({
    required EntityRef channel,
    required EntityRef message,
    required int answerId,
    EntityRef? after,
    int limit = 50,
  }) async {
    final response = await api.getJson(
      '/api/v1/channels/${channel.wire}/messages/${message.wire}/polls/answers/$answerId',
      query: <String, Object?>{
        'limit': limit,
        if (after != null) 'after': after.wire,
      },
    );
    return PollVoterPage(
      items: strictNetworkObjectList(
        response['users'],
        label: 'Poll voters',
      ).map(KaedeUser.fromJson).toList(growable: false),
      nextAfter: response['next_after'] == null
          ? null
          : EntityRef.fromJson(response['next_after']),
    );
  }

  Future<void> setInteractionPollVote({
    required String interactionId,
    required String responseId,
    required int answerId,
    required bool selected,
  }) =>
      api.sendJson(
        selected ? 'PUT' : 'DELETE',
        '/api/v1/interactions/$interactionId/responses/$responseId/polls/answers/$answerId/@me',
      );

  Future<PollVoterPage> interactionPollVoters({
    required String interactionId,
    required String responseId,
    required int answerId,
    EntityRef? after,
    int limit = 50,
  }) async {
    final response = await api.getJson(
      '/api/v1/interactions/$interactionId/responses/$responseId/polls/answers/$answerId',
      query: <String, Object?>{
        'limit': limit,
        if (after != null) 'after': after.wire,
      },
    );
    return PollVoterPage(
      items: strictNetworkObjectList(
        response['users'],
        label: 'Interaction poll voters',
      ).map(KaedeUser.fromJson).toList(growable: false),
      nextAfter: response['next_after'] == null
          ? null
          : EntityRef.fromJson(response['next_after']),
    );
  }

  Future<KaedeMessage> finalizePoll({
    required EntityRef channel,
    required EntityRef message,
  }) async =>
      KaedeMessage.fromJson(await api.sendJson(
        'POST',
        '/api/v1/channels/${channel.wire}/messages/${message.wire}/polls/expire',
      ));

  Future<KaedeMessage> forwardedMessage({
    required EntityRef destinationChannel,
    required EntityRef destinationMessage,
  }) async =>
      KaedeMessage.fromJson(
        await api.getJson(
          '/api/v1/channels/${destinationChannel.wire}/messages/${destinationMessage.wire}/forwarded',
        ),
      );

  Future<List<Map<String, Object?>>> botIntegrations(EntityRef guild) =>
      api.getList('/api/v1/guilds/${guild.wire}/integrations/bots');

  Future<Map<String, Object?>> updateBotIntegrationChannelRestrictions(
    EntityRef guild,
    EntityRef application,
    Iterable<EntityRef> channelRestrictions,
  ) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/integrations/bots/${application.wire}',
        data: <String, Object?>{
          'channel_restrictions': channelRestrictions
              .map((channel) => channel.wire)
              .toList(growable: false),
        },
      );

  String _botE2eeParticipationPath(
    EntityRef guild,
    EntityRef channel,
    EntityRef application,
  ) =>
      '/api/v1/guilds/${guild.wire}/channels/${channel.wire}/e2ee/bots/${application.wire}';

  Future<BotE2eeParticipation> botE2eeParticipation({
    required EntityRef guild,
    required EntityRef channel,
    required EntityRef application,
  }) async =>
      BotE2eeParticipation.fromJson(await api.getJson(
        _botE2eeParticipationPath(guild, channel, application),
      ));

  Future<BotE2eeParticipation> grantBotE2eeParticipation({
    required EntityRef guild,
    required EntityRef channel,
    required EntityRef application,
    String? reason,
  }) async =>
      BotE2eeParticipation.fromJson(await api.sendJson(
        'PUT',
        _botE2eeParticipationPath(guild, channel, application),
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      ));

  Future<BotE2eeParticipation> revokeBotE2eeParticipation({
    required EntityRef guild,
    required EntityRef channel,
    required EntityRef application,
    String? reason,
  }) async =>
      BotE2eeParticipation.fromJson(await api.sendJson(
        'DELETE',
        _botE2eeParticipationPath(guild, channel, application),
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      ));

  String _dmBotE2eeParticipationPath(
    EntityRef channel,
    EntityRef application,
  ) =>
      '/api/v1/channels/${channel.wire}/e2ee/bots/${application.wire}';

  Future<DmBotE2eeParticipation> dmBotE2eeParticipation({
    required EntityRef channel,
    required EntityRef application,
  }) async =>
      DmBotE2eeParticipation.fromJson(await api.getJson(
        _dmBotE2eeParticipationPath(channel, application),
      ));

  Future<DmBotE2eeParticipation> consentToDmBotE2eeParticipation({
    required EntityRef channel,
    required EntityRef application,
  }) async =>
      DmBotE2eeParticipation.fromJson(await api.sendJson(
        'PUT',
        _dmBotE2eeParticipationPath(channel, application),
      ));

  Future<DmBotE2eeParticipation> revokeDmBotE2eeParticipation({
    required EntityRef channel,
    required EntityRef application,
  }) async =>
      DmBotE2eeParticipation.fromJson(await api.sendJson(
        'DELETE',
        _dmBotE2eeParticipationPath(channel, application),
      ));

  Future<List<ApplicationCommandPermissionScope>> applicationCommandPermissions(
    EntityRef application,
    EntityRef guild,
  ) async =>
      (await api.getList(
        '/api/v1/applications/${application.wire}/guilds/${guild.wire}/commands/permissions',
      ))
          .map(ApplicationCommandPermissionScope.fromJson)
          .toList(growable: false);

  Future<ApplicationCommandPermissionScope> updateApplicationCommandPermissions(
    EntityRef application,
    EntityRef guild,
    EntityRef scope,
    List<ApplicationCommandPermissionEntry> permissions,
  ) async =>
      ApplicationCommandPermissionScope.fromJson(
        await api.sendJson(
          'PUT',
          '/api/v1/applications/${application.wire}/guilds/${guild.wire}/commands/${scope.wire}/permissions',
          data: commandPermissionUpdateData(permissions),
        ),
      );

  Future<void> removeBotIntegration(
    EntityRef guild,
    EntityRef application, {
    String? reason,
  }) =>
      api.sendJson(
        'DELETE',
        '/api/v1/guilds/${guild.wire}/integrations/bots/${application.wire}',
        headers: <String, String>{
          if (reason?.trim().isNotEmpty == true)
            'X-Audit-Log-Reason': reason!.trim(),
        },
      );

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

  Future<List<Map<String, Object?>>> pushDevices() =>
      api.getList('/api/v1/users/@me/push-devices');

  Future<Map<String, Object?>> beginRelayPushEnrollment({
    required String installationId,
    required String platform,
    required String routeId,
    required String appId,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/users/@me/push-devices/relay/enrollment',
        data: <String, Object?>{
          'installation_id': installationId,
          'platform': platform,
          'route_id': routeId,
          'app_id': appId,
        },
      );

  Future<Map<String, Object?>> createRelayPushSubscription({
    required Uri relayUrl,
    required Map<String, Object?> grant,
    required String providerToken,
    required String managementSecret,
  }) =>
      api.postPublicJson(
        relayUrl.resolve('/push/v1/subscriptions'),
        expectedOrigin: relayUrl.host,
        data: <String, Object?>{
          'grant': grant,
          'provider_token': providerToken,
          'management_secret': managementSecret,
        },
      );

  Future<void> revokeRelayPushSubscription(RelayPushState state) =>
      api.deletePublic(
        state.relayUrl.resolve(
          '/push/v1/subscriptions/${Uri.encodeComponent(state.subscriptionId)}',
        ),
        expectedOrigin: state.relayUrl.host,
        headers: <String, String>{
          'X-Kaede-Push-Management': state.managementSecret,
        },
      );

  Future<Map<String, Object?>> completeRelayPushEnrollment({
    required String installationId,
    required String platform,
    required String routeId,
    required String wakeSecret,
    required Map<String, Object?> receipt,
    String? deviceName,
  }) =>
      api.sendJson(
        'POST',
        '/api/v1/users/@me/push-devices/relay/complete',
        data: <String, Object?>{
          'installation_id': installationId,
          'platform': platform,
          'route_id': routeId,
          'wake_secret': wakeSecret,
          'receipt': receipt,
          if (deviceName != null) 'device_name': deviceName,
        },
      );

  Future<void> unregisterPushDevice(String deviceId) => api.sendJson(
        'DELETE',
        '/api/v1/users/@me/push-devices/$deviceId',
      );

  Future<void> logout() async {
    final accountRef = api.tokens?.userRef?.wire;
    try {
      try {
        await api.sendJson('POST', '/api/v1/auth/logout');
      } on Object {
        // Explicit logout is locally authoritative even if the instance is
        // unreachable. The refresh token is still removed below.
      }
    } finally {
      _clearPendingVaultKey();
      discardPendingE2eeRecoveryAuthorization();
      try {
        if (accountRef != null) await passwordVault.clear(accountRef);
      } finally {
        await api.clearTokens();
      }
    }
  }
}

/// Serializes a complete bottom-to-top role ordering for the qualified guild
/// authority API. Role IDs are authority-local snowflakes; every item needs an
/// optimistic-lock version.
List<Map<String, Object?>> guildRolePositionRequest(List<KaedeRole> roles) {
  final positions = <Map<String, Object?>>[];
  for (var index = 0; index < roles.length; index++) {
    final role = roles[index];
    final version = role.version;
    if (version == null || version.isEmpty) {
      throw StateError('Refresh the guild before reordering its roles.');
    }
    positions.add(<String, Object?>{
      'id': role.ref.id.value,
      'position': index + 1,
      'version': version,
    });
  }
  return positions;
}

final class MfaRequired implements Exception {
  const MfaRequired(this.ticket);
  final String ticket;
}
