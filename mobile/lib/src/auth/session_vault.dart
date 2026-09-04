import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:uuid/uuid.dart';

final class SessionTokens {
  const SessionTokens({
    required this.instance,
    required this.accessToken,
    required this.refreshToken,
    this.userRef,
  });

  factory SessionTokens.fromJson(Map<String, Object?> json) => SessionTokens(
        instance: Domain(json['instance']! as String),
        accessToken: json['access_token']! as String,
        refreshToken: json['refresh_token']! as String,
        userRef: json['user_ref'] == null
            ? null
            : EntityRef.parse(json['user_ref']! as String),
      );

  final Domain instance;
  final String accessToken;
  final String refreshToken;
  final EntityRef? userRef;

  String get accountKey => '${instance.value}:${userRef?.wire ?? 'pending'}';

  Map<String, Object?> toJson() => <String, Object?>{
        'instance': instance.value,
        'access_token': accessToken,
        'refresh_token': refreshToken,
        'user_ref': userRef?.wire,
      };

  SessionTokens copyWith(
          {String? accessToken, String? refreshToken, EntityRef? userRef}) =>
      SessionTokens(
        instance: instance,
        accessToken: accessToken ?? this.accessToken,
        refreshToken: refreshToken ?? this.refreshToken,
        userRef: userRef ?? this.userRef,
      );
}

final class RelayPushState {
  const RelayPushState({
    required this.home,
    required this.relayUrl,
    required this.relayOrigin,
    required this.subscriptionId,
    required this.routeId,
    required this.wakeSecret,
    required this.managementSecret,
    this.voipSubscriptionId,
    this.voipRouteId,
    this.voipWakeSecret,
    this.voipManagementSecret,
  });

  factory RelayPushState.fromJson(Map<String, Object?> json) => RelayPushState(
        home: Domain(json['home']! as String),
        relayUrl: Uri.parse(json['relay_url']! as String),
        relayOrigin: Domain(json['relay_origin']! as String),
        subscriptionId: json['subscription_id']! as String,
        routeId: json['route_id']! as String,
        wakeSecret: json['wake_secret']! as String,
        managementSecret: json['management_secret']! as String,
        voipSubscriptionId: json['voip_subscription_id'] as String?,
        voipRouteId: json['voip_route_id'] as String?,
        voipWakeSecret: json['voip_wake_secret'] as String?,
        voipManagementSecret: json['voip_management_secret'] as String?,
      );

  final Domain home;
  final Uri relayUrl;
  final Domain relayOrigin;
  final String subscriptionId;
  final String routeId;
  final String wakeSecret;
  final String managementSecret;
  final String? voipSubscriptionId;
  final String? voipRouteId;
  final String? voipWakeSecret;
  final String? voipManagementSecret;

  Map<String, Object?> toJson() => <String, Object?>{
        'home': home.value,
        'relay_url': relayUrl.toString(),
        'relay_origin': relayOrigin.value,
        'subscription_id': subscriptionId,
        'route_id': routeId,
        'wake_secret': wakeSecret,
        'management_secret': managementSecret,
        if (voipSubscriptionId != null)
          'voip_subscription_id': voipSubscriptionId,
        if (voipRouteId != null) 'voip_route_id': voipRouteId,
        if (voipWakeSecret != null) 'voip_wake_secret': voipWakeSecret,
        if (voipManagementSecret != null)
          'voip_management_secret': voipManagementSecret,
      };
}

final class SessionVault {
  const SessionVault([
    this.storage = const FlutterSecureStorage(
      aOptions: AndroidOptions(encryptedSharedPreferences: true),
      iOptions: IOSOptions(
        accessibility: KeychainAccessibility.first_unlock_this_device,
        synchronizable: false,
      ),
    ),
  ]);

  static const _activeSession = 'kaede.active-session.v1';
  static const _installationId = 'kaede.installation-id.v1';
  static const _relayPushState = 'kaede.push-relay-state.v1';
  static const _pushOptIn = 'kaede.push-opt-in.v1';
  final FlutterSecureStorage storage;

  Future<SessionTokens?> read() async {
    final encoded = await storage.read(key: _activeSession);
    if (encoded == null) return null;
    try {
      return SessionTokens.fromJson(
          Map<String, Object?>.from(jsonDecode(encoded) as Map));
    } on Object {
      await clear();
      return null;
    }
  }

  Future<void> write(SessionTokens tokens) =>
      storage.write(key: _activeSession, value: jsonEncode(tokens.toJson()));

  Future<void> clear() async {
    await storage.delete(key: _activeSession);
    await clearRelayPushState();
    await storage.delete(key: _pushOptIn);
  }

  Future<bool?> readPushOptInChoice() async {
    final value = await storage.read(key: _pushOptIn);
    return switch (value) {
      'true' => true,
      'false' => false,
      _ => null,
    };
  }

  Future<bool> readPushOptIn() async => await readPushOptInChoice() ?? false;

  Future<void> writePushOptIn(bool enabled) =>
      storage.write(key: _pushOptIn, value: enabled ? 'true' : 'false');

  Future<RelayPushState?> readRelayPushState() async {
    final encoded = await storage.read(key: _relayPushState);
    if (encoded == null) return null;
    try {
      return RelayPushState.fromJson(
        Map<String, Object?>.from(jsonDecode(encoded) as Map),
      );
    } on Object {
      await clearRelayPushState();
      return null;
    }
  }

  Future<void> writeRelayPushState(RelayPushState state) => storage.write(
        key: _relayPushState,
        value: jsonEncode(state.toJson()),
      );

  Future<void> clearRelayPushState() => storage.delete(key: _relayPushState);

  Future<String> installationId() async {
    final existing = await storage.read(key: _installationId);
    if (existing != null && Uuid.isValidUUID(fromString: existing)) {
      return existing;
    }
    final created = const Uuid().v4();
    await storage.write(key: _installationId, value: created);
    return created;
  }
}
