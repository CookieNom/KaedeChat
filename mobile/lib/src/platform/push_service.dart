import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';

enum NotificationKind {
  directMessage,
  mention,
  guildMessage,
  call,
  moderation,
  activity,
}

final class OpaquePushWake {
  const OpaquePushWake(this.eventToken);

  final String eventToken;

  static final _tokenPattern = RegExp(r'^[A-Za-z0-9_-]{43}$');

  static OpaquePushWake? parse(Map<String, dynamic> data) {
    if (data.length != 2 ||
        !data.containsKey('sync_version') ||
        !data.containsKey('event_token')) {
      return null;
    }
    if ('${data['sync_version'] ?? ''}' != '1') return null;
    final token = '${data['event_token'] ?? ''}';
    return _tokenPattern.hasMatch(token) ? OpaquePushWake(token) : null;
  }
}

final class PushNotificationEnvelope {
  const PushNotificationEnvelope({
    required this.kind,
    required this.title,
    required this.body,
    required this.destination,
  });

  final NotificationKind kind;
  final String title;
  final String body;
  final PushDestination destination;

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
      if (kind == null ||
          title is! String ||
          title.isEmpty ||
          body is! String ||
          body.isEmpty ||
          destination == null ||
          destination.message == null) {
        return null;
      }
      return PushNotificationEnvelope(
        kind: kind,
        title: title,
        body: body,
        destination: destination,
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
  final local = await _initializeBackgroundNotifications();
  final redemption = await _redeemOpaqueWake(wake);
  final notification = redemption.notification;
  if (notification != null) {
    await _showLocalNotification(
      local,
      id: stableNotificationId(notification.destination.message!.wire),
      kind: notification.kind,
      title: notification.title,
      body: notification.body,
      payload: notification.destination.encode(),
    );
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
  } on Object {
    return const _PushRedemption(showFallback: true);
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
  PushService._(this._local, this._firebaseReady, this._destinations);

  final FlutterLocalNotificationsPlugin _local;
  final bool _firebaseReady;
  final StreamController<PushDestination> _destinations;
  final List<StreamSubscription<dynamic>> _subscriptions =
      <StreamSubscription<dynamic>>[];
  PushDestination? _initialDestination;
  var _appActive = true;
  EntityRef? _visibleChannel;

  Stream<PushDestination> get destinations => _destinations.stream;
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
    service = PushService._(local, firebaseReady, destinations);
    final launch = await local.getNotificationAppLaunchDetails();
    service._initialDestination =
        PushDestination.parse(launch?.notificationResponse?.payload);
    if (firebaseReady) await service._startFirebaseListeners();
    return service;
  }

  Future<void> _startFirebaseListeners() async {
    _subscriptions.add(FirebaseMessaging.onMessage.listen(_showRemoteMessage));
  }

  Future<void> _showRemoteMessage(RemoteMessage message) async {
    final wake = OpaquePushWake.parse(message.data);
    if (wake == null) return;
    final redemption = await _redeemOpaqueWake(wake);
    final notification = redemption.notification;
    if (notification != null) {
      if (_appActive && notification.destination.channel == _visibleChannel) {
        return;
      }
      await show(
        // Gateway and FCM can race while the process is foregrounded. Keying
        // both deliveries by the Kaede message reference makes the second one
        // replace the first instead of showing a duplicate notification.
        id: stableNotificationId(notification.destination.message!.wire),
        kind: notification.kind,
        title: notification.title,
        body: notification.body,
        payload: notification.destination.encode(),
      );
    } else if (redemption.showFallback) {
      await show(
        id: stableNotificationId(wake.eventToken),
        kind: NotificationKind.activity,
        title: 'Kaede Chat',
        body: 'New activity is waiting in Kaede.',
      );
    }
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

  Future<void> show({
    required int id,
    required NotificationKind kind,
    required String title,
    required String body,
    String? payload,
  }) async {
    await _showLocalNotification(
      _local,
      id: id,
      kind: kind,
      title: title,
      body: body,
      payload: payload,
    );
  }

  Future<void> dispose() async {
    for (final subscription in _subscriptions) {
      await subscription.cancel();
    }
    await _destinations.close();
  }
}

Future<void> _showLocalNotification(
  FlutterLocalNotificationsPlugin local, {
  required int id,
  required NotificationKind kind,
  required String title,
  required String body,
  String? payload,
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
