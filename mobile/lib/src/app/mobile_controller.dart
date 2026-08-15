import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/message_store.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_navigation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/platform/notification_policy.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:local_auth/local_auth.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

enum SessionPhase { restoring, locked, signedOut, authenticating, ready }

enum DegradedFeature {
  accountSettings,
  guildNavigation,
  guildNotifications,
  readStates,
  acknowledgements,
}

String _lastConversationKey(String accountKey) =>
    'last_conversation:${Uri.encodeComponent(accountKey)}';

const _pushTransport = String.fromEnvironment(
  'KAEDE_PUSH_TRANSPORT',
  defaultValue: 'relay',
);
const _pinnedPushRelayUrl = String.fromEnvironment(
  'KAEDE_PUSH_RELAY_URL',
  defaultValue: 'https://push.kaede.chat',
);
const _pinnedPushRelayOrigin = String.fromEnvironment(
  'KAEDE_PUSH_RELAY_ORIGIN',
  defaultValue: 'kaede.chat',
);
const _pushApplicationId = String.fromEnvironment(
  'KAEDE_PUSH_APP_ID',
  defaultValue: 'chat.kaede.mobile',
);

String _randomPushToken() {
  final random = Random.secure();
  final bytes = List<int>.generate(32, (_) => random.nextInt(256));
  return base64UrlEncode(bytes).replaceAll('=', '');
}

bool notificationPreviewsEnabled(Map<String, bool> settings) =>
    settings['show_notification_previews'] ?? true;

bool hasRegisteredPushInstallation(
  Iterable<Map<String, Object?>> devices,
  String installationId,
) =>
    devices.any(
      (device) =>
          '${device['id'] ?? ''}' == installationId &&
          device['enabled'] == true,
    );

/// Resolves a persisted composite reference against the conversations the
/// account can currently access. DMs are the deterministic first fallback,
/// followed by guild text/announcement channels in server and channel order.
KaedeChannel? resolveInitialConversation({
  required Object? saved,
  required List<KaedeChannel> dms,
  required List<KaedeGuild> guilds,
}) {
  EntityRef? savedRef;
  try {
    if (saved != null) savedRef = EntityRef.fromJson(saved);
  } on FormatException {
    // A stale or legacy preference must not prevent session restoration.
  }
  final accessible = <KaedeChannel>[
    ...dms.where((channel) => channel.type == ChannelType.dm),
    for (final guild in guilds)
      ...([...guild.channels]..sort((a, b) {
              final position = a.position.compareTo(b.position);
              return position != 0
                  ? position
                  : a.ref.wire.compareTo(b.ref.wire);
            }))
          .where((channel) =>
              channel.type == ChannelType.text ||
              channel.type == ChannelType.announcement),
  ];
  for (final channel in accessible) {
    if (channel.ref == savedRef) return channel;
  }
  return accessible.firstOrNull;
}

final class ReadBadgeSnapshot {
  const ReadBadgeSnapshot({required this.unread, required this.mentions});

  final Map<EntityRef, int> unread;
  final Map<EntityRef, int> mentions;
}

/// Decodes the authoritative REST read-state payload without discarding the
/// composite origin domain. The API currently exposes unread as a boolean,
/// while live events may expose an integer count.
ReadBadgeSnapshot decodeReadBadgeSnapshot(
  Iterable<Map<String, Object?>> readStates,
) {
  final unread = <EntityRef, int>{};
  final mentions = <EntityRef, int>{};
  for (final item in readStates) {
    final id = item['channel_id'];
    final domain = item['channel_domain'];
    if (id == null || domain == null) continue;
    EntityRef channel;
    try {
      channel = EntityRef(Snowflake('$id'), Domain('$domain'));
    } on Object {
      continue;
    }
    final rawUnread = item['unread_count'] ?? item['unread'];
    final unreadCount = rawUnread == true
        ? 1
        : rawUnread is num
            ? rawUnread.toInt()
            : int.tryParse('$rawUnread') ?? 0;
    final mentionCount = item['mention_count'] is num
        ? (item['mention_count']! as num).toInt()
        : int.tryParse('${item['mention_count'] ?? 0}') ?? 0;
    if (unreadCount > 0) unread[channel] = unreadCount;
    if (mentionCount > 0) mentions[channel] = mentionCount;
  }
  return ReadBadgeSnapshot(
    unread: Map.unmodifiable(unread),
    mentions: Map.unmodifiable(mentions),
  );
}

ReadBadgeSnapshot reconcileAuthoritativeChannelBadge({
  required Map<EntityRef, int> currentUnread,
  required Map<EntityRef, int> currentMentions,
  required ReadBadgeSnapshot authoritative,
  required EntityRef channel,
}) {
  final unread = Map<EntityRef, int>.of(currentUnread);
  final mentions = Map<EntityRef, int>.of(currentMentions);
  if (authoritative.unread[channel] case final count?) {
    unread[channel] = count;
  } else {
    unread.remove(channel);
  }
  if (authoritative.mentions[channel] case final count?) {
    mentions[channel] = count;
  } else {
    mentions.remove(channel);
  }
  return ReadBadgeSnapshot(
    unread: Map.unmodifiable(unread),
    mentions: Map.unmodifiable(mentions),
  );
}

List<KaedeGuild> preserveGuildHistorySync(
  List<KaedeGuild> current,
  List<KaedeGuild> incoming,
) {
  final currentByRef = {for (final guild in current) guild.ref: guild};
  return incoming.map((guild) {
    final previous = currentByRef[guild.ref];
    if (guild.historySyncStatus != null ||
        previous?.historySyncStatus == null) {
      return guild;
    }
    return KaedeGuild.fromJson(<String, Object?>{
      ...guild.toJson(),
      'history_sync_status': previous?.historySyncStatus,
      'history_sync_error_code': previous?.historySyncErrorCode,
      'history_sync_retry_after_ms': previous?.historySyncRetryAfterMs,
      'history_sync_resource': previous?.historySyncResource,
    });
  }).toList(growable: false);
}

Map<String, String> decodeGuildNotificationLevels(
  Iterable<Map<String, Object?>> settings,
) {
  final levels = <String, String>{};
  for (final item in settings) {
    final id = item['guild_id'];
    final domain = item['guild_domain'];
    if (id == null || domain == null) continue;
    levels['$id@$domain'] = '${item['level'] ?? 'mentions'}';
  }
  return Map.unmodifiable(levels);
}

bool shouldAcknowledgeVisibleChannel({
  required bool appActive,
  required bool conversationPaneVisible,
  required EntityRef? selectedChannel,
  required EntityRef channel,
}) =>
    appActive && conversationPaneVisible && selectedChannel == channel;

final class TypingParticipant {
  const TypingParticipant({
    required this.user,
    required this.name,
    required this.expiresAt,
  });

  final EntityRef user;
  final String name;
  final DateTime expiresAt;
}

final class _PendingAcknowledgement {
  const _PendingAcknowledgement({
    required this.message,
    required this.unread,
    required this.mentions,
    this.attempt = 0,
    this.restored = false,
  });

  final EntityRef message;
  final int unread;
  final int mentions;
  final int attempt;
  final bool restored;

  _PendingAcknowledgement copyWith({
    EntityRef? message,
    int? unread,
    int? mentions,
    int? attempt,
    bool? restored,
  }) =>
      _PendingAcknowledgement(
        message: message ?? this.message,
        unread: unread ?? this.unread,
        mentions: mentions ?? this.mentions,
        attempt: attempt ?? this.attempt,
        restored: restored ?? this.restored,
      );
}

final class MobileState {
  const MobileState({
    this.phase = SessionPhase.restoring,
    this.user,
    this.guilds = const <KaedeGuild>[],
    this.guildNavigation = const GuildNavigation(),
    this.dms = const <KaedeChannel>[],
    this.relationships = const <Map<String, Object?>>[],
    this.selectedGuild,
    this.selectedChannel,
    this.messageStore = const <EntityRef, List<KaedeMessage>>{},
    this.drafts = const <EntityRef, String>{},
    this.loadingChannels = const <EntityRef>{},
    this.channelsWithOlderMessages = const <EntityRef>{},
    this.outbox = const <OutboxItem>[],
    this.presencePreference = PresenceStatus.online,
    this.notificationSettings = const <String, bool>{},
    this.guildNotificationLevels = const <String, String>{},
    this.unreadCounts = const <EntityRef, int>{},
    this.mentionCounts = const <EntityRef, int>{},
    this.typingByChannel = const <EntityRef, List<TypingParticipant>>{},
    this.presenceByUser = const <EntityRef, PresenceStatus>{},
    this.userProfiles = const <EntityRef, KaedeUser>{},
    this.selfModerationByGuild = const <EntityRef, GuildSelfModerationStatus>{},
    this.gatewayHealth = const GatewayHealth(
      GatewayConnectionPhase.offline,
      message: 'Realtime updates are not connected.',
    ),
    this.degradedWarnings = const <DegradedFeature, String>{},
    this.gatewayProtocolWarning,
    this.pushWarning,
    this.offline = false,
    this.error,
  });

  final SessionPhase phase;
  final KaedeUser? user;
  final List<KaedeGuild> guilds;
  final GuildNavigation guildNavigation;
  final List<KaedeChannel> dms;
  final List<Map<String, Object?>> relationships;
  final EntityRef? selectedGuild;
  final EntityRef? selectedChannel;
  final Map<EntityRef, List<KaedeMessage>> messageStore;
  final Map<EntityRef, String> drafts;
  final Set<EntityRef> loadingChannels;
  final Set<EntityRef> channelsWithOlderMessages;
  final List<OutboxItem> outbox;
  final PresenceStatus presencePreference;
  final Map<String, bool> notificationSettings;
  final Map<String, String> guildNotificationLevels;
  final Map<EntityRef, int> unreadCounts;
  final Map<EntityRef, int> mentionCounts;
  final Map<EntityRef, List<TypingParticipant>> typingByChannel;
  final Map<EntityRef, PresenceStatus> presenceByUser;
  final Map<EntityRef, KaedeUser> userProfiles;
  final Map<EntityRef, GuildSelfModerationStatus> selfModerationByGuild;
  final GatewayHealth gatewayHealth;
  final Map<DegradedFeature, String> degradedWarnings;
  final String? gatewayProtocolWarning;
  final String? pushWarning;
  final bool offline;
  final String? error;

  KaedeGuild? get activeGuild {
    for (final guild in guilds) {
      if (guild.ref == selectedGuild) return guild;
    }
    return null;
  }

  KaedeChannel? get activeChannel {
    for (final channel in <KaedeChannel>[...dms, ...?activeGuild?.channels]) {
      if (channel.ref == selectedChannel) return channel;
    }
    return null;
  }

  GuildSelfModerationStatus? get activeModerationStatus {
    final guild = selectedGuild;
    if (guild == null) return null;
    final status = selfModerationByGuild[guild];
    return status?.activeAt() == true ? status : null;
  }

  List<KaedeMessage> get messages => selectedChannel == null
      ? const <KaedeMessage>[]
      : messageStore[selectedChannel] ?? const <KaedeMessage>[];

  bool get loadingMessages =>
      selectedChannel != null && loadingChannels.contains(selectedChannel);

  List<OutboxItem> get pendingMessages => selectedChannel == null
      ? const <OutboxItem>[]
      : outbox
          .where((item) => item.channelRef == selectedChannel!.wire)
          .toList(growable: false);

  MobileState copyWith({
    SessionPhase? phase,
    KaedeUser? user,
    bool clearUser = false,
    List<KaedeGuild>? guilds,
    GuildNavigation? guildNavigation,
    List<KaedeChannel>? dms,
    List<Map<String, Object?>>? relationships,
    EntityRef? selectedGuild,
    bool clearGuild = false,
    EntityRef? selectedChannel,
    bool clearChannel = false,
    Map<EntityRef, List<KaedeMessage>>? messageStore,
    Map<EntityRef, String>? drafts,
    Set<EntityRef>? loadingChannels,
    Set<EntityRef>? channelsWithOlderMessages,
    List<OutboxItem>? outbox,
    PresenceStatus? presencePreference,
    Map<String, bool>? notificationSettings,
    Map<String, String>? guildNotificationLevels,
    Map<EntityRef, int>? unreadCounts,
    Map<EntityRef, int>? mentionCounts,
    Map<EntityRef, List<TypingParticipant>>? typingByChannel,
    Map<EntityRef, PresenceStatus>? presenceByUser,
    Map<EntityRef, KaedeUser>? userProfiles,
    Map<EntityRef, GuildSelfModerationStatus>? selfModerationByGuild,
    GatewayHealth? gatewayHealth,
    Map<DegradedFeature, String>? degradedWarnings,
    String? gatewayProtocolWarning,
    bool clearGatewayProtocolWarning = false,
    String? pushWarning,
    bool clearPushWarning = false,
    bool? offline,
    String? error,
    bool clearError = false,
  }) =>
      MobileState(
        phase: phase ?? this.phase,
        user: clearUser ? null : user ?? this.user,
        guilds: guilds ?? this.guilds,
        guildNavigation: guildNavigation ?? this.guildNavigation,
        dms: dms ?? this.dms,
        relationships: relationships ?? this.relationships,
        selectedGuild: clearGuild ? null : selectedGuild ?? this.selectedGuild,
        selectedChannel:
            clearChannel ? null : selectedChannel ?? this.selectedChannel,
        messageStore: messageStore ?? this.messageStore,
        drafts: drafts ?? this.drafts,
        loadingChannels: loadingChannels ?? this.loadingChannels,
        channelsWithOlderMessages:
            channelsWithOlderMessages ?? this.channelsWithOlderMessages,
        outbox: outbox ?? this.outbox,
        presencePreference: presencePreference ?? this.presencePreference,
        notificationSettings: notificationSettings ?? this.notificationSettings,
        guildNotificationLevels:
            guildNotificationLevels ?? this.guildNotificationLevels,
        unreadCounts: unreadCounts ?? this.unreadCounts,
        mentionCounts: mentionCounts ?? this.mentionCounts,
        typingByChannel: typingByChannel ?? this.typingByChannel,
        presenceByUser: presenceByUser ?? this.presenceByUser,
        userProfiles: userProfiles ?? this.userProfiles,
        selfModerationByGuild:
            selfModerationByGuild ?? this.selfModerationByGuild,
        gatewayHealth: gatewayHealth ?? this.gatewayHealth,
        degradedWarnings: degradedWarnings ?? this.degradedWarnings,
        gatewayProtocolWarning: clearGatewayProtocolWarning
            ? null
            : gatewayProtocolWarning ?? this.gatewayProtocolWarning,
        pushWarning: clearPushWarning ? null : pushWarning ?? this.pushWarning,
        offline: offline ?? this.offline,
        error: clearError ? null : error ?? this.error,
      );
}

