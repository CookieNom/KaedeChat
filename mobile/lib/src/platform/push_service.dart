import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui';

import 'package:cryptography/cryptography.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:path_provider/path_provider.dart';

enum NotificationKind {
  directMessage,
  mention,
  guildMessage,
  call,
  moderation,
  activity,
}

const _configuredPushTransport = String.fromEnvironment(
  'KAEDE_PUSH_TRANSPORT',
  defaultValue: 'relay',
);

final class OpaquePushWake {
  const OpaquePushWake({
    required this.version,
    required this.eventToken,
    this.routeId,
    this.deliveryId,
    this.expiresAt,
    this.wakeMac,
  });

  final int version;
  final String eventToken;
  final String? routeId;
  final String? deliveryId;
  final int? expiresAt;
  final String? wakeMac;

  static final _tokenPattern = RegExp(r'^[A-Za-z0-9_-]{43}$');

  static OpaquePushWake? parse(Map<String, dynamic> data) {
    final version = int.tryParse('${data['sync_version'] ?? ''}');
    final token = '${data['event_token'] ?? ''}';
    if (!_tokenPattern.hasMatch(token)) return null;
    if (version == 1 && data.length == 2) {
      return OpaquePushWake(version: 1, eventToken: token);
    }
    if (version != 2 || data.length != 6) return null;
    final routeId = '${data['route_id'] ?? ''}';
    final deliveryId = '${data['delivery_id'] ?? ''}';
    final expiresAt = int.tryParse('${data['expires_at'] ?? ''}');
    final wakeMac = '${data['wake_mac'] ?? ''}';
    if (!_tokenPattern.hasMatch(routeId) ||
        !_tokenPattern.hasMatch(deliveryId) ||
        !_tokenPattern.hasMatch(wakeMac) ||
        expiresAt == null) {
      return null;
    }
    return OpaquePushWake(
      version: 2,
      eventToken: token,
      routeId: routeId,
      deliveryId: deliveryId,
      expiresAt: expiresAt,
      wakeMac: wakeMac,
    );
  }
}

Future<bool> authenticatePushWake(
  OpaquePushWake wake,
  RelayPushState? state, {
  String configuredTransport = _configuredPushTransport,
  int? nowEpochSeconds,
}) async {
  // Direct/community builds have no relay state and may accept the legacy
  // home-to-FCM wake. A relay enrollment requires a device-authenticated v2
  // wake, so the relay cannot downgrade to an unsigned v1 payload.
  if (wake.version == 1) {
    return configuredTransport == 'direct_fcm' && state == null;
  }
  final expiresAt = wake.expiresAt;
  final now = nowEpochSeconds ?? DateTime.now().millisecondsSinceEpoch ~/ 1000;
  if (state == null ||
      wake.routeId != state.routeId ||
      expiresAt == null ||
      expiresAt < now ||
      expiresAt > now + 600) {
    return false;
  }
  try {
    final key = base64Url.decode(
      base64Url.normalize(state.wakeSecret),
    );
    final canonical = utf8.encode(
      '2\n${wake.routeId}\n${wake.eventToken}\n${wake.deliveryId}\n$expiresAt',
    );
    final calculated = await Hmac.sha256().calculateMac(
      canonical,
      secretKey: SecretKey(key),
    );
    final supplied = base64Url.decode(base64Url.normalize(wake.wakeMac!));
    if (calculated.bytes.length != supplied.length) return false;
    var difference = 0;
    for (var index = 0; index < supplied.length; index += 1) {
      difference |= calculated.bytes[index] ^ supplied[index];
    }
    return difference == 0;
  } on Object {
    return false;
  }
}

Future<bool> _trustedRelayWake(OpaquePushWake wake, SessionVault vault) async =>
    authenticatePushWake(wake, await vault.readRelayPushState());

final class PushNotificationEnvelope {
  const PushNotificationEnvelope({
    required this.kind,
    required this.title,
    required this.body,
    required this.destination,
    this.senderName,
    this.senderRef,
    this.senderAvatarHash,
    this.sentAt,
  });

