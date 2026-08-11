import 'dart:async';
import 'dart:io';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/message_store.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
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

String _lastConversationKey(String accountKey) =>
    'last_conversation:${Uri.encodeComponent(accountKey)}';

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

final class MobileState {
  const MobileState({
    this.phase = SessionPhase.restoring,
    this.user,
    this.guilds = const <KaedeGuild>[],
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
    this.offline = false,
    this.error,
  });

  final SessionPhase phase;
  final KaedeUser? user;
  final List<KaedeGuild> guilds;
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
    bool? offline,
    String? error,
    bool clearError = false,
  }) =>
      MobileState(
        phase: phase ?? this.phase,
        user: clearUser ? null : user ?? this.user,
        guilds: guilds ?? this.guilds,
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
  StreamSubscription<GatewayEvent>? _gatewaySubscription;
  StreamSubscription<String>? _pushTokenSubscription;
  StreamSubscription<PushDestination>? _pushDestinationSubscription;
  StreamSubscription<void>? _sessionExpiredSubscription;
  Timer? _outboxTimer;
  Timer? _appLockTimer;
  Timer? _typingExpiryTimer;
  Timer? _navigationRefreshTimer;
  Timer? _metadataCacheTimer;
  String? _pushDeviceId;
  var _appActive = true;
  var _conversationPaneVisible = false;
  DateTime? _backgroundedAt;
  final Map<EntityRef, int> _messageRequestGenerations = <EntityRef, int>{};
  var _flushingOutbox = false;
  var _sessionLoadGeneration = 0;
  var _authenticationGeneration = 0;
  Future<void>? _navigationRefresh;
  Future<void>? _notificationActivation;
  Future<void> _cacheWriteTail = Future<void>.value();
  String? _activeAccountKey;

  void setAppActive(bool active) {
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
      unawaited(_flushOutbox());
      unawaited(_refreshReadBadges());
      unawaited(_activateNotifications());
      _acknowledgeVisibleConversation();
    } else {
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
    if (visible) _acknowledgeVisibleConversation();
  }

  bool _isChannelVisible(EntityRef channel) => shouldAcknowledgeVisibleChannel(
        appActive: _appActive,
        conversationPaneVisible: _conversationPaneVisible,
        selectedChannel: state.selectedChannel,
        channel: channel,
      );

  void _acknowledgeVisibleConversation() {
    final channel = state.selectedChannel;
    if (channel == null || !_isChannelVisible(channel)) return;
    // Opening the pane is the local read intent. Clear its marker immediately;
    // the API acknowledgement below makes that intent durable across clients.
    _clearUnread(channel);
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
    final settings = await _optional(
      repository.settings,
      const <String, Object?>{},
    );
    final guildSettings = await _optional(
      repository.guildNotificationSettingsList,
      const <Map<String, Object?>>[],
    );
    final readStates = await _optional(
      repository.readStates,
      const <Map<String, Object?>>[],
    );
    if (generation != _sessionLoadGeneration) return;
    final rawNotifications = settings['notification_settings'];
    final notifications = rawNotifications is Map<Object?, Object?>
        ? rawNotifications.map((key, value) => MapEntry('$key', value == true))
        : const <String, bool>{};
    final guildNotificationLevels = <String, String>{};
    for (final item in guildSettings) {
      final id = item['guild_id'];
      final domain = item['guild_domain'];
      if (id != null && domain != null) {
        guildNotificationLevels['$id@$domain'] =
            '${item['level'] ?? 'mentions'}';
      }
    }
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
    final preferences = await SharedPreferences.getInstance();
    final initial = resolveInitialConversation(
      saved: preferences.getString(_lastConversationKey(resolved.accountKey)),
      dms: dms,
      guilds: guilds,
    );
    state = MobileState(
      phase: SessionPhase.ready,
      user: user,
      guilds: guilds,
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
      offline: false,
    );
    push.setAppVisibility(
      active: _appActive,
      visibleChannel:
          _appActive && _conversationPaneVisible ? initial?.ref : null,
    );
    if (initial != null) unawaited(loadMessages());
    await _cacheLists();
    await _startSessionServices(resolved.accountKey, generation);
  }

  Future<void> _startSessionServices(
    String accountKey,
    int generation,
  ) async {
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
    await _gatewaySubscription?.cancel();
    if (!_sessionReadyIsCurrent(accountKey, generation)) return;
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
      state = state.copyWith(
        user: results[0] as KaedeUser,
        guilds: results[1] as List<KaedeGuild>,
        dms: results[2] as List<KaedeChannel>,
        unreadCounts: badges.unread,
        mentionCounts: badges.mentions,
        offline: false,
        clearError: true,
      );
      await _cacheLists();
      unawaited(_refreshRelationships());
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
    gateway.updatePresence(presence.name);
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
    _acknowledgeVisibleConversation();
    if (channel.type == ChannelType.text ||
        channel.type == ChannelType.announcement ||
        channel.type == ChannelType.dm) {
      await loadMessages();
    }
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
      if (page.length >= 50) {
        withOlder.add(channelRef);
      } else {
        withOlder.remove(channelRef);
      }
      final current = state.messageStore[channelRef] ?? existing;
      _setChannelMessages(
        channelRef,
        older ? mergeMessages(<KaedeMessage>[...ordered, ...current]) : ordered,
      );
      state = state.copyWith(
        offline: false,
        channelsWithOlderMessages: Set.unmodifiable(withOlder),
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
    await _pushTokenSubscription?.cancel();
    _pushTokenSubscription = null;
    await _pushDestinationSubscription?.cancel();
    _pushDestinationSubscription = null;
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
    await _gatewaySubscription?.cancel();
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
    } on Object {
      // A newer or malformed event must not terminate the gateway stream.
      _scheduleNavigationRefresh();
    }
  }

  void _applyGateway(GatewayEvent event) {
    switch (event.name) {
      case 'READY':
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
          _clearUnread(message.channelRef);
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
          final reason =
              '${event.data['reason'] ?? event.data['code'] ?? 'Message could not be sent.'}';
          unawaited(
            database.failOutbox(nonce, reason).then((_) => _syncOutbox()),
          );
        }
        break;
      case 'MESSAGE_DELIVERY_UPDATE':
        final target = _messageRef(event.data);
        if (target != null) {
          _patchStoredMessage(
            target,
            (message) => message.copyWith(
              deliveryStatus: '${event.data['status'] ?? 'pending'}',
            ),
          );
        }
        break;
      case 'DM_OPEN_REJECTED':
        state = state.copyWith(
          error:
              '${event.data['reason'] ?? 'This direct message could not be opened.'}',
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
      case 'CHANNEL_ACCESS_REVOKED':
        unawaited(_revokeChannelAccess(event.data));
        break;
      case 'CHANNEL_ACCESS_GRANTED' ||
            'CHANNEL_PERMISSION_UPDATE' ||
            'GUILD_ROLE_CREATE' ||
            'GUILD_ROLE_UPDATE' ||
            'GUILD_ROLE_DELETE' ||
            'GUILD_MEMBER_ADD' ||
            'GUILD_MEMBER_UPDATE' ||
            'GUILD_MEMBER_REMOVE' ||
            'GUILD_MEMBERS_CHUNK' ||
            'GUILD_MEMBER_LIST_UPDATE' ||
            'GUILD_AVAILABILITY_UPDATE' ||
            'GUILD_EMOJI_CREATE' ||
            'GUILD_EMOJI_DELETE' ||
            'VOICE_STATE_UPDATE' ||
            'VOICE_CHANNEL_MOVE' ||
            'VOICE_TOKEN' ||
            'CALL_CREATE' ||
            'CALL_RING' ||
            'CALL_ACCEPT' ||
            'CALL_DECLINE' ||
            'CALL_END' ||
            'USER_UPDATE' ||
            'RESUMED' ||
            'INVALID_SESSION':
        _scheduleNavigationRefresh();
        break;
      case 'PRESENCE_UPDATE':
        final user = _userRef(event.data);
        if (user != null) {
          final presence =
              _presence(event.data['status'] ?? event.data['preference']);
          final presenceByUser = Map<EntityRef, PresenceStatus>.of(
            state.presenceByUser,
          )..[user] = presence;
          state = state.copyWith(
            presenceByUser: Map.unmodifiable(presenceByUser),
            presencePreference:
                user == state.user?.ref ? presence : state.presencePreference,
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
        final key = '$reaction';
        final count = (counts[key] ?? 0) + (data['removed'] == true ? -1 : 1);
        if (count <= 0) {
          counts.remove(key);
        } else {
          counts[key] = count;
        }
        next = next.copyWith(reactionCounts: Map.unmodifiable(counts));
      }
      if (data.containsKey('delivery_status')) {
        next = next.copyWith(deliveryStatus: '${data['delivery_status']}');
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
    _clearUnread(channel);
    try {
      await repository.acknowledge(channel, message);
    } on Object {
      // The next visible message or channel load retries this ephemeral ack.
    }
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
    final showPreview =
        state.notificationSettings['show_notification_previews'] ?? false;
    final privateBody = switch (kind) {
      NotificationKind.directMessage => 'New direct message',
      NotificationKind.mention => 'You were mentioned',
      NotificationKind.guildMessage => 'New guild message',
      _ => 'Open Kaede to view this update.',
    };
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
  }

  Future<void> _registerPushDevice({String? token}) async {
    final accountKey = api.tokens?.accountKey;
    final generation = _sessionLoadGeneration;
    if (accountKey == null || !_sessionIsCurrent(accountKey, generation)) {
      return;
    }
    try {
      final resolvedToken = token ?? await push.pushToken();
      if (resolvedToken == null ||
          resolvedToken.isEmpty ||
          !_sessionIsCurrent(accountKey, generation)) {
        return;
      }
      final response = await repository.registerPushDevice(
        installationId: await api.installationId(),
        token: resolvedToken,
        platform: Platform.isIOS ? 'ios' : 'android',
        deviceName: Platform.operatingSystemVersion.length <= 100
            ? Platform.operatingSystemVersion
            : Platform.operatingSystemVersion.substring(0, 100),
      );
      if (_sessionIsCurrent(accountKey, generation)) {
        _pushDeviceId = '${response['id']}';
      }
    } on KaedeException catch (error) {
      if (error.code != 'PUSH_DISABLED' && error.status != 503) {
        // Push is supplemental. Login and foreground gateway delivery must
        // remain usable when registration is temporarily unavailable.
      }
    } on Object {
      // Firebase is optional for self-hosted and development builds.
    }
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
      if (!await push.requestPermission()) return;
      await _registerPushDevice();
    } on Object {
      // Notification availability must never block session restoration.
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
      if (state.selectedChannel case final selected?
          when _isChannelVisible(selected)) {
        unread.remove(selected);
        mentions.remove(selected);
      }
      state = state.copyWith(
        unreadCounts: Map.unmodifiable(unread),
        mentionCounts: Map.unmodifiable(mentions),
      );
      _scheduleMetadataCache();
    } on Object {
      // Gateway-maintained counters remain usable while REST is unavailable.
    }
  }

  /// Requests notification permission as a direct result of a user action and
  /// registers this installation only after consent is granted.
  Future<bool> enablePushNotifications() async {
    if (!await push.requestPermission()) return false;
    final token = await push.pushToken();
    if (token != null && token.isNotEmpty) {
      await _registerPushDevice(token: token);
    }
    return true;
  }

  String _message(Object error) =>
      error is KaedeException ? error.message : error.toString();

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
      if (permanent) {
        await database.failOutbox(item.nonce, error.message);
      } else {
        await database.retryOutbox(
          item.nonce,
          item.attempts + 1,
          error.message,
        );
      }
      await _syncOutbox();
    } on Object catch (error) {
      if (!_sessionIsCurrent(activeAccount, activeGeneration)) return;
      await database.retryOutbox(
        item.nonce,
        item.attempts + 1,
        '$error',
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
    _sessionExpiredSubscription?.cancel();
    _outboxTimer?.cancel();
    _appLockTimer?.cancel();
    _typingExpiryTimer?.cancel();
    _navigationRefreshTimer?.cancel();
    _metadataCacheTimer?.cancel();
    super.dispose();
  }

  Future<T> _optional<T>(Future<T> Function() operation, T fallback) async {
    try {
      return await operation();
    } on Object {
      return fallback;
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
    await _pushTokenSubscription?.cancel();
    _pushTokenSubscription = null;
    await _pushDestinationSubscription?.cancel();
    _pushDestinationSubscription = null;
    await _gatewaySubscription?.cancel();
    await gateway.disconnect();
    if (accountKey != null) {
      await _queueCacheWrite(() => database.purgeAccount(accountKey));
    }
    _activeAccountKey = null;
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