final mobileControllerProvider =
    StateNotifierProvider<MobileController, MobileState>((ref) {
  final controller = MobileController(
    ref.watch(repositoryProvider),
    ref.watch(apiClientProvider),
    ref.watch(gatewayProvider),
    ref.watch(localDatabaseProvider),
    ref.watch(pushServiceProvider),
  );
  ref.onDispose(controller.dispose);
  return controller..restore();
});

final class MobileController extends StateNotifier<MobileState> {
  MobileController(
    this.repository,
    this.api,
    this.gateway,
    this.database,
    this.push,
  ) : super(const MobileState());

  final KaedeRepository repository;
  final KaedeApiClient api;
  final GatewayClient gateway;
  final LocalDatabase database;
  final PushService push;
  bool get remotePushAvailable => push.remotePushAvailable;
  bool get usesPushRelay => _pushTransport == 'relay';
  String get pushRelayHost => Uri.parse(_pinnedPushRelayUrl).host;
  StreamSubscription<GatewayEvent>? _gatewaySubscription;
  StreamSubscription<GatewayHealth>? _gatewayHealthSubscription;
  StreamSubscription<String>? _pushTokenSubscription;
  StreamSubscription<PushDestination>? _pushDestinationSubscription;
  StreamSubscription<String?>? _pushHealthSubscription;
  StreamSubscription<void>? _sessionExpiredSubscription;
  Timer? _outboxTimer;
  Timer? _appLockTimer;
  Timer? _typingExpiryTimer;
  Timer? _navigationRefreshTimer;
  Timer? _metadataCacheTimer;
  Timer? _acknowledgementRetryTimer;
  Timer? _selfModerationExpiryTimer;
  Timer? _selfModerationRetryTimer;
  String? _pushDeviceId;
  String? _pushRegistrationWarning;
  String? _pushRemoteDeliveryWarning;
  String? _pushLocalDisplayWarning;
  var _appActive = true;
  var _conversationPaneVisible = false;
  DateTime? _backgroundedAt;
  final Map<EntityRef, int> _messageRequestGenerations = <EntityRef, int>{};
  var _flushingOutbox = false;
  var _sessionLoadGeneration = 0;
  var _authenticationGeneration = 0;
  var _selfModerationRequestGeneration = 0;
  Future<void>? _navigationRefresh;
  Future<void>? _notificationActivation;
  Future<void> _cacheWriteTail = Future<void>.value();
  String? _activeAccountKey;
  final Map<EntityRef, _PendingAcknowledgement> _pendingAcknowledgements =
      <EntityRef, _PendingAcknowledgement>{};
  final Set<EntityRef> _acknowledgementsInFlight = <EntityRef>{};
  var _malformedGatewayEvents = 0;
  var _validGatewayEventsAfterWarning = 0;

  void setAppActive(bool active) {
    final becameActive = active && !_appActive;
    _appActive = active;
    push.setAppVisibility(
      active: active,
      visibleChannel:
          active && _conversationPaneVisible ? state.selectedChannel : null,
    );
    if (active) {
      _appLockTimer?.cancel();
      _appLockTimer = null;
      unawaited(_lockAfterBackgroundIfNeeded());
      if (becameActive) {
        unawaited(retryRealtime(foreground: true));
      }
      unawaited(_flushOutbox());
      unawaited(_refreshReadBadges());
      unawaited(_activateNotifications());
      if (_conversationPaneVisible) {
        if (state.selectedGuild case final guild?) {
          unawaited(refreshSelfModeration(guild));
        }
      }
      _acknowledgeVisibleConversation();
    } else {
      _selfModerationRetryTimer?.cancel();
      _selfModerationRetryTimer = null;
      _backgroundedAt ??= DateTime.now();
      unawaited(_scheduleAppLock());
    }
  }

  void setConversationPaneVisible(bool visible) {
    _conversationPaneVisible = visible;
    push.setAppVisibility(
      active: _appActive,
      visibleChannel: _appActive && visible ? state.selectedChannel : null,
    );
    if (visible) {
      _acknowledgeVisibleConversation();
      if (state.selectedGuild case final guild?) {
        unawaited(refreshSelfModeration(guild));
      }
    } else {
      _selfModerationRetryTimer?.cancel();
      _selfModerationRetryTimer = null;
    }
  }

  bool _isChannelVisible(EntityRef channel) => shouldAcknowledgeVisibleChannel(
        appActive: _appActive,
        conversationPaneVisible: _conversationPaneVisible,
        selectedChannel: state.selectedChannel,
        channel: channel,
      );

  void _setDegradedWarning(DegradedFeature feature, String? warning) {
    final warnings = Map<DegradedFeature, String>.of(state.degradedWarnings);
    if (warning == null) {
      warnings.remove(feature);
    } else {
      warnings[feature] = warning;
    }
    state = state.copyWith(degradedWarnings: Map.unmodifiable(warnings));
  }

  void _applyGatewayHealth(GatewayHealth health) {
    if (state.phase != SessionPhase.ready) return;
    state = state.copyWith(gatewayHealth: health);
  }

  void _setPushRegistrationWarning(String? warning) {
    _pushRegistrationWarning = warning;
    _publishPushWarning();
  }

  void _setPushRemoteDeliveryWarning(String? warning) {
    _pushRemoteDeliveryWarning = warning;
    _publishPushWarning();
  }

  void _setPushLocalDisplayWarning(String? warning) {
    _pushLocalDisplayWarning = warning;
    _publishPushWarning();
  }

  void _publishPushWarning() {
    if (state.phase != SessionPhase.ready) return;
    final warnings = <String>[
      if (_pushRegistrationWarning != null) _pushRegistrationWarning!,
      if (_pushRemoteDeliveryWarning != null) _pushRemoteDeliveryWarning!,
      if (_pushLocalDisplayWarning != null) _pushLocalDisplayWarning!,
    ];
    final warning = warnings.isEmpty ? null : warnings.join(' ');
    state = warning == null
        ? state.copyWith(clearPushWarning: true)
        : state.copyWith(pushWarning: warning);
  }

  Future<void> retryRealtime({bool foreground = false}) async {
    final tokens = api.tokens;
    if (tokens == null || state.phase != SessionPhase.ready) return;
    try {
      if (foreground) {
        await gateway.reconnectAfterForeground(tokens);
      } else {
        await gateway.connect(tokens);
      }
    } on Object {
      // GatewayClient keeps retrying and publishes the actionable state.
    }
  }

  void _acknowledgeVisibleConversation() {
    final channel = state.selectedChannel;
    if (channel == null || !_isChannelVisible(channel)) return;
    final messages = state.messageStore[channel];
    if (messages == null || messages.isEmpty) {
      unawaited(loadMessages());
      return;
    }
    unawaited(_acknowledge(channel, messages.last.ref));
  }

  Future<void> _scheduleAppLock() async {
    final preferences = await SharedPreferences.getInstance();
    if (!(preferences.getBool('biometric_lock') ?? false) ||
        state.phase != SessionPhase.ready) {
      return;
    }
    final seconds = preferences.getInt('biometric_lock_timeout_seconds') ?? 30;
    _appLockTimer?.cancel();
    _appLockTimer = Timer(Duration(seconds: seconds), () {
      if (!_appActive && state.phase == SessionPhase.ready) {
        state = state.copyWith(phase: SessionPhase.locked, clearError: true);
      }
    });
  }

  Future<void> _lockAfterBackgroundIfNeeded() async {
    final backgroundedAt = _backgroundedAt;
    _backgroundedAt = null;
    if (backgroundedAt == null || state.phase != SessionPhase.ready) return;
    final preferences = await SharedPreferences.getInstance();
    if (!(preferences.getBool('biometric_lock') ?? false)) return;
    final seconds = preferences.getInt('biometric_lock_timeout_seconds') ?? 30;
    if (DateTime.now().difference(backgroundedAt) >=
        Duration(seconds: seconds)) {
      state = state.copyWith(phase: SessionPhase.locked, clearError: true);
    }
  }

  Future<void> restore() async {
    final authenticationGeneration = ++_authenticationGeneration;
    try {
      final tokens = await api.restore();
      if (authenticationGeneration != _authenticationGeneration) return;
      if (tokens == null) {
        state = state.copyWith(phase: SessionPhase.signedOut);
        return;
      }
      final preferences = await SharedPreferences.getInstance();
      if (authenticationGeneration != _authenticationGeneration) return;
      if (preferences.getBool('biometric_lock') ?? false) {
        state = state.copyWith(phase: SessionPhase.locked);
        return;
      }
      await _loadSession(tokens);
    } on Object catch (error) {
      if (authenticationGeneration != _authenticationGeneration) return;
      final tokens = api.tokens;
      if (tokens != null &&
          _isOffline(error) &&
          await _loadCached(tokens, authenticationGeneration)) {
        await _startSessionServices(
          tokens.accountKey,
          _sessionLoadGeneration,
        );
        return;
      }
      if (error is KaedeException && error.status == 401) {
        await api.clearTokens();
      }
      state =
          MobileState(phase: SessionPhase.signedOut, error: _message(error));
    }
  }

  Future<void> unlock() async {
    final authenticationGeneration = ++_authenticationGeneration;
    try {
      final authenticated = await LocalAuthentication().authenticate(
        localizedReason: 'Unlock Kaede Chat',
        options: const AuthenticationOptions(
          biometricOnly: false,
          stickyAuth: true,
        ),
      );
      if (!authenticated ||
          authenticationGeneration != _authenticationGeneration) {
        return;
      }
      _backgroundedAt = null;
      _appActive = true;
      push.setAppVisibility(
        active: true,
        visibleChannel: _conversationPaneVisible ? state.selectedChannel : null,
      );
      if (state.user != null) {
        state = state.copyWith(phase: SessionPhase.ready, clearError: true);
        unawaited(_flushOutbox());
        return;
      }
      state = state.copyWith(phase: SessionPhase.restoring, clearError: true);
      final tokens = api.tokens ?? await api.restore();
      if (authenticationGeneration != _authenticationGeneration) return;
      if (tokens == null) {
        state = state.copyWith(phase: SessionPhase.signedOut);
        return;
      }
      await _loadSession(tokens);
    } on Object catch (error) {
      if (authenticationGeneration != _authenticationGeneration) return;
      state =
          state.copyWith(phase: SessionPhase.locked, error: _message(error));
    }
  }

  Future<void> login(
      {required Domain instance,
      required String identifier,
      required String password,
      String? turnstileToken}) async {
    final authenticationGeneration = ++_authenticationGeneration;
    state =
        state.copyWith(phase: SessionPhase.authenticating, clearError: true);
    try {
      final tokens = await repository.login(
        instance: instance,
        identifier: identifier,
        password: password,
        turnstileToken: turnstileToken,
      );
      if (authenticationGeneration != _authenticationGeneration) return;
      await _loadSession(tokens);
    } on MfaRequired {
      if (authenticationGeneration != _authenticationGeneration) return;
      rethrow;
    } on Object catch (error) {
      if (authenticationGeneration != _authenticationGeneration) return;
      state =
          state.copyWith(phase: SessionPhase.signedOut, error: _message(error));
      if (error is KaedeException && error.code == 'TURNSTILE_REQUIRED') {
        rethrow;
      }
    }
  }

  Future<void> finishMfa(Domain instance, String ticket, String code) async {
    final authenticationGeneration = ++_authenticationGeneration;
    state =
        state.copyWith(phase: SessionPhase.authenticating, clearError: true);
    try {
      final tokens = await repository.finishMfa(instance, ticket, code);
      if (authenticationGeneration != _authenticationGeneration) return;
      await _loadSession(tokens);
    } on Object catch (error) {
      if (authenticationGeneration != _authenticationGeneration) return;
      state =
          state.copyWith(phase: SessionPhase.signedOut, error: _message(error));
    }
  }

  Future<void> _loadSession(SessionTokens tokens) async {
    final generation = ++_sessionLoadGeneration;
    _messageRequestGenerations.clear();
    _pushRegistrationWarning = null;
    _pushRemoteDeliveryWarning = null;
    _pushLocalDisplayWarning = null;
    final user = await repository.me();
    if (generation != _sessionLoadGeneration) return;
    final resolved = api.tokens ?? tokens.copyWith(userRef: user.ref);
    await api.useTokens(resolved.copyWith(userRef: user.ref));
    _activeAccountKey = resolved.accountKey;
    final critical = await Future.wait<Object>([
      repository.guilds(),
      repository.dms(),
    ]);
    if (generation != _sessionLoadGeneration) return;
    final relationships = await _optional(
      repository.relationships,
      const <Map<String, Object?>>[],
    );
    final settingsLoad = await _optionalWithWarning(
      repository.settings,
      const <String, Object?>{},
      summary:
          'Account settings could not be loaded. Some preferences may show defaults.',
    );
    final guildNavigationLoad = await _optionalWithWarning(
      repository.guildNavigation,
      const GuildNavigation(),
      summary:
          'Your guild order could not be loaded. Kaede is showing the default order until sync recovers.',
    );
    final guildSettingsLoad = await _optionalWithWarning(
      repository.guildNotificationSettingsList,
      const <Map<String, Object?>>[],
      summary:
          'Guild notification preferences could not be loaded. Mention-only defaults may be shown.',
    );
    final readStatesLoad = await _optionalWithWarning(
      repository.readStates,
      const <Map<String, Object?>>[],
      summary:
          'Unread markers could not be loaded. Counts may be incomplete until sync recovers.',
    );
    if (generation != _sessionLoadGeneration) return;
    final settings = settingsLoad.value;
    final guildSettings = guildSettingsLoad.value;
    final readStates = readStatesLoad.value;
    final rawNotifications = settings['notification_settings'];
    final notifications = rawNotifications is Map<Object?, Object?>
        ? rawNotifications.map((key, value) => MapEntry('$key', value == true))
        : const <String, bool>{};
    final guildNotificationLevels =
        decodeGuildNotificationLevels(guildSettings);
    final presence = PresenceStatus.values.firstWhere(
      (value) => value.name == '${settings['presence_preference'] ?? 'online'}',
      orElse: () => PresenceStatus.online,
    );
    final badges = decodeReadBadgeSnapshot(readStates);
    final outbox = await database.outboxForAccount(resolved.accountKey);
    final drafts = _decodeDrafts(
      await database.snapshots(resolved.accountKey, 'drafts'),
    );
    if (generation != _sessionLoadGeneration) return;
    final guilds = critical[0] as List<KaedeGuild>;
    final dms = critical[1] as List<KaedeChannel>;
    final guildNavigation = reconcileGuildNavigation(
      guildNavigationLoad.value,
      guilds,
    );
    final preferences = await SharedPreferences.getInstance();
    final initial = resolveInitialConversation(
      saved: preferences.getString(_lastConversationKey(resolved.accountKey)),
      dms: dms,
      guilds: guilds,
    );
    final degradedWarnings = <DegradedFeature, String>{
      if (settingsLoad.warning case final warning?)
        DegradedFeature.accountSettings: warning,
      if (guildNavigationLoad.warning case final warning?)
        DegradedFeature.guildNavigation: warning,
      if (guildSettingsLoad.warning case final warning?)
        DegradedFeature.guildNotifications: warning,
      if (readStatesLoad.warning case final warning?)
        DegradedFeature.readStates: warning,
    };
    state = MobileState(
      phase: SessionPhase.ready,
      user: user,
      guilds: guilds,
      guildNavigation: guildNavigation,
      dms: dms,
      selectedGuild: initial?.guildRef,
      selectedChannel: initial?.ref,
      relationships: relationships,
      presencePreference: presence,
      notificationSettings: notifications,
      guildNotificationLevels: guildNotificationLevels,
      unreadCounts: badges.unread,
      mentionCounts: badges.mentions,
      outbox: outbox,
      drafts: Map.unmodifiable(drafts),
      gatewayHealth: gateway.currentHealth,
      degradedWarnings: Map.unmodifiable(degradedWarnings),
      offline: false,
    );
    push.setAppVisibility(
      active: _appActive,
      visibleChannel:
          _appActive && _conversationPaneVisible ? initial?.ref : null,
    );
    if (initial != null) {
      unawaited(loadMessages());
      if (initial.guildRef case final guild?) {
        unawaited(refreshSelfModeration(guild));
      }
    }
    await _cacheLists();
    await _startSessionServices(resolved.accountKey, generation);
  }