  final NotificationKind kind;
  final String title;
  final String body;
  final PushDestination destination;
  final String? senderName;
  final EntityRef? senderRef;
  final String? senderAvatarHash;
  final DateTime? sentAt;

  Uri? get senderAvatarUri => senderRef == null
      ? null
      : publicAssetUri(
          senderRef!.domain,
          senderAvatarHash,
          variant: 'thumbnail_128',
        );

  static PushNotificationEnvelope? parse(Map<String, Object?> json) {
    try {
      final kind = switch ('${json['kind'] ?? ''}') {
        'direct_message' => NotificationKind.directMessage,
        'mention' => NotificationKind.mention,
        'guild_message' => NotificationKind.guildMessage,
        _ => null,
      };
      final title = json['title'];
      final body = json['body'];
      final destination = PushDestination.parse(json);
      final rawSenderName = json['sender_name'];
      final rawSenderRef = json['sender_ref'];
      final senderName =
          rawSenderName is String && rawSenderName.trim().isNotEmpty
              ? rawSenderName.trim()
              : null;
      final senderRef = senderName == null || rawSenderRef is! String
          ? null
          : EntityRef.parse(rawSenderRef);
      final avatarHash = json['sender_avatar_hash'];
      final senderAvatarHash =
          avatarHash is String && RegExp(r'^[0-9a-f]{64}$').hasMatch(avatarHash)
              ? avatarHash
              : null;
      final sentAt = DateTime.tryParse('${json['sent_at'] ?? ''}')?.toUtc();
      if (kind == null ||
          title is! String ||
          title.isEmpty ||
          body is! String ||
          body.isEmpty ||
          destination == null ||
          destination.message == null ||
          (senderName != null && senderRef == null)) {
        return null;
      }
      return PushNotificationEnvelope(
        kind: kind,
        title: title,
        body: body,
        destination: destination,
        senderName: senderName,
        senderRef: senderRef,
        senderAvatarHash: senderAvatarHash,
        sentAt: sentAt,
      );
    } on Object {
      return null;
    }
  }
}

final class _PushRedemption {
  const _PushRedemption({this.notification, this.showFallback = false});

  final PushNotificationEnvelope? notification;
  final bool showFallback;
}

/// Produces the same positive notification identifier on every Dart process.
///
/// `String.hashCode` is deliberately not used here: its implementation is not
/// a wire-format contract, so an app restart could prevent a later delivery of
/// the same event from replacing the earlier system notification. FNV-1a keeps
/// the identifier stable while fitting Android's signed 31-bit ID space.
int stableNotificationId(String value) {
  var hash = 0x811c9dc5;
  for (final byte in utf8.encode(value)) {
    hash ^= byte;
    hash = (hash * 0x01000193) & 0xffffffff;
  }
  return hash & 0x7fffffff;
}

final class PushDestination {
  const PushDestination({required this.channel, this.message});

  final EntityRef channel;
  final EntityRef? message;

  String encode() => jsonEncode(<String, String>{
        'channel_ref': channel.wire,
        if (message != null) 'message_ref': message!.wire,
      });

  Uri toUri() => Uri(
        scheme: 'kaede',
        host: 'app',
        path: '/open',
        queryParameters: <String, String>{
          'channel_ref': channel.wire,
          if (message != null) 'message_ref': message!.wire,
        },
      );

  static PushDestination? fromUri(Uri uri) {
    if (uri.scheme != 'kaede' || uri.host != 'app' || uri.path != '/open') {
      return null;
    }
    return parse(uri.queryParameters);
  }

  static PushDestination? parse(Object? value) {
    if (value == null) return null;
    try {
      if (value case final Map<Object?, Object?> map) {
        final channel = '${map['channel_ref'] ?? ''}';
        if (channel.isEmpty) return null;
        final message = '${map['message_ref'] ?? ''}';
        return PushDestination(
          channel: EntityRef.parse(channel),
          message: message.isEmpty ? null : EntityRef.parse(message),
        );
      }
      final text = '$value'.trim();
      if (text.isEmpty) return null;
      final uri = Uri.tryParse(text);
      if (uri != null && uri.hasScheme) return fromUri(uri);
      if (text.startsWith('{')) {
        return parse(jsonDecode(text));
      }
      return PushDestination(channel: EntityRef.parse(text));
    } on Object {
      return null;
    }
  }
}

