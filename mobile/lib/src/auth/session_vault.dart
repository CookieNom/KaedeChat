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

  Future<void> clear() => storage.delete(key: _activeSession);

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