  Future<void> _startSessionServices(
    String accountKey,
    int generation,
  ) async {
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    await _gatewaySubscription?.cancel();
    await _gatewayHealthSubscription?.cancel();
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    _gatewayHealthSubscription = gateway.health.listen(_applyGatewayHealth);
    state = state.copyWith(gatewayHealth: gateway.currentHealth);
    _gatewaySubscription = gateway.events.listen(_reduceGateway);
    try {
      final tokens = api.tokens;
      if (tokens == null || tokens.accountKey != accountKey) return;
      await gateway.connect(tokens);
    } on Object {
      // REST and the offline cache remain usable while the realtime transport
      // reconnects independently.
    }
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    await _sessionExpiredSubscription?.cancel();
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    _sessionExpiredSubscription = api.sessionExpired.listen(
      (_) => unawaited(_expireSession()),
    );
    await _pushTokenSubscription?.cancel();
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    _pushTokenSubscription = push.tokenRefresh.listen(
      (token) => unawaited(_registerPushDevice(token: token)),
    );
    await _pushDestinationSubscription?.cancel();
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    _pushDestinationSubscription = push.destinations.listen(
      (destination) => unawaited(openPushDestination(destination)),
    );
    await _pushHealthSubscription?.cancel();
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    _pushHealthSubscription = push.health.listen(_setPushRemoteDeliveryWarning);
    if (push.consumeInitialDestination() case final destination?) {
      unawaited(openPushDestination(destination));
    }
    unawaited(_activateNotifications());
    unawaited(_flushOutbox());
    _outboxTimer?.cancel();
    _outboxTimer = Timer.periodic(
      const Duration(seconds: 10),
      (_) => unawaited(_flushOutbox()),
    );
  }

  Future<void> refreshNavigation() {
    if (_navigationRefresh case final pending?) return pending;
    final pending = _refreshNavigation();
    _navigationRefresh = pending;
    pending.whenComplete(() {
      if (identical(_navigationRefresh, pending)) _navigationRefresh = null;
    });
    return pending;
  }

  void _scheduleNavigationRefresh() {
    _navigationRefreshTimer?.cancel();
    _navigationRefreshTimer = Timer(
      const Duration(milliseconds: 180),
      () => unawaited(refreshNavigation()),
    );
  }

  Future<void> _refreshNavigation() async {
    final generation = _sessionLoadGeneration;
    final accountKey = api.tokens?.accountKey ?? _activeAccountKey;
    if (accountKey == null || state.phase != SessionPhase.ready) return;
    try {
      final results = await Future.wait<Object>([
        repository.me(),
        repository.guilds(),
        repository.dms(),
        repository.readStates(),
      ]);
      if (generation != _sessionLoadGeneration ||
          state.phase != SessionPhase.ready ||
          api.tokens?.accountKey != accountKey) {
        return;
      }
      final badges = decodeReadBadgeSnapshot(
        results[3] as List<Map<String, Object?>>,
      );
      final refreshedGuilds = preserveGuildHistorySync(
        state.guilds,
        results[1] as List<KaedeGuild>,
      );
      state = state.copyWith(
        user: results[0] as KaedeUser,
        guilds: refreshedGuilds,
        guildNavigation: reconcileGuildNavigation(
          state.guildNavigation,
          refreshedGuilds,
        ),
        dms: results[2] as List<KaedeChannel>,
        unreadCounts: badges.unread,
        mentionCounts: badges.mentions,
        offline: false,
        clearError: true,
      );
      _setDegradedWarning(DegradedFeature.readStates, null);
      await _cacheLists();
      unawaited(_refreshRelationships());
      unawaited(_retryGuildNavigation());
    } on Object catch (error) {
      if (_sessionIsCurrent(accountKey, generation)) {
        state = state.copyWith(error: _message(error));
      }
    }
  }

  Future<void> _refreshRelationships() async {
    final generation = _sessionLoadGeneration;
    final accountKey = api.tokens?.accountKey;
    if (accountKey == null || state.phase != SessionPhase.ready) return;
    try {
      final relationships = await repository.relationships();
      if (generation != _sessionLoadGeneration ||
          state.phase != SessionPhase.ready ||
          api.tokens?.accountKey != accountKey) {
        return;
      }
      state = state.copyWith(relationships: relationships);
      await _cacheLists();
    } on Object {
      // Relationships are optional navigation metadata. A temporary outage
      // must not roll back otherwise-current guild and DM state.
    }
  }

  void applySettings(Map<String, Object?> settings) {
    final rawNotifications = settings['notification_settings'];
    final notifications = rawNotifications is Map<Object?, Object?>
        ? rawNotifications.map((key, value) => MapEntry('$key', value == true))
        : state.notificationSettings;
    final presence = PresenceStatus.values.firstWhere(
      (value) =>
          value.name ==
          '${settings['presence_preference'] ?? state.presencePreference.name}',
      orElse: () => state.presencePreference,
    );
    state = state.copyWith(
      notificationSettings: notifications,
      presencePreference: presence,
    );
    _setDegradedWarning(DegradedFeature.accountSettings, null);
  }

  Future<void> retryDegradedData() async {
    if (state.phase != SessionPhase.ready) return;
    final pending = Set<DegradedFeature>.of(state.degradedWarnings.keys);
    await Future.wait<void>([
      if (pending.contains(DegradedFeature.accountSettings))
        _retryAccountSettings(),
      if (pending.contains(DegradedFeature.guildNavigation))
        _retryGuildNavigation(),
      if (pending.contains(DegradedFeature.guildNotifications))
        _retryGuildNotificationSettings(),
      if (pending.contains(DegradedFeature.readStates)) _refreshReadBadges(),
      if (pending.contains(DegradedFeature.acknowledgements))
        _retryPendingAcknowledgements(),
    ]);
  }

  Future<void> _retryAccountSettings() async {
    try {
      applySettings(await repository.settings());
      _scheduleMetadataCache();
    } on Object catch (error) {
      _setDegradedWarning(
        DegradedFeature.accountSettings,
        userFacingError(
          error,
          summary:
              'Account settings still could not be loaded. Some preferences may show defaults.',
        ),
      );
    }
  }

  Future<void> _retryGuildNavigation() async {
    try {
      final navigation = reconcileGuildNavigation(
        await repository.guildNavigation(),
        state.guilds,
      );
      state = state.copyWith(guildNavigation: navigation);
      _setDegradedWarning(DegradedFeature.guildNavigation, null);
    } on Object catch (error) {
      _setDegradedWarning(
        DegradedFeature.guildNavigation,
        userFacingError(
          error,
          summary:
              'Your guild order still could not be loaded. Kaede is showing the default order.',
        ),
      );
    }
  }

  Future<void> saveGuildNavigation(GuildNavigation navigation) async {
    if (state.phase != SessionPhase.ready) return;
    final previous = state.guildNavigation;
    final optimistic = reconcileGuildNavigation(navigation, state.guilds);
    state = state.copyWith(guildNavigation: optimistic, clearError: true);
    try {
      final saved = await repository.updateGuildNavigation(optimistic);
      state = state.copyWith(
        guildNavigation: reconcileGuildNavigation(saved, state.guilds),
      );
      _setDegradedWarning(DegradedFeature.guildNavigation, null);
    } on Object catch (error) {
      state = state.copyWith(
        guildNavigation: previous,
        error: userFacingError(
          error,
          summary:
              'Could not save your guild order. The previous layout was restored.',
        ),
      );
    }
  }

  Future<void> _retryGuildNotificationSettings() async {
    try {
      final levels = decodeGuildNotificationLevels(
        await repository.guildNotificationSettingsList(),
      );
      state = state.copyWith(guildNotificationLevels: levels);
      _setDegradedWarning(DegradedFeature.guildNotifications, null);
      _scheduleMetadataCache();
    } on Object catch (error) {
      _setDegradedWarning(
        DegradedFeature.guildNotifications,
        userFacingError(
          error,
          summary: 'Guild notification preferences still could not be loaded.',
        ),
      );
    }
  }

  Future<void> setPresence(PresenceStatus presence) async {
    final previous = state.presencePreference;
    state = state.copyWith(presencePreference: presence, clearError: true);
    gateway.updatePresence(presence.name);
    try {
      final settings = await repository.updateSettings(
        <String, Object?>{'presence_preference': presence.name},
      );
      applySettings(settings);
      _scheduleMetadataCache();
    } on Object catch (error) {
      state = state.copyWith(
        presencePreference: previous,
        error: _message(error),
      );
      gateway.updatePresence(previous.name);
    }
  }

  Future<void> selectGuild(KaedeGuild guild) {
    state = state.copyWith(selectedGuild: guild.ref, clearChannel: true);
    push.setAppVisibility(active: _appActive);
    unawaited(refreshSelfModeration(guild.ref));
    return Future<void>.value();
  }

  void selectHome() {
    state = state.copyWith(clearGuild: true, clearChannel: true);
    push.setAppVisibility(active: _appActive);
  }

  Future<void> selectDm(KaedeChannel channel) async {
    state = state.copyWith(clearGuild: true, selectedChannel: channel.ref);
    push.setAppVisibility(
      active: _appActive,
      visibleChannel:
          _appActive && _conversationPaneVisible ? channel.ref : null,
    );
    unawaited(_rememberConversation(channel.ref));
    _acknowledgeVisibleConversation();
    await loadMessages();
  }

  Future<void> selectChannel(KaedeChannel channel) async {
    state = state.copyWith(
      selectedGuild: channel.guildRef ?? state.selectedGuild,
      selectedChannel: channel.ref,
    );
    push.setAppVisibility(
      active: _appActive,
      visibleChannel:
          _appActive && _conversationPaneVisible ? channel.ref : null,
    );
    unawaited(_rememberConversation(channel.ref));
    if (channel.guildRef case final guild?) {
      unawaited(refreshSelfModeration(guild));
    }
    _acknowledgeVisibleConversation();
    if (channel.type == ChannelType.text ||
        channel.type == ChannelType.announcement ||
        channel.type == ChannelType.dm) {
      await loadMessages();
    }
  }

  Future<void> refreshSelfModeration(EntityRef guild) async {
    final request = ++_selfModerationRequestGeneration;
    final accountKey = api.tokens?.accountKey;
    final sessionGeneration = _sessionLoadGeneration;
    if (accountKey == null ||
        !_sessionIsCurrent(accountKey, sessionGeneration)) {
      return;
    }
    try {
      final status = await repository.selfModerationStatus(guild);
      if (request != _selfModerationRequestGeneration ||
          !_sessionIsCurrent(accountKey, sessionGeneration) ||
          status.guildRef != guild) {
        return;
      }
      final statuses = Map<EntityRef, GuildSelfModerationStatus>.of(
          state.selfModerationByGuild);
      if (status.activeAt()) {
        statuses[guild] = status;
      } else {
        statuses.remove(guild);
      }
      state = state.copyWith(selfModerationByGuild: Map.unmodifiable(statuses));
      _scheduleSelfModerationExpiry(status);
      _scheduleSelfModerationRetry(status);
    } on Object {
      // This is an informational private projection. Authoritative permission
      // checks still happen at the guild home, so a transient refresh failure
      // must not obscure chat or invent a moderation state.
      final cached = state.selfModerationByGuild[guild];
      if (cached != null) _scheduleSelfModerationRetry(cached);
    }
  }

  void _scheduleSelfModerationRetry(GuildSelfModerationStatus status) {
    _selfModerationRetryTimer?.cancel();
    _selfModerationRetryTimer = null;
    if (!shouldRetrySelfModerationStatus(
      status,
      appActive: _appActive,
      conversationPaneVisible: _conversationPaneVisible,
      selectedGuild: state.selectedGuild,
    )) {
      return;
    }
    _selfModerationRetryTimer = Timer(const Duration(seconds: 15), () {
      _selfModerationRetryTimer = null;
      unawaited(refreshSelfModeration(status.guildRef));
    });
  }

  void _scheduleSelfModerationExpiry(GuildSelfModerationStatus status) {
    _selfModerationExpiryTimer?.cancel();
    _selfModerationExpiryTimer = null;
    _selfModerationRetryTimer?.cancel();
    _selfModerationRetryTimer = null;
    if (!status.activeAt() || status.timeoutIndefinite) return;
    final until = status.timeoutUntil;
    if (until == null) return;
    final delay = until.difference(DateTime.now().toUtc());
    if (delay <= Duration.zero) {
      _clearSelfModeration(status.guildRef);
      return;
    }
    _selfModerationExpiryTimer =
        Timer(delay + const Duration(milliseconds: 250), () {
      _selfModerationExpiryTimer = null;
      _selfModerationRetryTimer?.cancel();
      _selfModerationRetryTimer = null;
      _clearSelfModeration(status.guildRef);
      unawaited(refreshSelfModeration(status.guildRef));
    });
  }

  void _clearSelfModeration(EntityRef guild) {
    if (!state.selfModerationByGuild.containsKey(guild)) return;
    final statuses = Map<EntityRef, GuildSelfModerationStatus>.of(
        state.selfModerationByGuild)
      ..remove(guild);
    state = state.copyWith(selfModerationByGuild: Map.unmodifiable(statuses));
  }