@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  WidgetsFlutterBinding.ensureInitialized();
  DartPluginRegistrant.ensureInitialized();
  try {
    await Firebase.initializeApp();
  } on Object {
    // A release without platform Firebase configuration intentionally keeps
    // foreground notifications available and ignores remote delivery.
    return;
  }
  final wake = OpaquePushWake.parse(message.data);
  if (wake == null) return;
  const vault = SessionVault();
  if (!await _trustedRelayWake(wake, vault)) return;
  final local = await _initializeBackgroundNotifications();
  final redemption = await _redeemOpaqueWake(wake);
  final notification = redemption.notification;
  if (notification != null) {
    await _displayRedeemedNotification(local, notification);
  } else if (redemption.showFallback) {
    await _showLocalNotification(
      local,
      id: stableNotificationId(wake.eventToken),
      kind: NotificationKind.activity,
      title: 'Kaede Chat',
      body: 'New activity is waiting in Kaede.',
    );
  }
}

Future<_PushRedemption> _redeemOpaqueWake(OpaquePushWake wake) async {
  final api = KaedeApiClient(vault: const SessionVault());
  final tokens = await api.restore();
  if (tokens == null) return const _PushRedemption();
  try {
    final response = await api.sendJson(
      'POST',
      '/api/v1/users/@me/push-devices/notifications/redeem',
      data: <String, Object?>{
        'installation_id': await api.installationId(),
        'event_token': wake.eventToken,
      },
    ).timeout(const Duration(seconds: 8));
    if (response.isEmpty) return const _PushRedemption();
    final notification = PushNotificationEnvelope.parse(response);
    return notification == null
        ? const _PushRedemption(showFallback: true)
        : _PushRedemption(notification: notification);
  } on KaedeException catch (error) {
    if (error.status == 404 || error.status == 204) {
      return const _PushRedemption();
    }
    return _PushRedemption(
      showFallback: error.status == 0 || error.status >= 500,
    );
  } on TimeoutException {
    return const _PushRedemption(showFallback: true);
  } on Object {
    return const _PushRedemption();
  }
}

Future<FlutterLocalNotificationsPlugin>
    _initializeBackgroundNotifications() async {
  final local = FlutterLocalNotificationsPlugin();
  await local.initialize(
    const InitializationSettings(
      android: AndroidInitializationSettings('@drawable/ic_stat_kaede'),
      iOS: DarwinInitializationSettings(
        notificationCategories: PushService._darwinCategories,
      ),
      macOS: DarwinInitializationSettings(
        notificationCategories: PushService._darwinCategories,
      ),
    ),
  );
  final android = local.resolvePlatformSpecificImplementation<
      AndroidFlutterLocalNotificationsPlugin>();
  for (final channel in PushService._androidChannels) {
    await android?.createNotificationChannel(channel);
  }
  return local;
}

final class PushService {
  PushService._(
    this._local,
    this._firebaseReady,
    this._destinations,
    this._healthEvents,
  );

  /// Test-only constructor: builds the service around inert platform
  /// objects so widget tests can exercise the controller without
  /// notification or Firebase channels.
  @visibleForTesting
  PushService.test({bool firebaseReady = true})
      : this._(
          FlutterLocalNotificationsPlugin(),
          firebaseReady,
          StreamController<PushDestination>.broadcast(),
          StreamController<String?>.broadcast(sync: true),
        );

  final FlutterLocalNotificationsPlugin _local;
  final bool _firebaseReady;
  final StreamController<PushDestination> _destinations;
  final StreamController<String?> _healthEvents;
  final List<StreamSubscription<dynamic>> _subscriptions =
      <StreamSubscription<dynamic>>[];
  PushDestination? _initialDestination;
  var _appActive = true;
  EntityRef? _visibleChannel;