  Future<void> _rememberConversation(EntityRef channel) async {
    final accountKey = api.tokens?.accountKey ?? _activeAccountKey;
    if (accountKey == null) return;
    final preferences = await SharedPreferences.getInstance();
    await preferences.setString(_lastConversationKey(accountKey), channel.wire);
  }

  Future<bool> openPushDestination(PushDestination destination) async {
    if (state.phase != SessionPhase.ready) return false;
    KaedeChannel? channel;
    KaedeGuild? guild;
    for (final candidate in state.dms) {
      if (candidate.ref == destination.channel) channel = candidate;
    }
    for (final candidateGuild in state.guilds) {
      for (final candidate in candidateGuild.channels) {
        if (candidate.ref == destination.channel) {
          guild = candidateGuild;
          channel = candidate;
        }
      }
    }
    if (channel == null) {
      await refreshNavigation();
      for (final candidate in state.dms) {
        if (candidate.ref == destination.channel) channel = candidate;
      }
      for (final candidateGuild in state.guilds) {
        for (final candidate in candidateGuild.channels) {
          if (candidate.ref == destination.channel) {
            guild = candidateGuild;
            channel = candidate;
          }
        }
      }
    }
    if (channel == null) {
      state =
          state.copyWith(error: 'This conversation is no longer available.');
      return false;
    }
    if (guild != null) {
      state = state.copyWith(selectedGuild: guild.ref);
      await selectChannel(channel);
    } else {
      await selectDm(channel);
    }
    if (destination.message case final message?) {
      await loadAround(message);
    }
    return true;
  }

  Future<void> loadMessages({bool older = false}) async {
    final channel = state.activeChannel;
    if (channel == null || state.loadingChannels.contains(channel.ref)) return;
    final accountKey = api.tokens?.accountKey;
    final sessionGeneration = _sessionLoadGeneration;
    if (accountKey == null ||
        !_sessionIsCurrent(accountKey, sessionGeneration)) {
      return;
    }
    final channelRef = channel.ref;
    final generation = (_messageRequestGenerations[channelRef] ?? 0) + 1;
    _messageRequestGenerations[channelRef] = generation;
    final existing = state.messageStore[channelRef] ?? const <KaedeMessage>[];
    _setChannelLoading(channelRef, true);
    state = state.copyWith(clearError: true);
    try {
      final page = await repository.messages(
        channelRef,
        before: older && existing.isNotEmpty ? existing.first.ref : null,
      );
      if (!_messageRequestIsCurrent(
        channelRef,
        generation,
        accountKey,
        sessionGeneration,
      )) {
        return;
      }
      final ordered = page.reversed.toList();
      final withOlder = Set<EntityRef>.of(state.channelsWithOlderMessages);
      final reachedRetainedStart = channel.historyTruncated &&
          !channel.historyRemoteAvailable &&
          channel.oldestAvailableMessageRef != null &&
          page.isNotEmpty &&
          page.last.ref == channel.oldestAvailableMessageRef;
      final authorityHistoryComplete =
          page.isEmpty || page.last.historyPageComplete;
      final authorityHistoryUnavailable = page.isNotEmpty &&
          page.last.historyPageErrorCode == 'FEDERATED_DM_HISTORY_UNAVAILABLE';
      if ((page.length >= 50 || authorityHistoryUnavailable) &&
          !authorityHistoryComplete &&
          !reachedRetainedStart) {
        withOlder.add(channelRef);
      } else {
        withOlder.remove(channelRef);
      }
      final current = state.messageStore[channelRef] ?? existing;
      var nextMessages = older
          ? mergeMessages(<KaedeMessage>[...ordered, ...current])
          : ordered;
      // An empty successful older page cannot carry the per-page terminal
      // marker. Preserve that state on the oldest retained row so ordinary
      // rebuilds show a real conversation start instead of a cache boundary.
      if (older && page.isEmpty && nextMessages.isNotEmpty) {
        final mutable = nextMessages.toList();
        mutable[0] = mutable[0].copyWith(
          historyPageComplete: true,
          clearHistoryPageErrorCode: true,
        );
        nextMessages = List<KaedeMessage>.unmodifiable(mutable);
      }
      _setChannelMessages(
        channelRef,
        nextMessages,
      );
      state = state.copyWith(
        offline: false,
        channelsWithOlderMessages: Set.unmodifiable(withOlder),
        error: authorityHistoryUnavailable
            ? 'Older messages are temporarily unavailable from this conversation’s home instance. Recent cached messages remain visible; retry in a moment.'
            : null,
        clearError: !authorityHistoryUnavailable,
      );
      await _cacheMessages(channelRef);
      if (!older && ordered.isNotEmpty && _isChannelVisible(channelRef)) {
        unawaited(_acknowledge(channelRef, ordered.last.ref));
      }
    } on Object catch (error) {
      if (_messageRequestIsCurrent(
        channelRef,
        generation,
        accountKey,
        sessionGeneration,
      )) {
        final restored = await _restoreCachedMessages(
          channelRef,
          accountKey,
          sessionGeneration,
        );
        if (!_messageRequestIsCurrent(
          channelRef,
          generation,
          accountKey,
          sessionGeneration,
        )) {
          return;
        }
        state = state.copyWith(
          offline: restored || state.offline,
          error:
              restored ? 'Offline — showing saved messages.' : _message(error),
        );
      }
    } finally {
      if (_messageRequestIsCurrent(
        channelRef,
        generation,
        accountKey,
        sessionGeneration,
      )) {
        _setChannelLoading(channelRef, false);
      }
    }
  }

  Future<void> send(
    EntityRef channel,
    String content, {
    List<EntityRef> attachments = const <EntityRef>[],
    EntityRef? replyTo,
    EntityRef? replyAuthor,
    List<EntityRef> mentionUsers = const <EntityRef>[],
    bool notify = true,
  }) async {
    if (content.trim().isEmpty && attachments.isEmpty) {
      return;
    }
    final tokens = api.tokens;
    if (tokens == null) throw StateError('No active session');
    final generation = _sessionLoadGeneration;
    final accountKey = tokens.accountKey;
    final nonce = const Uuid().v4();
    final payload = <String, Object?>{
      'content': content.trim().isEmpty ? null : content.trim(),
      'attachments': attachments.map((item) => item.wire).toList(),
      if (replyTo != null) 'reply_to': replyTo.wire,
      if (notify && replyAuthor != null) 'reply_author': replyAuthor.wire,
      'mention_users': mentionUsers.map((item) => item.wire).toList(),
    };
    await database.enqueue(
      nonce: nonce,
      accountKey: tokens.accountKey,
      channelRef: channel.wire,
      payload: payload,
    );
    if (!_sessionIsCurrent(accountKey, generation)) {
      await database.deleteOutbox(nonce);
      throw StateError('The active account changed before the message sent.');
    }
    await _syncOutbox();
    await _deliverOutboxItem(OutboxItem(
      nonce: nonce,
      channelRef: channel.wire,
      payload: payload,
      attempts: 0,
      state: 'pending',
      createdAt: DateTime.now(),
    ));
  }

  void setDraft(EntityRef channel, String content) {
    final tokens = api.tokens;
    if (tokens == null || state.phase != SessionPhase.ready) return;
    final normalized =
        content.length <= 4000 ? content : content.substring(0, 4000);
    final drafts = Map<EntityRef, String>.of(state.drafts);
    if (normalized.isEmpty) {
      drafts.remove(channel);
    } else {
      drafts[channel] = normalized;
    }
    state = state.copyWith(drafts: Map.unmodifiable(drafts));
    final accountKey = tokens.accountKey;
    final generation = _sessionLoadGeneration;
    unawaited(_queueCacheWrite(() async {
      if (!_cacheSessionIsCurrent(accountKey, generation)) return;
      if (normalized.isEmpty) {
        await database.removeSnapshot(accountKey, 'drafts', channel.wire);
      } else {
        await database.putSnapshot(
          accountKey,
          'drafts',
          channel.wire,
          <String, Object?>{
            'channel_ref': channel.wire,
            'text': normalized,
          },
        );
      }
    }));
  }

  Future<void> retrySend(String nonce) async {
    await database.retryOutboxNow(nonce);
    await _syncOutbox();
    await _flushOutbox();
  }

  Future<void> discardSend(String nonce) async {
    await database.deleteOutbox(nonce);
    await _syncOutbox();
  }

  Future<void> replaceMessage(KaedeMessage message, String content) async {
    final accountKey = api.tokens?.accountKey;
    final generation = _sessionLoadGeneration;
    if (accountKey == null || !_sessionIsCurrent(accountKey, generation)) {
      return;
    }
    final updated =
        await repository.editMessage(message.channelRef, message.ref, content);
    if (!_sessionIsCurrent(accountKey, generation)) return;
    final messages = state.messageStore[message.channelRef];
    if (messages != null) {
      _setChannelMessages(
        message.channelRef,
        messages
            .map((item) => item.ref == updated.ref ? updated : item)
            .toList(),
      );
    }
    await _cacheMessages(message.channelRef);
  }

  Future<void> removeMessage(KaedeMessage message) async {
    final accountKey = api.tokens?.accountKey;
    final generation = _sessionLoadGeneration;
    if (accountKey == null || !_sessionIsCurrent(accountKey, generation)) {
      return;
    }
    await repository.deleteMessage(message.channelRef, message.ref);
    if (!_sessionIsCurrent(accountKey, generation)) return;
    final messages = state.messageStore[message.channelRef];
    if (messages != null) {
      _setChannelMessages(
        message.channelRef,
        messages.where((item) => item.ref != message.ref).toList(),
      );
    }
    await _cacheMessages(message.channelRef);
  }

  Future<void> loadAround(EntityRef message) async {
    final channel = state.activeChannel;
    if (channel == null) return;
    final accountKey = api.tokens?.accountKey;
    final sessionGeneration = _sessionLoadGeneration;
    if (accountKey == null ||
        !_sessionIsCurrent(accountKey, sessionGeneration)) {
      return;
    }
    final channelRef = channel.ref;
    final generation = (_messageRequestGenerations[channelRef] ?? 0) + 1;
    _messageRequestGenerations[channelRef] = generation;
    _setChannelLoading(channelRef, true);
    state = state.copyWith(clearError: true);
    try {
      final page =
          await repository.messages(channelRef, around: message, limit: 50);
      if (_messageRequestIsCurrent(
        channelRef,
        generation,
        accountKey,
        sessionGeneration,
      )) {
        _setChannelMessages(channelRef, page.reversed.toList());
        await _cacheMessages(channelRef);
        if (page.isNotEmpty && _isChannelVisible(channelRef)) {
          unawaited(_acknowledge(channelRef, page.first.ref));
        }
      }
    } on Object catch (error) {
      if (_messageRequestIsCurrent(
        channelRef,
        generation,
        accountKey,
        sessionGeneration,
      )) {
        state = state.copyWith(
          error: 'Could not open that message. ${_message(error)}',
        );
      }
    } finally {
      if (_messageRequestIsCurrent(
        channelRef,
        generation,
        accountKey,
        sessionGeneration,
      )) {
        _setChannelLoading(channelRef, false);
      }
    }
  }

  Future<void> publishTyping(EntityRef channel) async {
    if (state.phase != SessionPhase.ready) return;
    try {
      await repository.typing(channel);
    } on Object {
      // Typing is ephemeral and should never interrupt composing a message.
    }
  }

  Future<void> logout() async {
    _authenticationGeneration += 1;
    _sessionLoadGeneration += 1;
    _messageRequestGenerations.clear();
    final accountKey = api.tokens?.accountKey;
    final pushDeviceId = _pushDeviceId;
    if (pushDeviceId != null) {
      try {
        await repository.unregisterPushDevice(pushDeviceId);
      } on Object {
        // Explicit logout still clears the session when the server cannot be
        // reached. An unreachable push token expires or can be disabled by FCM.
      }
    }
    _pushDeviceId = null;
    _pushRegistrationWarning = null;
    _pushRemoteDeliveryWarning = null;
    _pushLocalDisplayWarning = null;
    await _pushTokenSubscription?.cancel();
    _pushTokenSubscription = null;
    await _pushDestinationSubscription?.cancel();
    _pushDestinationSubscription = null;
    await _pushHealthSubscription?.cancel();
    _pushHealthSubscription = null;
    await _sessionExpiredSubscription?.cancel();
    _sessionExpiredSubscription = null;
    _outboxTimer?.cancel();
    _outboxTimer = null;
    _appLockTimer?.cancel();
    _appLockTimer = null;
    _typingExpiryTimer?.cancel();
    _typingExpiryTimer = null;
    _navigationRefreshTimer?.cancel();
    _navigationRefreshTimer = null;
    _metadataCacheTimer?.cancel();
    _metadataCacheTimer = null;
    _acknowledgementRetryTimer?.cancel();
    _acknowledgementRetryTimer = null;
    _selfModerationExpiryTimer?.cancel();
    _selfModerationRetryTimer?.cancel();
    _selfModerationExpiryTimer = null;
    _pendingAcknowledgements.clear();
    _acknowledgementsInFlight.clear();
    await _gatewaySubscription?.cancel();
    await _gatewayHealthSubscription?.cancel();
    _gatewayHealthSubscription = null;
    await gateway.disconnect();
    await repository.logout();
    if (accountKey != null) {
      await _queueCacheWrite(() => database.purgeAccount(accountKey));
    }
    _activeAccountKey = null;
    state = const MobileState(phase: SessionPhase.signedOut);
  }

  Future<void> _cacheLists() async {
    final tokens = api.tokens;
    if (tokens == null) return;
    final generation = _sessionLoadGeneration;
    final accountKey = tokens.accountKey;
    final user = state.user;
    final guilds = List<KaedeGuild>.of(state.guilds);
    final dms = List<KaedeChannel>.of(state.dms);
    final relationships = List<Map<String, Object?>>.of(state.relationships);
    final presence = state.presencePreference.name;
    final notifications = Map<String, bool>.of(state.notificationSettings);
    final guildNotifications =
        Map<String, String>.of(state.guildNotificationLevels);
    final unreadCounts = <String, int>{
      for (final entry in state.unreadCounts.entries)
        entry.key.wire: entry.value,
    };
    final mentionCounts = <String, int>{
      for (final entry in state.mentionCounts.entries)
        entry.key.wire: entry.value,
    };
    await _queueCacheWrite(() async {
      if (!_cacheSessionIsCurrent(accountKey, generation)) return;
      await database.replaceSnapshotGroups(accountKey, {
        'identity': {
          if (user != null) user.ref.wire: user.toJson(),
        },
        'guilds': {
          for (final guild in guilds) guild.ref.wire: guild.toJson(),
        },
        'dms': {
          for (final dm in dms) dm.ref.wire: dm.toJson(),
        },
        'relationships': {
          for (var index = 0; index < relationships.length; index++)
            '$index': relationships[index],
        },
        'preferences': {
          'current': {
            'presence': presence,
            'notifications': notifications,
            'guild_notifications': guildNotifications,
            'unread_counts': unreadCounts,
            'mention_counts': mentionCounts,
          },
        },
      });
    });
  }

  void _scheduleMetadataCache() {
    if (state.phase != SessionPhase.ready || api.tokens == null) return;
    _metadataCacheTimer?.cancel();
    _metadataCacheTimer = Timer(const Duration(milliseconds: 300), () {
      _metadataCacheTimer = null;
      unawaited(_cacheLists());
    });
  }

  Future<bool> _loadCached(
    SessionTokens tokens,
    int authenticationGeneration,
  ) async {
    final generation = ++_sessionLoadGeneration;
    _messageRequestGenerations.clear();
    final identities = await database.snapshots(tokens.accountKey, 'identity');
    if (identities.isEmpty ||
        authenticationGeneration != _authenticationGeneration ||
        generation != _sessionLoadGeneration ||
        api.tokens?.accountKey != tokens.accountKey) {
      return false;
    }
    _activeAccountKey = tokens.accountKey;
    final guilds = (await database.snapshots(tokens.accountKey, 'guilds'))
        .map(KaedeGuild.fromJson)
        .toList();
    final dms = (await database.snapshots(tokens.accountKey, 'dms'))
        .map(KaedeChannel.fromJson)
        .toList();
    final relationships =
        await database.snapshots(tokens.accountKey, 'relationships');
    final preferences =
        await database.snapshots(tokens.accountKey, 'preferences');
    final preference = preferences.firstOrNull ?? const <String, Object?>{};
    final outbox = await database.outboxForAccount(tokens.accountKey);
    final drafts = _decodeDrafts(
      await database.snapshots(tokens.accountKey, 'drafts'),
    );
    if (authenticationGeneration != _authenticationGeneration ||
        generation != _sessionLoadGeneration ||
        api.tokens?.accountKey != tokens.accountKey) {
      return false;
    }
    final rawNotifications = preference['notifications'];
    final rawLevels = preference['guild_notifications'];
    final unreadCounts = _decodeRefCounts(preference['unread_counts']);
    final mentionCounts = _decodeRefCounts(preference['mention_counts']);
    final localPreferences = await SharedPreferences.getInstance();
    final initial = resolveInitialConversation(
      saved: localPreferences.getString(
        _lastConversationKey(tokens.accountKey),
      ),
      dms: dms,
      guilds: guilds,
    );
    state = MobileState(
      phase: SessionPhase.ready,
      user: KaedeUser.fromJson(identities.first),
      guilds: guilds,
      dms: dms,
      relationships: relationships,
      selectedGuild: initial?.guildRef,
      selectedChannel: initial?.ref,
      presencePreference: PresenceStatus.values.firstWhere(
        (item) => item.name == '${preference['presence']}',
        orElse: () => PresenceStatus.offline,
      ),
      notificationSettings: rawNotifications is Map
          ? rawNotifications
              .map((key, value) => MapEntry('$key', value == true))
          : const <String, bool>{},
      guildNotificationLevels: rawLevels is Map
          ? rawLevels.map((key, value) => MapEntry('$key', '$value'))
          : const <String, String>{},
      unreadCounts: Map.unmodifiable(unreadCounts),
      mentionCounts: Map.unmodifiable(mentionCounts),
      outbox: outbox,
      drafts: Map.unmodifiable(drafts),
      offline: true,
      error: 'Offline — showing saved conversations.',
    );
    if (initial != null) {
      unawaited(_restoreCachedMessages(
        initial.ref,
        tokens.accountKey,
        generation,
      ));
    }
    return true;
  }

  Map<EntityRef, String> _decodeDrafts(
    List<Map<String, Object?>> records,
  ) {
    final drafts = <EntityRef, String>{};
    for (final record in records) {
      final rawRef = record['channel_ref'];
      final rawText = record['text'];
      if (rawRef is! String || rawText is! String || rawText.isEmpty) continue;
      try {
        drafts[EntityRef.parse(rawRef)] = rawText;
      } on Object {
        // A corrupt draft must not prevent an otherwise valid cached session.
      }
    }
    return drafts;
  }

  Map<EntityRef, int> _decodeRefCounts(Object? raw) {
    if (raw is! Map) return <EntityRef, int>{};
    final counts = <EntityRef, int>{};
    for (final entry in raw.entries) {
      try {
        final count = _asInt(entry.value);
        if (count > 0) counts[EntityRef.parse('${entry.key}')] = count;
      } on Object {
        // Ignore corrupt offline metadata without discarding the session.
      }
    }
    return counts;
  }

  Future<void> _cacheMessages(EntityRef channel) async {
    final tokens = api.tokens;
    if (tokens == null) return;
    final generation = _sessionLoadGeneration;
    final accountKey = tokens.accountKey;
    final kind = 'messages:${channel.wire}';
    final messages = state.messageStore[channel] ?? const <KaedeMessage>[];
    final retained = messages.length <= 250
        ? messages
        : messages.sublist(messages.length - 250);
    final values = <String, Object?>{
      for (final message in retained) message.ref.wire: message.toJson(),
    };
    await _queueCacheWrite(() async {
      if (!_cacheSessionIsCurrent(accountKey, generation)) return;
      await database.replaceSnapshots(accountKey, kind, values);
    });
  }

  Future<void> _cacheMessage(KaedeMessage message) async {
    final tokens = api.tokens;
    if (tokens == null) return;
    final generation = _sessionLoadGeneration;
    final accountKey = tokens.accountKey;
    final payload = message.toJson();
    await _queueCacheWrite(() async {
      if (!_cacheSessionIsCurrent(accountKey, generation)) return;
      await database.putSnapshot(
        accountKey,
        'messages:${message.channelRef.wire}',
        message.ref.wire,
        payload,
      );
    });
    final messages = state.messageStore[message.channelRef];
    if (messages != null && messages.length > 250) {
      await _cacheMessages(message.channelRef);
    }
  }

  Future<bool> _restoreCachedMessages(
    EntityRef channel,
    String accountKey,
    int generation,
  ) async {
    if (!_sessionIsCurrent(accountKey, generation)) return false;
    final cached = await database.snapshots(
      accountKey,
      'messages:${channel.wire}',
    );
    if (cached.isEmpty || !_sessionIsCurrent(accountKey, generation)) {
      return false;
    }
    _setChannelMessages(channel, cached.map(KaedeMessage.fromJson).toList());
    return true;
  }

  void _reduceGateway(GatewayEvent event) {
    try {
      _applyGateway(event);
      _malformedGatewayEvents = 0;
      if (state.gatewayProtocolWarning != null) {
        _validGatewayEventsAfterWarning += 1;
        if (_validGatewayEventsAfterWarning >= 5) {
          _validGatewayEventsAfterWarning = 0;
          state = state.copyWith(clearGatewayProtocolWarning: true);
        }
      }
    } on Object {
      // A newer or malformed event must not terminate the gateway stream.
      _validGatewayEventsAfterWarning = 0;
      _malformedGatewayEvents += 1;
      if (_malformedGatewayEvents >= 3) {
        state = state.copyWith(
          gatewayProtocolWarning:
              'Kaede received repeated invalid realtime updates. Some live information may be incomplete while it refreshes safely.',
        );
      }
      _scheduleNavigationRefresh();
    }
  }

  void _applyGateway(GatewayEvent event) {
    switch (event.name) {
      case 'READY':
        state = state.copyWith(clearGatewayProtocolWarning: true);
        _malformedGatewayEvents = 0;
        _validGatewayEventsAfterWarning = 0;
        final counts = Map<EntityRef, int>.of(state.mentionCounts);
        final unread = Map<EntityRef, int>.of(state.unreadCounts);
        final rawStates = event.data['read_states'];
        if (rawStates is List) {
          for (final raw in rawStates.whereType<Map<Object?, Object?>>()) {
            final data = Map<String, Object?>.from(raw);
            final channel = _channelRef(data);
            if (channel != null) {
              final mentionCount = _asInt(data['mention_count']);
              final hasUnread = data.containsKey('unread_count') ||
                  data.containsKey('unread');
              final unreadCount = hasUnread
                  ? _asInt(data['unread_count'] ?? data['unread'])
                  : 0;
              if (mentionCount > 0) {
                counts[channel] = mentionCount;
              } else {
                counts.remove(channel);
              }
              // Older gateways omit unread entirely from READY. Preserve the
              // authoritative REST snapshot instead of interpreting absence
              // as an explicit zero.
              if (hasUnread) {
                if (unreadCount > 0) {
                  unread[channel] = unreadCount;
                } else {
                  unread.remove(channel);
                }
              }
            }
          }
          if (state.selectedChannel case final selected?
              when _isChannelVisible(selected)) {
            counts.remove(selected);
            unread.remove(selected);
          }
          state = state.copyWith(
            mentionCounts: Map.unmodifiable(counts),
            unreadCounts: Map.unmodifiable(unread),
          );
          _scheduleMetadataCache();
        }
        break;
      case 'MESSAGE_CREATE':
        final raw =
            event.data['message'] is Map ? event.data['message'] : event.data;
        final message =
            KaedeMessage.fromJson(Map<String, Object?>.from(raw as Map));
        final existing = state.messageStore[message.channelRef];
        if (existing != null || message.channelRef == state.selectedChannel) {
          _setChannelMessages(
            message.channelRef,
            mergeMessages(<KaedeMessage>[...?existing, message]),
          );
          unawaited(_cacheMessage(message));
        }
        _removeTyping(message.channelRef, message.authorRef);
        if (message.clientNonce case final nonce?) {
          unawaited(
            database.completeOutbox(nonce).then((_) => _syncOutbox()),
          );
        }
        if (_isChannelVisible(message.channelRef)) {
          unawaited(_acknowledge(message.channelRef, message.ref));
        } else if (message.authorRef != state.user?.ref) {
          _incrementUnread(
            message.channelRef,
            mentioned: state.user != null &&
                message.mentionUserRefs.contains(state.user!.ref),
          );
        }
        unawaited(_notifyFor(message));
        break;
      case 'MESSAGE_UPDATE':
        final raw =
            event.data['message'] is Map ? event.data['message'] : event.data;
        _applyMessageUpdate(Map<String, Object?>.from(raw as Map));
        break;
      case 'MESSAGE_DELETE':
        final target = _messageRef(event.data);
        if (target != null) _tombstoneMessage(target);
        break;
      case 'ATTACHMENT_UPDATE':
        _applyAttachmentUpdate(event.data);
        break;
      case 'MESSAGE_SEND_REJECTED':
        final nonce = '${event.data['client_nonce'] ?? ''}';
        if (nonce.isNotEmpty) {
          final reason = userFacingGatewayError(
            event.data,
            fallback: 'The message could not be sent.',
          );
          unawaited(
            database.failOutbox(nonce, reason).then((_) => _syncOutbox()),
          );
        }
        break;
      case 'MESSAGE_DELIVERY_UPDATE':
        final target = _messageRef(event.data);
        if (target != null) {
          final status = '${event.data['status'] ?? 'pending'}';
          _patchStoredMessage(
            target,
            (message) => message.copyWith(
              deliveryStatus: status,
              failureReason: status == 'failed'
                  ? userFacingGatewayError(
                      event.data,
                      fallback: 'The message was not delivered.',
                    )
                  : status == 'retrying'
                      ? 'The receiving instance is at capacity. Kaede is retrying automatically.'
                      : null,
              clearFailureReason: status != 'failed' && status != 'retrying',
            ),
          );
        }
        break;
      case 'DM_OPEN_REJECTED':
        state = state.copyWith(
          error: userFacingGatewayError(
            event.data,
            fallback: 'This direct message could not be opened.',
          ),
        );
        _scheduleNavigationRefresh();
        break;
      case 'READ_STATE_UPDATE':
        final channel = _channelRef(event.data);
        if (channel != null) {
          final mentions = Map<EntityRef, int>.of(state.mentionCounts);
          final mentionCount = _asInt(event.data['mention_count']);
          if (mentionCount > 0) {
            mentions[channel] = mentionCount;
          } else {
            mentions.remove(channel);
          }
          final unread = Map<EntityRef, int>.of(state.unreadCounts);
          final hasUnread = event.data.containsKey('unread_count') ||
              event.data.containsKey('unread');
          if (hasUnread) {
            final unreadCount =
                _asInt(event.data['unread_count'] ?? event.data['unread']);
            if (unreadCount > 0) {
              unread[channel] = unreadCount;
            } else {
              unread.remove(channel);
            }
          } else if (mentionCount > 0) {
            // Mention fanout on older servers carries only mention_count, but
            // a new mention is necessarily unread until acknowledged.
            unread[channel] = unread[channel] ?? 1;
          } else if (event.data['last_message_id'] != null) {
            // An acknowledgement event from another client carries the new
            // read cursor. With no explicit unread field it is safe to clear
            // the local indicator; newer servers send the exact boolean.
            unread.remove(channel);
          }
          if (_isChannelVisible(channel)) {
            mentions.remove(channel);
            unread.remove(channel);
          }
          state = state.copyWith(
            mentionCounts: Map.unmodifiable(mentions),
            unreadCounts: Map.unmodifiable(unread),
          );
          _scheduleMetadataCache();
        }
        break;
      case 'TYPING_START':
        final channel = _channelRef(event.data);
        final user = _userRef(event.data);
        if (channel != null && user != null && user != state.user?.ref) {
          _setTyping(channel, user);
        }
        break;
      case 'GUILD_CREATE' ||
            'GUILD_UPDATE' ||
            'GUILD_DELETE' ||
            'CHANNEL_CREATE' ||
            'CHANNEL_UPDATE' ||
            'CHANNEL_DELETE':
        _scheduleNavigationRefresh();
        break;
      case 'GUILD_NAVIGATION_UPDATE':
        state = state.copyWith(
          guildNavigation: reconcileGuildNavigation(
            GuildNavigation.fromJson(event.data),
            state.guilds,
          ),
        );
        _setDegradedWarning(DegradedFeature.guildNavigation, null);
        break;
      case 'GUILD_HISTORY_SYNC_UPDATE':
        try {
          final guildRef = EntityRef(
            Snowflake('${event.data['guild_id']}'),
            Domain('${event.data['guild_domain']}'),
          );
          final status = '${event.data['status'] ?? ''}';
          if (const {'syncing', 'retrying', 'ready', 'failed'}
              .contains(status)) {
            final guilds = state.guilds
                .map(
                  (guild) => guild.ref != guildRef
                      ? guild
                      : KaedeGuild.fromJson(<String, Object?>{
                          ...guild.toJson(),
                          'history_sync_status': status,
                          'history_sync_error_code':
                              status == 'ready' ? null : event.data['code'],
                          'history_sync_retry_after_ms': status == 'retrying'
                              ? event.data['retry_after_ms']
                              : null,
                          'history_sync_resource': status == 'failed'
                              ? event.data['resource']
                              : null,
                        }),
                )
                .toList(growable: false);
            state = state.copyWith(guilds: List.unmodifiable(guilds));
            _scheduleMetadataCache();
          }
        } on FormatException {
          // Ignore a malformed projection without disrupting other events.
        }
        break;
      case 'CHANNEL_ACCESS_REVOKED':
        unawaited(_revokeChannelAccess(event.data));
        break;
      case 'GUILD_MEMBER_UPDATE':
        _scheduleNavigationRefresh();
        final nestedUser = event.data['user'];
        final user = nestedUser is Map
            ? _entityRef(nestedUser['id'], nestedUser['origin_domain'])
            : _userRef(event.data);
        if (user == state.user?.ref) {
          final guild = state.selectedGuild;
          if (guild != null) unawaited(refreshSelfModeration(guild));
        }
        break;
      case 'CHANNEL_ACCESS_GRANTED' ||
            'CHANNEL_PERMISSION_UPDATE' ||
            'GUILD_ROLE_CREATE' ||
            'GUILD_ROLE_UPDATE' ||
            'GUILD_ROLE_DELETE' ||
            'GUILD_MEMBER_ADD' ||
            'GUILD_MEMBER_REMOVE' ||
            'GUILD_MEMBERS_CHUNK' ||
            'GUILD_MEMBER_LIST_UPDATE' ||
            'GUILD_AVAILABILITY_UPDATE' ||
            'GUILD_EMOJI_CREATE' ||
            'GUILD_EMOJI_DELETE' ||
            'VOICE_STATE_UPDATE' ||
            'VOICE_CHANNEL_MOVE' ||
            'CALL_CREATE' ||
            'CALL_RING' ||
            'CALL_ACCEPT' ||
            'CALL_DECLINE' ||
            'CALL_END' ||
            'RESUMED' ||
            'INVALID_SESSION':
        _scheduleNavigationRefresh();
        break;
      case 'USER_UPDATE':
        final relationship = event.data['relationship'];
        if (relationship is Map) {
          final detail = Map<String, Object?>.from(relationship);
          final errorCode = detail['error_code'];
          if (errorCode is String && errorCode.isNotEmpty) {
            state = state.copyWith(
              error: userFacingGatewayError(
                <String, Object?>{'code': errorCode},
                fallback: 'The friend request could not be delivered.',
              ),
            );
          }
        } else if (event.data['id'] != null &&
            event.data['origin_domain'] != null &&
            event.data['username'] != null) {
          _applyUserProfileUpdate(KaedeUser.fromJson(event.data));
        }
        _scheduleNavigationRefresh();
        break;
      case 'VOICE_TOKEN':
        final correlation = event.data['move_session_id'];
        final grant = event.data['grant'];
        final grantCorrelation =
            grant is Map<String, Object?> ? grant['move_session_id'] : null;
        final localMove = grant is Map<String, Object?> &&
            correlation == null &&
            grantCorrelation == null;
        final correlatedMove = correlation is String &&
            RegExp(r'^[A-Za-z0-9_-]{32,64}$').hasMatch(correlation) &&
            grantCorrelation == correlation;
        if (localMove || correlatedMove) {
          _scheduleNavigationRefresh();
        }
        break;
      case 'PRESENCE_UPDATE':
        final user = _userRef(event.data);
        if (user != null) {
          final visiblePresence = _presence(event.data['status']);
          final accountPreference = _presence(
            event.data['preference'] ?? event.data['status'],
          );
          final presenceByUser = Map<EntityRef, PresenceStatus>.of(
            state.presenceByUser,
          )..[user] = visiblePresence;
          state = state.copyWith(
            presenceByUser: Map.unmodifiable(presenceByUser),
            presencePreference: user == state.user?.ref
                ? accountPreference
                : state.presencePreference,
          );
          if (user == state.user?.ref) _scheduleMetadataCache();
        }
        break;
      case 'FEDERATION_PEER_STATUS':
        // Peer health is operator-facing. User-visible remote availability is
        // delivered through guild/channel availability events instead.
        break;
      default:
        // Preserve forward compatibility without refreshing for every frame:
        // the existing debounce coalesces an unknown mutation burst into one
        // bounded REST reconciliation.
        _scheduleNavigationRefresh();
    }
  }