  Stream<PushDestination> get destinations => _destinations.stream;
  Stream<String?> get health => _healthEvents.stream;
  bool get remotePushAvailable => _firebaseReady;

  PushDestination? consumeInitialDestination() {
    final destination = _initialDestination;
    _initialDestination = null;
    return destination;
  }

  void setAppVisibility({required bool active, EntityRef? visibleChannel}) {
    _appActive = active;
    _visibleChannel = active ? visibleChannel : null;
  }

  void _emitDestination(PushDestination destination) {
    if (_destinations.hasListener) {
      _destinations.add(destination);
    } else {
      _initialDestination = destination;
    }
  }

  static const _androidChannels = <AndroidNotificationChannel>[
    AndroidNotificationChannel('kaede_dms', 'Direct messages',
        description: 'New direct messages', importance: Importance.high),
    AndroidNotificationChannel('kaede_mentions', 'Mentions',
        description: 'Messages that mention you', importance: Importance.high),
    AndroidNotificationChannel('kaede_guilds', 'Guild messages',
        description: 'Guilds configured for all messages',
        importance: Importance.defaultImportance),
    AndroidNotificationChannel('kaede_calls', 'Calls',
        description: 'Incoming calls and voice alerts',
        importance: Importance.max),
    AndroidNotificationChannel('kaede_moderation', 'Account and moderation',
        description: 'Security and moderation notices',
        importance: Importance.high),
    AndroidNotificationChannel('kaede_activity', 'Background activity',
        description: 'Generic fallback alerts when secure sync is unavailable',
        importance: Importance.high),
  ];

  static const _darwinCategories = <DarwinNotificationCategory>[
    DarwinNotificationCategory('kaede_dms'),
    DarwinNotificationCategory('kaede_mentions'),
    DarwinNotificationCategory('kaede_guilds'),
    DarwinNotificationCategory('kaede_calls'),
    DarwinNotificationCategory('kaede_moderation'),
    DarwinNotificationCategory('kaede_activity'),
  ];

  static Future<PushService> create() async {
    final local = FlutterLocalNotificationsPlugin();
    final destinations = StreamController<PushDestination>.broadcast();
    final healthEvents = StreamController<String?>.broadcast(sync: true);
    late final PushService service;
    await local.initialize(
      const InitializationSettings(
        android: AndroidInitializationSettings('@drawable/ic_stat_kaede'),
        iOS: DarwinInitializationSettings(
          notificationCategories: _darwinCategories,
        ),
        macOS: DarwinInitializationSettings(
          notificationCategories: _darwinCategories,
        ),
      ),
      onDidReceiveNotificationResponse: (response) {
        final destination = PushDestination.parse(response.payload);
        if (destination != null) service._emitDestination(destination);
      },
    );
    final android = local.resolvePlatformSpecificImplementation<
        AndroidFlutterLocalNotificationsPlugin>();
    for (final channel in _androidChannels) {
      await android?.createNotificationChannel(channel);
    }
    var firebaseReady = false;
    try {
      await Firebase.initializeApp();
      FirebaseMessaging.onBackgroundMessage(
        firebaseMessagingBackgroundHandler,
      );
      firebaseReady = true;
    } on Object {
      // Self-hosters can ship without Firebase credentials. Foreground local
      // notifications continue to work; closed-app push remains unavailable.
    }
    service = PushService._(
      local,
      firebaseReady,
      destinations,
      healthEvents,
    );
    final launch = await local.getNotificationAppLaunchDetails();
    service._initialDestination =
        PushDestination.parse(launch?.notificationResponse?.payload);
    if (firebaseReady) await service._startFirebaseListeners();
    return service;
  }

  Future<void> _startFirebaseListeners() async {
    _subscriptions.add(FirebaseMessaging.onMessage.listen(
      _showRemoteMessage,
      onError: (_) => _reportHealth(
        'Background notification delivery was interrupted. Open Kaede to refresh messages while it reconnects.',
      ),
    ));
  }