  void _applyMessageUpdate(Map<String, Object?> data) {
    final target = _messageRef(data);
    if (target == null) return;
    if (data['author_id'] != null && data['created_at'] != null) {
      final message = KaedeMessage.fromJson(data);
      _patchStoredMessage(target, (_) => message);
      return;
    }
    _patchStoredMessage(target, (message) {
      var next = message;
      if (data.containsKey('pinned')) {
        next = next.copyWith(pinned: data['pinned'] == true);
      }
      if (data['reaction'] case final reaction?) {
        final counts = Map<String, int>.of(next.reactionCounts);
        final reacted = Set<String>.of(next.reactedEmoji);
        final key = '$reaction';
        final removed = data['removed'] == true;
        final count = (counts[key] ?? 0) + (removed ? -1 : 1);
        if (count <= 0) {
          counts.remove(key);
        } else {
          counts[key] = count;
        }
        final currentUser = state.user;
        if (currentUser != null &&
            '${data['user_id']}' == currentUser.ref.id.value &&
            '${data['user_domain']}' == currentUser.ref.domain.value) {
          if (removed) {
            reacted.remove(key);
          } else {
            reacted.add(key);
          }
        }
        next = next.copyWith(
          reactionCounts: Map.unmodifiable(counts),
          reactedEmoji: Set.unmodifiable(reacted),
        );
      }
      if (data.containsKey('delivery_status')) {
        final status = '${data['delivery_status']}';
        next = next.copyWith(
          deliveryStatus: status,
          failureReason: status == 'failed'
              ? userFacingGatewayError(
                  data,
                  fallback: 'The message was not delivered.',
                )
              : null,
          clearFailureReason: status != 'failed',
        );
      }
      return next;
    });
  }

  void _applyAttachmentUpdate(Map<String, Object?> data) {
    final target = _messageRef(data);
    final raw = data['attachment'];
    if (target == null || raw is! Map) return;
    final attachment = KaedeAttachment.fromJson(Map<String, Object?>.from(raw));
    _patchStoredMessage(target, (message) {
      final items = <KaedeAttachment>[];
      var replaced = false;
      for (final item in message.attachments) {
        if (item.ref == attachment.ref) {
          items.add(attachment);
          replaced = true;
        } else {
          items.add(item);
        }
      }
      if (!replaced) items.add(attachment);
      return message.copyWith(attachments: List.unmodifiable(items));
    });
  }

  void _patchStoredMessage(
    EntityRef target,
    KaedeMessage Function(KaedeMessage message) patch,
  ) {
    for (final entry in state.messageStore.entries.toList()) {
      final index = entry.value.indexWhere((item) => item.ref == target);
      if (index < 0) continue;
      final messages = List<KaedeMessage>.of(entry.value);
      messages[index] = patch(messages[index]);
      _setChannelMessages(entry.key, messages);
      unawaited(_cacheMessage(messages[index]));
      return;
    }
  }

  void _applyUserProfileUpdate(KaedeUser user) {
    final store = <EntityRef, List<KaedeMessage>>{};
    final changedChannels = <EntityRef>[];
    for (final entry in state.messageStore.entries) {
      var changed = false;
      final messages = entry.value.map((message) {
        if (message.authorRef != user.ref) return message;
        changed = true;
        return message.copyWith(author: user);
      }).toList(growable: false);
      store[entry.key] =
          changed ? List<KaedeMessage>.unmodifiable(messages) : entry.value;
      if (changed) changedChannels.add(entry.key);
    }
    final dms = state.dms
        .map(
          (channel) => KaedeChannel.fromJson(<String, Object?>{
            ...channel.toJson(),
            'recipients': channel.recipients
                .map((recipient) => recipient.ref == user.ref
                    ? user.toJson()
                    : recipient.toJson())
                .toList(growable: false),
          }),
        )
        .toList(growable: false);
    final relationships = state.relationships.map((relationship) {
      final raw = Map<String, Object?>.from(relationship);
      final related = raw['user'];
      if (related is Map) {
        try {
          if (KaedeUser.fromJson(Map<String, Object?>.from(related)).ref ==
              user.ref) {
            raw['user'] = user.toJson();
          }
        } on Object {
          // Ignore a malformed unrelated relationship projection.
        }
      }
      return Map<String, Object?>.unmodifiable(raw);
    }).toList(growable: false);
    state = state.copyWith(
      dms: List<KaedeChannel>.unmodifiable(dms),
      relationships: List<Map<String, Object?>>.unmodifiable(relationships),
      messageStore: Map<EntityRef, List<KaedeMessage>>.unmodifiable(store),
      userProfiles: Map<EntityRef, KaedeUser>.unmodifiable(
        <EntityRef, KaedeUser>{...state.userProfiles, user.ref: user},
      ),
    );
    for (final channel in changedChannels) {
      unawaited(_cacheMessages(channel));
    }
    _scheduleMetadataCache();
  }

  void _tombstoneMessage(EntityRef target) {
    _patchStoredMessage(
      target,
      (message) => message.copyWith(
        clearContent: true,
        attachments: const <KaedeAttachment>[],
        mentionUserRefs: const <EntityRef>[],
        reactionCounts: const <String, int>{},
        deletedAt: DateTime.now().toUtc(),
      ),
    );
  }

  EntityRef? _messageRef(Map<String, Object?> data) {
    final id = data['message_id'] ?? data['id'];
    final domain = data['message_domain'] ?? data['origin_domain'];
    return _entityRef(id, domain);
  }

  EntityRef? _channelRef(Map<String, Object?> data) =>
      _entityRef(data['channel_id'], data['channel_domain']);

  EntityRef? _userRef(Map<String, Object?> data) => _entityRef(
        data['user_id'] ?? data['id'],
        data['user_domain'] ?? data['origin_domain'],
      );

  EntityRef? _entityRef(Object? id, Object? domain) {
    if (id == null || domain == null || '$id'.isEmpty || '$domain'.isEmpty) {
      return null;
    }
    try {
      return EntityRef(Snowflake('$id'), Domain('$domain'));
    } on Object {
      return null;
    }
  }

  int _asInt(Object? value) {
    if (value is bool) return value ? 1 : 0;
    if (value is int) return value;
    return int.tryParse('$value') ?? 0;
  }

  PresenceStatus _presence(Object? value) {
    final raw = '$value'.toLowerCase();
    return PresenceStatus.values.firstWhere(
      (item) => item.name == raw,
      orElse: () => PresenceStatus.offline,
    );
  }

  void _incrementUnread(EntityRef channel, {required bool mentioned}) {
    final unread = Map<EntityRef, int>.of(state.unreadCounts)
      ..[channel] = (state.unreadCounts[channel] ?? 0) + 1;
    final mentions = Map<EntityRef, int>.of(state.mentionCounts);
    if (mentioned) {
      mentions[channel] = (mentions[channel] ?? 0) + 1;
    }
    state = state.copyWith(
      unreadCounts: Map.unmodifiable(unread),
      mentionCounts: Map.unmodifiable(mentions),
    );
    _scheduleMetadataCache();
  }

  void _clearUnread(EntityRef channel) {
    if ((state.unreadCounts[channel] ?? 0) == 0 &&
        (state.mentionCounts[channel] ?? 0) == 0) {
      return;
    }
    final unread = Map<EntityRef, int>.of(state.unreadCounts)..remove(channel);
    final mentions = Map<EntityRef, int>.of(state.mentionCounts)
      ..remove(channel);
    state = state.copyWith(
      unreadCounts: Map.unmodifiable(unread),
      mentionCounts: Map.unmodifiable(mentions),
    );
    _scheduleMetadataCache();
  }

  Future<void> _acknowledge(EntityRef channel, EntityRef message) async {
    final existing = _pendingAcknowledgements[channel];
    final currentUnread = state.unreadCounts[channel] ?? 0;
    final currentMentions = state.mentionCounts[channel] ?? 0;
    _pendingAcknowledgements[channel] = existing == null
        ? _PendingAcknowledgement(
            message: message,
            unread: currentUnread,
            mentions: currentMentions,
          )
        : existing.copyWith(
            message: message,
            unread: existing.restored
                ? max(existing.unread, currentUnread)
                : existing.unread + currentUnread,
            mentions: existing.restored
                ? max(existing.mentions, currentMentions)
                : existing.mentions + currentMentions,
            restored: false,
          );
    _clearUnread(channel);
    await _drainAcknowledgement(channel);
  }

  Future<void> _drainAcknowledgement(EntityRef channel) async {
    if (!_acknowledgementsInFlight.add(channel)) return;
    try {
      while (true) {
        final pending = _pendingAcknowledgements[channel];
        if (pending == null) break;
        try {
          await repository.acknowledge(channel, pending.message);
          if (identical(_pendingAcknowledgements[channel], pending)) {
            if (pending.restored) {
              await _reconcileAcknowledgedChannel(channel, pending);
            }
            if (identical(_pendingAcknowledgements[channel], pending)) {
              _pendingAcknowledgements.remove(channel);
            }
          }
          if (_pendingAcknowledgements.isEmpty) {
            _setDegradedWarning(DegradedFeature.acknowledgements, null);
          }
        } on Object catch (error) {
          if (state.phase != SessionPhase.ready || api.tokens == null) return;
          final latest = _pendingAcknowledgements[channel] ?? pending;
          final retry = latest.copyWith(
            attempt: latest.attempt + 1,
            restored: true,
          );
          _pendingAcknowledgements[channel] = retry;
          _restoreUnreadAfterFailedAcknowledgement(channel, retry);
          _setDegradedWarning(
            DegradedFeature.acknowledgements,
            userFacingError(
              error,
              summary:
                  'Messages could not be marked as read. The unread marker was restored and Kaede will retry.',
            ),
          );
          _scheduleAcknowledgementRetry();
          break;
        }
      }
    } finally {
      _acknowledgementsInFlight.remove(channel);
    }
  }