  Future<void> _showRemoteMessage(RemoteMessage message) async {
    try {
      final wake = OpaquePushWake.parse(message.data);
      if (wake == null) return;
      if (!await _trustedRelayWake(wake, const SessionVault())) return;
      final redemption = await _redeemOpaqueWake(wake);
      final notification = redemption.notification;
      if (notification != null) {
        _reportHealth(null);
        if (_appActive && notification.destination.channel == _visibleChannel) {
          return;
        }
        await _displayRedeemedNotification(_local, notification);
      } else if (redemption.showFallback) {
        _reportHealth(
          'Notification details could not be fetched securely. A private fallback alert was shown; open Kaede to refresh.',
        );
        await show(
          id: stableNotificationId(wake.eventToken),
          kind: NotificationKind.activity,
          title: 'Kaede Chat',
          body: 'New activity is waiting in Kaede.',
        );
      }
    } on Object {
      _reportHealth(
        'A background notification could not be displayed. Open Kaede to view the message and retry notification delivery.',
      );
    }
  }

  void _reportHealth(String? warning) {
    if (!_healthEvents.isClosed) _healthEvents.add(warning);
  }

  Future<bool> requestPermission() async {
    var localAllowed = true;
    if (Platform.isAndroid) {
      localAllowed = await _local
              .resolvePlatformSpecificImplementation<
                  AndroidFlutterLocalNotificationsPlugin>()
              ?.requestNotificationsPermission() ??
          true;
    } else {
      localAllowed = await _local
              .resolvePlatformSpecificImplementation<
                  IOSFlutterLocalNotificationsPlugin>()
              ?.requestPermissions(alert: true, badge: true, sound: true) ??
          true;
    }
    if (!_firebaseReady) return localAllowed;
    final settings = await FirebaseMessaging.instance.requestPermission(
      alert: true,
      badge: true,
      sound: true,
    );
    return localAllowed && _isAuthorized(settings.authorizationStatus);
  }

  Future<bool> permissionGranted() async {
    final localAllowed = Platform.isAndroid
        ? await _local
                .resolvePlatformSpecificImplementation<
                    AndroidFlutterLocalNotificationsPlugin>()
                ?.areNotificationsEnabled() ??
            true
        : true;
    if (!localAllowed || !_firebaseReady) return localAllowed;
    final settings = await FirebaseMessaging.instance.getNotificationSettings();
    return localAllowed && _isAuthorized(settings.authorizationStatus);
  }

  bool _isAuthorized(AuthorizationStatus status) =>
      status == AuthorizationStatus.authorized ||
      status == AuthorizationStatus.provisional;

  /// Returns a provider token without unexpectedly opening an OS permission
  /// sheet during session restoration. Permission is requested only from an
  /// explicit settings action.
  Future<String?> pushToken({bool requestPermission = false}) async {
    final allowed = requestPermission
        ? await this.requestPermission()
        : await permissionGranted();
    if (!allowed) return null;
    if (!_firebaseReady) return null;
    return FirebaseMessaging.instance.getToken();
  }

  Stream<String> get tokenRefresh => _firebaseReady
      ? FirebaseMessaging.instance.onTokenRefresh
      : const Stream<String>.empty();

  bool get remoteDeliveryAvailable => _firebaseReady;

  Future<void> show({
    required int id,
    required NotificationKind kind,
    required String title,
    required String body,
    String? payload,
    String? senderName,
    EntityRef? senderRef,
    Uri? senderAvatarUri,
    DateTime? sentAt,
  }) async {
    await _showLocalNotification(
      _local,
      id: id,
      kind: kind,
      title: title,
      body: body,
      payload: payload,
      senderName: senderName,
      senderKey: senderRef?.wire,
      sentAt: sentAt,
    );
    final avatarPath = await _notificationAvatar(senderAvatarUri);
    if (avatarPath == null) return;
    await _showLocalNotification(
      _local,
      id: id,
      kind: kind,
      title: title,
      body: body,
      payload: payload,
      senderName: senderName,
      senderKey: senderRef?.wire,
      senderAvatarPath: avatarPath,
      sentAt: sentAt,
    );
  }