  void _restoreUnreadAfterFailedAcknowledgement(
    EntityRef channel,
    _PendingAcknowledgement pending,
  ) {
    final unread = Map<EntityRef, int>.of(state.unreadCounts);
    final mentions = Map<EntityRef, int>.of(state.mentionCounts);
    if (pending.unread > 0) {
      unread[channel] = max(unread[channel] ?? 0, pending.unread);
    }
    if (pending.mentions > 0) {
      mentions[channel] = max(mentions[channel] ?? 0, pending.mentions);
    }
    state = state.copyWith(
      unreadCounts: Map.unmodifiable(unread),
      mentionCounts: Map.unmodifiable(mentions),
    );
    _scheduleMetadataCache();
  }

  Future<void> _reconcileAcknowledgedChannel(
    EntityRef channel,
    _PendingAcknowledgement pending,
  ) async {
    final accountKey = api.tokens?.accountKey;
    final generation = _sessionLoadGeneration;
    if (accountKey == null || !_sessionIsCurrent(accountKey, generation)) {
      return;
    }
    try {
      final badges = decodeReadBadgeSnapshot(await repository.readStates());
      if (!_sessionIsCurrent(accountKey, generation) ||
          !identical(_pendingAcknowledgements[channel], pending)) {
        return;
      }
      final reconciled = reconcileAuthoritativeChannelBadge(
        currentUnread: state.unreadCounts,
        currentMentions: state.mentionCounts,
        authoritative: badges,
        channel: channel,
      );
      state = state.copyWith(
        unreadCounts: reconciled.unread,
        mentionCounts: reconciled.mentions,
      );
      _setDegradedWarning(DegradedFeature.readStates, null);
      _scheduleMetadataCache();
    } on Object catch (error) {
      if (!_sessionIsCurrent(accountKey, generation) ||
          !identical(_pendingAcknowledgements[channel], pending)) {
        return;
      }
      _setDegradedWarning(
        DegradedFeature.readStates,
        userFacingError(
          error,
          summary:
              'The message was marked as read, but its unread marker could not be refreshed. Use Retry to sync the marker.',
        ),
      );
    }
  }

  void _scheduleAcknowledgementRetry() {
    if (_acknowledgementRetryTimer?.isActive == true ||
        _pendingAcknowledgements.isEmpty) {
      return;
    }
    final attempt = _pendingAcknowledgements.values.fold<int>(
      1,
      (current, pending) => max(current, pending.attempt),
    );
    final seconds = min(30, 1 << min(attempt, 4));
    _acknowledgementRetryTimer = Timer(
      Duration(seconds: seconds),
      () => unawaited(_retryPendingAcknowledgements()),
    );
  }

  Future<void> _retryPendingAcknowledgements() async {
    _acknowledgementRetryTimer?.cancel();
    _acknowledgementRetryTimer = null;
    if (state.phase != SessionPhase.ready) return;
    await Future.wait<void>(
      _pendingAcknowledgements.keys
          .toList(growable: false)
          .map(_drainAcknowledgement),
    );
  }

  void _setTyping(EntityRef channel, EntityRef user) {
    final expiresAt = DateTime.now().add(const Duration(seconds: 10));
    final typing = Map<EntityRef, List<TypingParticipant>>.of(
      state.typingByChannel,
    );
    final current = List<TypingParticipant>.of(
      typing[channel] ?? const <TypingParticipant>[],
    )..removeWhere((participant) => participant.user == user);
    current.add(TypingParticipant(
      user: user,
      name: _displayName(user),
      expiresAt: expiresAt,
    ));
    typing[channel] = List.unmodifiable(current);
    state = state.copyWith(typingByChannel: Map.unmodifiable(typing));
    _scheduleTypingExpiry();
  }

  void _removeTyping(EntityRef channel, EntityRef user) {
    final existing = state.typingByChannel[channel];
    if (existing == null ||
        !existing.any((participant) => participant.user == user)) {
      return;
    }
    final typing = Map<EntityRef, List<TypingParticipant>>.of(
      state.typingByChannel,
    );
    final remaining = existing
        .where((participant) => participant.user != user)
        .toList(growable: false);
    if (remaining.isEmpty) {
      typing.remove(channel);
    } else {
      typing[channel] = remaining;
    }
    state = state.copyWith(typingByChannel: Map.unmodifiable(typing));
    _scheduleTypingExpiry();
  }

  void _scheduleTypingExpiry() {
    _typingExpiryTimer?.cancel();
    DateTime? next;
    for (final participants in state.typingByChannel.values) {
      for (final participant in participants) {
        if (next == null || participant.expiresAt.isBefore(next)) {
          next = participant.expiresAt;
        }
      }
    }
    if (next == null) return;
    final delay = next.difference(DateTime.now());
    _typingExpiryTimer = Timer(
      delay.isNegative ? Duration.zero : delay,
      _expireTyping,
    );
  }

  void _expireTyping() {
    final now = DateTime.now();
    final typing = <EntityRef, List<TypingParticipant>>{};
    for (final entry in state.typingByChannel.entries) {
      final active = entry.value
          .where((participant) => participant.expiresAt.isAfter(now))
          .toList(growable: false);
      if (active.isNotEmpty) typing[entry.key] = active;
    }
    state = state.copyWith(typingByChannel: Map.unmodifiable(typing));
    _scheduleTypingExpiry();
  }

  String _displayName(EntityRef user) {
    if (state.user?.ref == user) return state.user!.name;
    for (final dm in state.dms) {
      for (final recipient in dm.recipients) {
        if (recipient.ref == user) return recipient.name;
      }
    }
    for (final messages in state.messageStore.values) {
      for (final message in messages) {
        if (message.authorRef == user && message.author != null) {
          return message.author!.name;
        }
      }
    }
    return user.wire;
  }

  Future<void> _revokeChannelAccess(Map<String, Object?> data) async {
    final tokens = api.tokens;
    final id = '${data['channel_id'] ?? ''}';
    final domain = '${data['channel_domain'] ?? ''}';
    if (tokens == null || id.isEmpty || domain.isEmpty) return;
    final ref = EntityRef(Snowflake(id), Domain(domain));
    final generation = _sessionLoadGeneration;
    final accountKey = tokens.accountKey;
    final revokedAttachments = <EntityRef>{
      for (final message in state.messageStore[ref] ?? const <KaedeMessage>[])
        for (final attachment in message.attachments) attachment.ref,
    };
    await _queueCacheWrite(() async {
      if (!_cacheSessionIsCurrent(accountKey, generation)) return;
      await database.clearSnapshots(accountKey, 'messages:${ref.wire}');
      await database.removeSnapshot(accountKey, 'drafts', ref.wire);
    });
    await _purgeDownloadedAttachments(revokedAttachments);
    _messageRequestGenerations[ref] =
        (_messageRequestGenerations[ref] ?? 0) + 1;
    final store = Map<EntityRef, List<KaedeMessage>>.of(state.messageStore)
      ..remove(ref);
    final unread = Map<EntityRef, int>.of(state.unreadCounts)..remove(ref);
    final mentions = Map<EntityRef, int>.of(state.mentionCounts)..remove(ref);
    final typing = Map<EntityRef, List<TypingParticipant>>.of(
      state.typingByChannel,
    )..remove(ref);
    final drafts = Map<EntityRef, String>.of(state.drafts)..remove(ref);
    if (state.selectedChannel == ref) {
      state = state.copyWith(
        clearChannel: true,
        messageStore: store,
        unreadCounts: unread,
        mentionCounts: mentions,
        typingByChannel: typing,
        drafts: drafts,
        error: 'Access to this channel was revoked.',
      );
    } else {
      state = state.copyWith(
        messageStore: store,
        unreadCounts: unread,
        mentionCounts: mentions,
        typingByChannel: typing,
        drafts: drafts,
      );
    }
    _scheduleNavigationRefresh();
    _scheduleMetadataCache();
  }

  Future<void> _notifyFor(KaedeMessage message) async {
    final self = state.user;
    if (self == null) return;

    final isDm = state.dms.any((channel) => channel.ref == message.channelRef);
    final mentioned = message.mentionUserRefs.contains(self.ref);
    KaedeGuild? guild;
    if (!isDm) {
      for (final candidate in state.guilds) {
        if (candidate.channels
            .any((channel) => channel.ref == message.channelRef)) {
          guild = candidate;
          break;
        }
      }
    }
    final decision = decideLocalMessageNotification(
      authoredByCurrentUser: message.authorRef == self.ref,
      doNotDisturb: state.presencePreference == PresenceStatus.dnd,
      conversationIsVisible: _isChannelVisible(message.channelRef),
      isDirectMessage: isDm,
      mentionsCurrentUser: mentioned,
      directMessagesEnabled:
          state.notificationSettings['direct_messages'] ?? true,
      mentionsEnabled: state.notificationSettings['mentions'] ?? true,
      guildNotificationLevel: guild == null
          ? 'none'
          : state.guildNotificationLevels[guild.ref.wire] ?? 'mentions',
    );
    final kind = switch (decision) {
      LocalMessageNotificationDecision.none => null,
      LocalMessageNotificationDecision.directMessage =>
        NotificationKind.directMessage,
      LocalMessageNotificationDecision.mention => NotificationKind.mention,
      LocalMessageNotificationDecision.guildMessage =>
        NotificationKind.guildMessage,
    };
    if (kind == null) return;
    final showPreview = notificationPreviewsEnabled(state.notificationSettings);
    final privateBody = switch (kind) {
      NotificationKind.directMessage => 'New direct message',
      NotificationKind.mention => 'You were mentioned',
      NotificationKind.guildMessage => 'New guild message',
      _ => 'Open Kaede to view this update.',
    };
    try {
      await push.show(
        id: stableNotificationId(message.ref.wire),
        kind: kind,
        title: showPreview
            ? message.author?.name ?? message.authorRef.wire
            : 'Kaede Chat',
        body: showPreview
            ? (message.content?.trim().isNotEmpty == true
                ? message.content!.trim()
                : 'Sent an attachment')
            : privateBody,
        payload: PushDestination(
          channel: message.channelRef,
          message: message.ref,
        ).encode(),
        senderName:
            showPreview ? message.author?.name ?? message.authorRef.wire : null,
        senderRef: showPreview ? message.authorRef : null,
        senderAvatarUri: showPreview
            ? publicAssetUri(
                message.authorRef.domain,
                message.author?.avatarHash,
                variant: 'thumbnail_128',
              )
            : null,
        sentAt: message.createdAt,
      );
      _setPushLocalDisplayWarning(null);
    } on Object catch (error) {
      _setPushLocalDisplayWarning(userFacingError(
        error,
        summary:
            'A notification could not be shown. Messages are still available in Kaede.',
      ));
    }
  }

  Future<bool> _registerPushDevice({
    String? token,
    bool surfaceErrors = false,
    bool requireStoredOptIn = true,
  }) async {
    final accountKey = api.tokens?.accountKey;
    final generation = _sessionLoadGeneration;
    if (accountKey == null || !_sessionIsCurrent(accountKey, generation)) {
      return false;
    }
    if (requireStoredOptIn && !await api.pushOptedIn()) return false;
    try {
      final resolvedToken = token ?? await push.pushToken();
      if (resolvedToken == null ||
          resolvedToken.isEmpty ||
          !_sessionIsCurrent(accountKey, generation)) {
        if (_sessionIsCurrent(accountKey, generation)) {
          _setPushRegistrationWarning(push.remoteDeliveryAvailable
              ? 'Background notifications are enabled, but this device did not provide a push token. Check notification permissions and the device push service, then retry.'
              : 'This build is not configured for closed-app notifications. Foreground alerts still work.');
        }
        return false;
      }
      final installationId = await api.installationId();
      final platform = Platform.isIOS ? 'ios' : 'android';
      final deviceName = Platform.operatingSystemVersion.length <= 100
          ? Platform.operatingSystemVersion
          : Platform.operatingSystemVersion.substring(0, 100);
      late final Map<String, Object?> response;
      if (_pushTransport == 'direct_fcm') {
        response = await repository.registerPushDevice(
          installationId: installationId,
          token: resolvedToken,
          platform: platform,
          deviceName: deviceName,
        );
      } else {
        final previousRelayState = await api.relayPushState();
        final routeId = _randomPushToken();
        final wakeSecret = _randomPushToken();
        final managementSecret = _randomPushToken();
        final enrollment = await repository.beginRelayPushEnrollment(
          installationId: installationId,
          platform: platform,
          routeId: routeId,
          appId: _pushApplicationId,
        );
        final relayUrl = Uri.parse('${enrollment['relay_url'] ?? ''}');
        final relayOrigin = '${enrollment['relay_origin'] ?? ''}';
        final pinnedUrl = Uri.parse(_pinnedPushRelayUrl);
        if (relayUrl.scheme != 'https' ||
            relayUrl.host != pinnedUrl.host ||
            relayUrl.port != pinnedUrl.port ||
            relayUrl.path != pinnedUrl.path ||
            relayOrigin != _pinnedPushRelayOrigin) {
          throw const KaedeException(
            code: 'PUSH_RELAY_INVALID',
            message:
                'Your home offered a notification relay that this app does not trust.',
            status: 502,
          );
        }
        final grant = Map<String, Object?>.from(enrollment['grant']! as Map);
        final subscription = await repository.createRelayPushSubscription(
          relayUrl: relayUrl,
          grant: grant,
          providerToken: resolvedToken,
          managementSecret: managementSecret,
        );
        final receipt =
            Map<String, Object?>.from(subscription['receipt']! as Map);
        response = await repository.completeRelayPushEnrollment(
          installationId: installationId,
          platform: platform,
          routeId: routeId,
          wakeSecret: wakeSecret,
          receipt: receipt,
          deviceName: deviceName,
        );
        final home = api.tokens?.instance;
        if (home == null) return false;
        await api.saveRelayPushState(RelayPushState(
          home: home,
          relayUrl: relayUrl,
          relayOrigin: Domain(relayOrigin),
          subscriptionId: '${subscription['subscription_id']}',
          routeId: routeId,
          wakeSecret: wakeSecret,
          managementSecret: managementSecret,
        ));
        if (previousRelayState != null &&
            previousRelayState.subscriptionId !=
                '${subscription['subscription_id']}') {
          try {
            await repository.revokeRelayPushSubscription(previousRelayState);
          } on Object {
            // The home binding now points at the new route. The old relay
            // subscription expires even if this best-effort cleanup is lost.
          }
        }
      }
      if (_sessionIsCurrent(accountKey, generation)) {
        _pushDeviceId = '${response['id']}';
        _setPushRegistrationWarning(null);
        return true;
      }
    } on KaedeException catch (error) {
      if (surfaceErrors) rethrow;
      _setPushRegistrationWarning(userFacingError(
        error,
        summary:
            'Background notifications could not be registered. In-app messaging still works.',
      ));
    } on Object catch (error) {
      if (surfaceErrors) rethrow;
      _setPushRegistrationWarning(userFacingError(
        error,
        summary:
            'Background notifications could not be registered. In-app messaging still works.',
      ));
    }
    return false;
  }