  Future<void> dispose() async {
    for (final subscription in _subscriptions) {
      await subscription.cancel();
    }
    await _destinations.close();
    await _healthEvents.close();
  }
}

Future<void> _displayRedeemedNotification(
  FlutterLocalNotificationsPlugin local,
  PushNotificationEnvelope notification,
) async {
  final id = stableNotificationId(notification.destination.message!.wire);
  final payload = notification.destination.encode();
  await _showLocalNotification(
    local,
    id: id,
    kind: notification.kind,
    title: notification.title,
    body: notification.body,
    payload: payload,
    senderName: notification.senderName,
    senderKey: notification.senderRef?.wire,
    sentAt: notification.sentAt,
  );
  final avatarPath = await _notificationAvatar(notification.senderAvatarUri);
  if (avatarPath == null) return;
  await _showLocalNotification(
    local,
    id: id,
    kind: notification.kind,
    title: notification.title,
    body: notification.body,
    payload: payload,
    senderName: notification.senderName,
    senderKey: notification.senderRef?.wire,
    senderAvatarPath: avatarPath,
    sentAt: notification.sentAt,
  );
}

const _maximumNotificationAvatarBytes = 512 * 1024;
const _maximumNotificationAvatarFiles = 64;

Future<String?> _notificationAvatar(Uri? initialUri) async {
  if (initialUri == null || !_safePublicAvatarUri(initialUri)) return null;
  File? partial;
  final client = HttpClient()..connectionTimeout = const Duration(seconds: 3);
  try {
    final directory = Directory(
      '${(await getTemporaryDirectory()).path}/kaede-notification-avatars',
    );
    await directory.create(recursive: true);
    final hash = initialUri.pathSegments[2];
    final host = initialUri.host.replaceAll(RegExp(r'[^a-z0-9.-]'), '_');
    final cached = File('${directory.path}/$host-$hash.img');
    if (await cached.exists()) {
      final length = await cached.length();
      if (length > 0 && length <= _maximumNotificationAvatarBytes) {
        await cached.setLastModified(DateTime.now());
        return cached.path;
      }
      await cached.delete();
    }

    var uri = initialUri;
    for (var redirects = 0; redirects <= 3; redirects += 1) {
      final request = await client.getUrl(uri);
      request.followRedirects = false;
      request.headers.set(HttpHeaders.acceptHeader, 'image/*');
      final response =
          await request.close().timeout(const Duration(seconds: 3));
      if (const <int>{301, 302, 303, 307, 308}.contains(response.statusCode)) {
        final location = response.headers.value(HttpHeaders.locationHeader);
        await response.drain<void>();
        if (location == null || redirects == 3) return null;
        final redirected = uri.resolve(location);
        if (!_safeHttpsUri(redirected)) return null;
        uri = redirected;
        continue;
      }
      if (response.statusCode != HttpStatus.ok ||
          response.headers.contentType?.primaryType != 'image' ||
          response.contentLength > _maximumNotificationAvatarBytes) {
        await response.drain<void>();
        return null;
      }

      partial = File(
        '${cached.path}.${DateTime.now().microsecondsSinceEpoch}.part',
      );
      final sink = partial.openWrite();
      var received = 0;
      try {
        await response.timeout(const Duration(seconds: 3)).forEach((chunk) {
          received += chunk.length;
          if (received > _maximumNotificationAvatarBytes) {
            throw const FormatException('notification avatar is too large');
          }
          sink.add(chunk);
        });
        await sink.flush();
      } finally {
        await sink.close();
      }
      if (received == 0) return null;
      try {
        await partial.rename(cached.path);
      } on FileSystemException {
        if (!await cached.exists()) rethrow;
      }
      partial = null;
      unawaited(_pruneNotificationAvatars(directory));
      return cached.path;
    }
  } on Object {
    return null;
  } finally {
    client.close(force: true);
    if (partial != null && await partial.exists()) {
      await partial.delete();
    }
  }
  return null;
}

bool _safePublicAvatarUri(Uri uri) =>
    _safeHttpsUri(uri) &&
    uri.pathSegments.length == 4 &&
    uri.pathSegments[0] == 'media' &&
    uri.pathSegments[1] == 'assets' &&
    RegExp(r'^[0-9a-f]{64}$').hasMatch(uri.pathSegments[2]) &&
    uri.pathSegments[3] == 'thumbnail_128';

bool _safeHttpsUri(Uri uri) =>
    uri.scheme == 'https' &&
    uri.host.isNotEmpty &&
    uri.userInfo.isEmpty &&
    !uri.hasFragment;

Future<void> _pruneNotificationAvatars(Directory directory) async {
  try {
    final files = await directory
        .list()
        .where((entry) => entry is File && !entry.path.endsWith('.part'))
        .cast<File>()
        .toList();
    if (files.length <= _maximumNotificationAvatarFiles) return;
    final dated = <(File, DateTime)>[];
    for (final file in files) {
      dated.add((file, (await file.stat()).modified));
    }
    dated.sort((left, right) => right.$2.compareTo(left.$2));
    for (final item in dated.skip(_maximumNotificationAvatarFiles)) {
      await item.$1.delete();
    }
  } on Object {
    // Avatar cache cleanup is best effort.
  }
}

Future<void> _showLocalNotification(
  FlutterLocalNotificationsPlugin local, {
  required int id,
  required NotificationKind kind,
  required String title,
  required String body,
  String? payload,
  String? senderName,
  String? senderKey,
  String? senderAvatarPath,
  DateTime? sentAt,
}) async {
  final channel = switch (kind) {
    NotificationKind.directMessage => PushService._androidChannels[0],
    NotificationKind.mention => PushService._androidChannels[1],
    NotificationKind.guildMessage => PushService._androidChannels[2],
    NotificationKind.call => PushService._androidChannels[3],
    NotificationKind.moderation => PushService._androidChannels[4],
    NotificationKind.activity => PushService._androidChannels[5],
  };
  final interruptionLevel = switch (kind) {
    NotificationKind.call => InterruptionLevel.timeSensitive,
    NotificationKind.directMessage ||
    NotificationKind.mention ||
    NotificationKind.moderation ||
    NotificationKind.activity =>
      InterruptionLevel.active,
    NotificationKind.guildMessage => InterruptionLevel.passive,
  };
  final sender = senderName == null
      ? null
      : Person(
          name: senderName,
          key: senderKey,
          icon: senderAvatarPath == null
              ? null
              : BitmapFilePathAndroidIcon(senderAvatarPath),
        );
  final style = sender == null
      ? null
      : MessagingStyleInformation(
          const Person(name: 'You', key: 'kaede-current-user'),
          conversationTitle:
              kind == NotificationKind.directMessage ? null : title,
          groupConversation: kind != NotificationKind.directMessage,
          messages: <Message>[
            Message(body, sentAt ?? DateTime.now().toUtc(), sender),
          ],
        );
  await local.show(
    id,
    title,
    body,
    NotificationDetails(
      android: AndroidNotificationDetails(
        channel.id,
        channel.name,
        channelDescription: channel.description,
        icon: '@drawable/ic_stat_kaede',
        largeIcon: senderAvatarPath == null
            ? null
            : FilePathAndroidBitmap(senderAvatarPath),
        styleInformation: style,
        visibility: NotificationVisibility.private,
        category: sender == null ? null : AndroidNotificationCategory.message,
        onlyAlertOnce: true,
      ),
      iOS: DarwinNotificationDetails(
        presentAlert: true,
        presentBadge: true,
        presentSound: true,
        presentBanner: true,
        presentList: true,
        categoryIdentifier: channel.id,
        threadIdentifier: channel.id,
        interruptionLevel: interruptionLevel,
      ),
    ),
    payload: payload,
  );
}