  Future<void> _activateNotifications() {
    if (_notificationActivation case final pending?) return pending;
    final pending = _requestNotificationDelivery();
    _notificationActivation = pending;
    pending.whenComplete(() {
      if (identical(_notificationActivation, pending)) {
        _notificationActivation = null;
      }
    });
    return pending;
  }

  Future<void> _requestNotificationDelivery() async {
    try {
      final choice = await api.pushOptInChoice();
      if (choice == false) return;
      if (choice == null) {
        // Relay transport introduced an explicit local preference after older
        // builds had already registered devices. Migrate only an installation
        // that the authenticated home confirms was previously registered;
        // this preserves an existing choice without prompting or silently
        // enrolling a new installation.
        final installationId = await api.installationId();
        final devices = await repository.pushDevices();
        final previouslyRegistered =
            hasRegisteredPushInstallation(devices, installationId);
        if (!previouslyRegistered || !await push.permissionGranted()) return;
        final migrated = await _registerPushDevice(
          requireStoredOptIn: false,
        );
        if (migrated) await api.savePushOptIn(true);
        return;
      }
      if (!await push.permissionGranted()) {
        _setPushRegistrationWarning(
          'System notifications are turned off. Enable them in Android settings to receive alerts.',
        );
        return;
      }
      await _registerPushDevice();
    } on Object catch (error) {
      _setPushRegistrationWarning(userFacingError(
        error,
        summary:
            'Notification delivery could not be started. Login and messaging still work.',
      ));
    }
  }

  Future<void> _refreshReadBadges() async {
    final accountKey = api.tokens?.accountKey;
    final generation = _sessionLoadGeneration;
    if (accountKey == null || !_sessionIsCurrent(accountKey, generation)) {
      return;
    }
    try {
      final badges = decodeReadBadgeSnapshot(await repository.readStates());
      if (!_sessionIsCurrent(accountKey, generation)) return;
      final unread = Map<EntityRef, int>.of(badges.unread);
      final mentions = Map<EntityRef, int>.of(badges.mentions);
      for (final entry in _pendingAcknowledgements.entries) {
        final pending = entry.value;
        if (pending.restored) {
          if (pending.unread > 0) {
            unread[entry.key] = max(
              max(unread[entry.key] ?? 0, state.unreadCounts[entry.key] ?? 0),
              pending.unread,
            );
          }
          if (pending.mentions > 0) {
            mentions[entry.key] = max(
              max(
                mentions[entry.key] ?? 0,
                state.mentionCounts[entry.key] ?? 0,
              ),
              pending.mentions,
            );
          }
        } else {
          // This acknowledgement is still in its initial optimistic attempt.
          // Its snapshot is restored if the request fails.
          unread.remove(entry.key);
          mentions.remove(entry.key);
        }
      }
      state = state.copyWith(
        unreadCounts: Map.unmodifiable(unread),
        mentionCounts: Map.unmodifiable(mentions),
      );
      _setDegradedWarning(DegradedFeature.readStates, null);
      _scheduleMetadataCache();
    } on Object catch (error) {
      _setDegradedWarning(
        DegradedFeature.readStates,
        userFacingError(
          error,
          summary:
              'Unread markers could not be refreshed. Existing counts may be incomplete.',
        ),
      );
    }
  }

  /// Requests notification permission as a direct result of a user action and
  /// registers this installation only after consent is granted.
  Future<bool> enablePushNotifications() async {
    if (!await push.requestPermission()) {
      _setPushRegistrationWarning(
        'System notifications are turned off. Allow them in Android settings, then retry.',
      );
      return false;
    }
    if (!push.remoteDeliveryAvailable) {
      _setPushRegistrationWarning(
        'This build is not configured for closed-app notifications. Foreground alerts still work.',
      );
      throw const KaedeException(
        code: 'PUSH_PROVIDER_UNAVAILABLE',
        message:
            'This community build has no compatible background notification provider.',
        status: 503,
      );
    }
    final token = await push.pushToken();
    if (token == null || token.isEmpty) {
      _setPushRegistrationWarning(
        'The device did not provide a notification token. Check notification permissions and its push service, then retry.',
      );
      throw const KaedeException(
        code: 'PUSH_TOKEN_UNAVAILABLE',
        message:
            'The device did not provide a notification token. Check notification permissions and its push service, then retry.',
        status: 503,
      );
    }
    try {
      final registered = await _registerPushDevice(
        token: token,
        surfaceErrors: true,
        requireStoredOptIn: false,
      );
      if (registered) {
        await api.savePushOptIn(true);
        _setPushRegistrationWarning(null);
      }
      return registered;
    } on Object catch (error) {
      _setPushRegistrationWarning(userFacingError(
        error,
        summary: 'Background notifications could not be enabled.',
      ));
      rethrow;
    }
  }

  Future<void> disablePushNotifications() async {
    final deviceId = _pushDeviceId;
    final relayState = await api.relayPushState();
    _pushDeviceId = null;
    await api.savePushOptIn(false);
    if (relayState != null) {
      try {
        await repository.revokeRelayPushSubscription(relayState);
      } on Object {
        // The authenticated home revocation below is an independent path.
      }
    }
    if (deviceId != null) {
      try {
        await repository.unregisterPushDevice(deviceId);
      } on Object {
        // With the device-held relay state removed, subsequent v2 wakes fail
        // authentication even if an offline home cannot revoke immediately.
      }
    }
    await api.clearRelayPushState();
    _setPushRegistrationWarning(null);
  }

  String _message(Object error) => userFacingError(error);

  bool _isOffline(Object error) => error is KaedeException && error.status == 0;

  void _setChannelMessages(EntityRef channel, List<KaedeMessage> messages) {
    final store = Map<EntityRef, List<KaedeMessage>>.of(state.messageStore);
    store[channel] = List<KaedeMessage>.unmodifiable(messages);
    state = state.copyWith(messageStore: Map.unmodifiable(store));
  }

  void _setChannelLoading(EntityRef channel, bool loading) {
    final channels = Set<EntityRef>.of(state.loadingChannels);
    if (loading) {
      channels.add(channel);
    } else {
      channels.remove(channel);
    }
    state = state.copyWith(loadingChannels: Set.unmodifiable(channels));
  }

  Future<void> _flushOutbox() async {
    if (_flushingOutbox) return;
    final tokens = api.tokens;
    if (tokens == null) return;
    final generation = _sessionLoadGeneration;
    _flushingOutbox = true;
    try {
      for (final item in await database.dueOutbox(tokens.accountKey)) {
        if (!_sessionIsCurrent(tokens.accountKey, generation)) return;
        try {
          await _deliverOutboxItem(
            item,
            accountKey: tokens.accountKey,
            generation: generation,
          );
        } on Object {
          // Each item owns its retry state. Continue so one bad destination
          // cannot starve unrelated conversations.
        }
      }
    } finally {
      _flushingOutbox = false;
    }
  }

  Future<void> _deliverOutboxItem(
    OutboxItem item, {
    String? accountKey,
    int? generation,
  }) async {
    final activeAccount = accountKey ?? api.tokens?.accountKey;
    final activeGeneration = generation ?? _sessionLoadGeneration;
    if (activeAccount == null ||
        !_sessionIsCurrent(activeAccount, activeGeneration)) {
      return;
    }
    final channel = EntityRef.parse(item.channelRef);
    final payload = item.payload;
    try {
      final result = await repository.sendMessage(
        channel,
        content: payload['content'] as String?,
        attachments:
            (payload['attachments'] as List<Object?>? ?? const <Object?>[])
                .map(EntityRef.fromJson)
                .toList(),
        replyTo: payload['reply_to'] == null
            ? null
            : EntityRef.fromJson(payload['reply_to']),
        replyAuthor: payload['reply_author'] == null
            ? null
            : EntityRef.fromJson(payload['reply_author']),
        mentionUsers:
            (payload['mention_users'] as List<Object?>? ?? const <Object?>[])
                .map(EntityRef.fromJson)
                .toList(),
        nonce: item.nonce,
      );
      if (!_sessionIsCurrent(activeAccount, activeGeneration)) return;
      await database.completeOutbox(item.nonce);
      if (!_sessionIsCurrent(activeAccount, activeGeneration)) return;
      await _syncOutbox();
      if (result['id'] != null) {
        final message = KaedeMessage.fromJson(result);
        final existing = state.messageStore[channel] ?? const <KaedeMessage>[];
        _setChannelMessages(
          channel,
          mergeMessages(<KaedeMessage>[...existing, message]),
        );
        await _cacheMessages(channel);
      }
    } on KaedeException catch (error) {
      if (!_sessionIsCurrent(activeAccount, activeGeneration)) return;
      final permanent = error.status >= 400 &&
          error.status < 500 &&
          error.status != 408 &&
          error.status != 429;
      final message = userFacingError(error);
      if (permanent) {
        await database.failOutbox(item.nonce, message);
      } else {
        await database.retryOutbox(
          item.nonce,
          item.attempts + 1,
          message,
        );
      }
      await _syncOutbox();
    } on Object catch (error) {
      if (!_sessionIsCurrent(activeAccount, activeGeneration)) return;
      await database.retryOutbox(
        item.nonce,
        item.attempts + 1,
        userFacingError(error),
      );
      await _syncOutbox();
    }
  }

  Future<void> _syncOutbox() async {
    final tokens = api.tokens;
    if (tokens == null) return;
    final items = await database.outboxForAccount(tokens.accountKey);
    if (api.tokens?.accountKey == tokens.accountKey) {
      state = state.copyWith(outbox: items);
    }
  }

  bool _cacheSessionIsCurrent(String accountKey, int generation) =>
      _sessionIsCurrent(accountKey, generation);

  bool _sessionIsCurrent(String accountKey, int generation) =>
      generation == _sessionLoadGeneration &&
      api.tokens?.accountKey == accountKey &&
      state.phase != SessionPhase.signedOut &&
      state.phase != SessionPhase.restoring;

  bool _sessionReadyIsCurrent(String accountKey, int generation) =>
      generation == _sessionLoadGeneration &&
      api.tokens?.accountKey == accountKey &&
      state.phase == SessionPhase.ready;

  bool _messageRequestIsCurrent(
    EntityRef channel,
    int requestGeneration,
    String accountKey,
    int sessionGeneration,
  ) =>
      _messageRequestGenerations[channel] == requestGeneration &&
      _sessionIsCurrent(accountKey, sessionGeneration);

  Future<void> _queueCacheWrite(Future<void> Function() operation) {
    final run = _cacheWriteTail.then((_) => operation());
    _cacheWriteTail = run.then<void>(
      (_) {},
      onError: (Object _, StackTrace __) {},
    );
    return run;
  }

  @override
  void dispose() {
    _authenticationGeneration += 1;
    _sessionLoadGeneration += 1;
    _gatewaySubscription?.cancel();
    _pushTokenSubscription?.cancel();
    _pushDestinationSubscription?.cancel();
    _pushHealthSubscription?.cancel();
    _sessionExpiredSubscription?.cancel();
    _outboxTimer?.cancel();
    _appLockTimer?.cancel();
    _typingExpiryTimer?.cancel();
    _navigationRefreshTimer?.cancel();
    _metadataCacheTimer?.cancel();
    _acknowledgementRetryTimer?.cancel();
    _selfModerationExpiryTimer?.cancel();
    _selfModerationRetryTimer?.cancel();
    _pendingAcknowledgements.clear();
    _acknowledgementsInFlight.clear();
    _gatewayHealthSubscription?.cancel();
    super.dispose();
  }

  Future<T> _optional<T>(Future<T> Function() operation, T fallback) async {
    try {
      return await operation();
    } on Object {
      return fallback;
    }
  }

  Future<({T value, String? warning})> _optionalWithWarning<T>(
    Future<T> Function() operation,
    T fallback, {
    required String summary,
  }) async {
    try {
      return (value: await operation(), warning: null);
    } on Object catch (error) {
      return (
        value: fallback,
        warning: userFacingError(error, summary: summary),
      );
    }
  }

  Future<void> _expireSession() async {
    final accountKey = _activeAccountKey;
    _authenticationGeneration += 1;
    _sessionLoadGeneration += 1;
    _messageRequestGenerations.clear();
    _outboxTimer?.cancel();
    _outboxTimer = null;
    _appLockTimer?.cancel();
    _appLockTimer = null;
    _typingExpiryTimer?.cancel();
    _typingExpiryTimer = null;
    _navigationRefreshTimer?.cancel();
    _navigationRefreshTimer = null;
    _metadataCacheTimer?.cancel();
    _metadataCacheTimer = null;
    _acknowledgementRetryTimer?.cancel();
    _acknowledgementRetryTimer = null;
    _selfModerationExpiryTimer?.cancel();
    _selfModerationExpiryTimer = null;
    _selfModerationRetryTimer?.cancel();
    _selfModerationRetryTimer = null;
    _pendingAcknowledgements.clear();
    _acknowledgementsInFlight.clear();
    await _pushTokenSubscription?.cancel();
    _pushTokenSubscription = null;
    await _pushDestinationSubscription?.cancel();
    _pushDestinationSubscription = null;
    await _pushHealthSubscription?.cancel();
    _pushHealthSubscription = null;
    await _gatewaySubscription?.cancel();
    await _gatewayHealthSubscription?.cancel();
    _gatewayHealthSubscription = null;
    await gateway.disconnect();
    if (accountKey != null) {
      await _queueCacheWrite(() => database.purgeAccount(accountKey));
    }
    _activeAccountKey = null;
    _pushRegistrationWarning = null;
    _pushRemoteDeliveryWarning = null;
    _pushLocalDisplayWarning = null;
    state = const MobileState(
      phase: SessionPhase.signedOut,
      error: 'Your session expired. Sign in again.',
    );
  }
}

Future<void> _purgeDownloadedAttachments(Set<EntityRef> attachments) async {
  if (attachments.isEmpty) return;
  final directory = await getTemporaryDirectory();
  final prefixes = <String>{
    for (final attachment in attachments)
      'kaede-media-${attachment.id.value}-'
          '${attachment.domain.value.replaceAll(RegExp('[^a-z0-9.-]'), '_')}-',
  };
  try {
    await for (final entry in directory.list()) {
      if (entry is! File) continue;
      final name = entry.uri.pathSegments.last;
      if (!prefixes.any(name.startsWith)) continue;
      try {
        await entry.delete();
      } on FileSystemException {
        // A decoder may still hold the file briefly. The in-memory entity is
        // already gone and the next temporary-directory cleanup removes it.
      }
    }
  } on FileSystemException {
    // Revocation must still complete if the operating system has already
    // removed or made the process temporary directory unavailable.
  }
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
