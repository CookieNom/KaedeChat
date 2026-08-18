import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:path_provider/path_provider.dart';

const _maximumE2eeStateBytes = 32 * 1024 * 1024;
const _maximumLocalE2eeRecordBytes = 80 * 1024 * 1024;
const _maximumPendingRoomOperations = 32;
const maximumMobileMessageCacheEntries = 2000;
const maximumMobileMessageCacheBytes = 8 * 1024 * 1024;
const maximumMobileOutboundVaultBytes = 31 * 1024 * 1024;
final _maximumVaultSequence = BigInt.from(0x7fffffffffffffff);
const mobileZeroVaultChain = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';

String _vaultSequence(Object? value, {bool allowZero = false}) {
  if (value is! String ||
      !(allowZero
              ? RegExp(r'^(0|[1-9][0-9]{0,18})$')
              : RegExp(r'^[1-9][0-9]{0,18}$'))
          .hasMatch(value)) {
    throw const FormatException('Invalid account-vault sequence.');
  }
  final parsed = BigInt.parse(value);
  if (parsed > _maximumVaultSequence) {
    throw const FormatException('Invalid account-vault sequence.');
  }
  return value;
}

String _nextVaultSequence(String value) {
  final parsed = BigInt.parse(_vaultSequence(value, allowZero: true));
  if (parsed >= _maximumVaultSequence) {
    throw const FormatException('Invalid account-vault sequence.');
  }
  return (parsed + BigInt.one).toString();
}

void _validateVaultEnvelope(Map<String, Object?> envelope) {
  const fields = <String>{
    'version',
    'cipher',
    'sequence',
    'nonce',
    'ciphertext',
  };
  if (envelope.length != fields.length ||
      !envelope.keys.toSet().containsAll(fields) ||
      envelope['version'] != 2 ||
      envelope['cipher'] != 'AES-256-GCM' ||
      envelope['nonce'] is! String ||
      envelope['ciphertext'] is! String) {
    throw const FormatException('Invalid account-vault envelope.');
  }
  _vaultSequence(envelope['sequence']);
  final nonce = _decode(envelope['nonce']! as String, maximum: 12);
  final ciphertext = _decode(
    envelope['ciphertext']! as String,
    maximum: _maximumE2eeStateBytes + 16,
  );
  try {
    if (nonce.length != 12 || ciphertext.length < 17) {
      throw const FormatException('Invalid account-vault envelope.');
    }
  } finally {
    nonce.fillRange(0, nonce.length, 0);
    ciphertext.fillRange(0, ciphertext.length, 0);
  }
}

String _vaultDigest(Object? value) {
  if (value is! String) {
    throw const FormatException('Invalid account-vault digest.');
  }
  final decoded = _decode(value, maximum: 32);
  try {
    if (decoded.length != 32) {
      throw const FormatException('Invalid account-vault digest.');
    }
  } finally {
    decoded.fillRange(0, decoded.length, 0);
  }
  return value;
}

String _vaultChain(Object? value) {
  if (value is! String) {
    throw const FormatException('Invalid account-vault chain root.');
  }
  final decoded = _decode(value, maximum: 32);
  try {
    if (decoded.length != 32) {
      throw const FormatException('Invalid account-vault chain root.');
    }
  } finally {
    decoded.fillRange(0, decoded.length, 0);
  }
  return value;
}

final class MobileVaultCheckpoint {
  const MobileVaultCheckpoint({
    required this.accountRef,
    required this.revision,
    required this.digest,
    required this.chainRoot,
  });

  factory MobileVaultCheckpoint.fromJson(
    Map<String, Object?> json, {
    required String accountRef,
    required String accountHash,
  }) {
    const fields = <String>{
      'schema',
      'account_hash',
      'revision',
      'digest',
      'chain_root',
    };
    if (json.length != fields.length ||
        !json.keys.toSet().containsAll(fields) ||
        json['schema'] != 1 ||
        json['account_hash'] != accountHash) {
      throw const FormatException('Invalid account-vault checkpoint.');
    }
    return MobileVaultCheckpoint(
      accountRef: accountRef,
      revision: _vaultSequence(json['revision']),
      digest: _vaultDigest(json['digest']),
      chainRoot: _vaultChain(json['chain_root']),
    );
  }

  final String accountRef;
  final String revision;
  final String digest;
  final String chainRoot;

  Map<String, Object?> toJson(String accountHash) => <String, Object?>{
        'schema': 1,
        'account_hash': accountHash,
        'revision': revision,
        'digest': digest,
        'chain_root': chainRoot,
      };
}

final class MobileMessageCacheEntry {
  const MobileMessageCacheEntry({
    required this.plaintext,
    required this.authorRef,
    required this.messageRef,
  });

  factory MobileMessageCacheEntry.fromJson(Map<String, Object?> json) {
    const fields = <String>{'plaintext', 'authorRef', 'messageRef'};
    final plaintext = json['plaintext'];
    final authorRef = json['authorRef'];
    final messageRef = json['messageRef'];
    if (json.length != fields.length ||
        !json.keys.toSet().containsAll(fields) ||
        plaintext is! String ||
        authorRef is! String ||
        (messageRef != null && messageRef is! String)) {
      throw const FormatException('Invalid encrypted message cache.');
    }
    try {
      if (EntityRef.parse(authorRef).wire != authorRef ||
          (messageRef is String &&
              EntityRef.parse(messageRef).wire != messageRef)) {
        throw const FormatException('Invalid encrypted message cache.');
      }
    } on Object {
      throw const FormatException('Invalid encrypted message cache.');
    }
    return MobileMessageCacheEntry(
      plaintext: plaintext,
      authorRef: authorRef,
      messageRef: messageRef as String?,
    );
  }

  final String plaintext;
  final String authorRef;
  final String? messageRef;

  Map<String, Object?> toJson() => <String, Object?>{
        'plaintext': plaintext,
        'authorRef': authorRef,
        'messageRef': messageRef,
      };

  MobileMessageCacheEntry bindMessage(String ref) => MobileMessageCacheEntry(
        plaintext: plaintext,
        authorRef: authorRef,
        messageRef: ref,
      );

  @override
  bool operator ==(Object other) =>
      other is MobileMessageCacheEntry &&
      other.plaintext == plaintext &&
      other.authorRef == authorRef &&
      other.messageRef == messageRef;

  @override
  int get hashCode => Object.hash(plaintext, authorRef, messageRef);
}

int mobileMessageCacheSerializedBytes(
  Map<String, MobileMessageCacheEntry> cache,
) {
  final encoded = Uint8List.fromList(
    utf8.encode(
      jsonEncode(cache.map((key, value) => MapEntry(key, value.toJson()))),
    ),
  );
  try {
    return encoded.length;
  } finally {
    encoded.fillRange(0, encoded.length, 0);
  }
}

void trimMobileMessageCache(
  Map<String, MobileMessageCacheEntry> cache, {
  int maximumBytes = maximumMobileMessageCacheBytes,
}) {
  while (cache.length > maximumMobileMessageCacheEntries) {
    cache.remove(cache.keys.first);
  }
  var serializedBytes = mobileMessageCacheSerializedBytes(cache);
  while (cache.isNotEmpty && serializedBytes > maximumBytes) {
    final key = cache.keys.first;
    final value = cache[key]!;
    final singleEntryBytes = mobileMessageCacheSerializedBytes(
          <String, MobileMessageCacheEntry>{key: value},
        ) -
        2;
    final commaBytes = cache.length > 1 ? 1 : 0;
    cache.remove(key);
    serializedBytes -= singleEntryBytes + commaBytes;
  }
}

int mobilePortableStateSerializedBytes(MobileE2EEState state) {
  final encoded = Uint8List.fromList(
    utf8.encode(jsonEncode(state.toPortableJson())),
  );
  try {
    return encoded.length;
  } finally {
    encoded.fillRange(0, encoded.length, 0);
  }
}

Map<String, MobileMessageCacheEntry> _decodeMessageCache(Object? value) {
  if (value is! Map || value.length > maximumMobileMessageCacheEntries) {
    throw const FormatException('Invalid encrypted message cache.');
  }
  final decoded = <String, MobileMessageCacheEntry>{};
  for (final entry in value.entries) {
    if (entry.key is! String || entry.value is! Map) {
      throw const FormatException('Invalid encrypted message cache.');
    }
    final ciphertext = _decode(entry.key! as String, maximum: 64 * 1024);
    try {
      if (ciphertext.isEmpty) {
        throw const FormatException('Invalid encrypted message cache.');
      }
    } finally {
      ciphertext.fillRange(0, ciphertext.length, 0);
    }
    decoded[entry.key! as String] = MobileMessageCacheEntry.fromJson(
      Map<String, Object?>.from(entry.value! as Map),
    );
  }
  if (mobileMessageCacheSerializedBytes(decoded) >
      maximumMobileMessageCacheBytes) {
    throw const FormatException('Invalid encrypted message cache.');
  }
  return Map<String, MobileMessageCacheEntry>.unmodifiable(decoded);
}

final class MobilePendingRoomOperation {
  const MobilePendingRoomOperation({
    required this.operationId,
    required this.channelRef,
    required this.kind,
    required this.phase,
    this.policyGeneration,
    this.groupId,
    this.commit,
    this.welcome,
  });

  factory MobilePendingRoomOperation.fromJson(Map<String, Object?> json) {
    final operationId = json['operationId'];
    final channelRef = json['channelRef'];
    final kind = json['kind'];
    final phase = json['phase'];
    final expectedKeys = phase == 'proposing'
        ? const <String>{
            'version',
            'operationId',
            'channelRef',
            'kind',
            'phase',
          }
        : const <String>{
            'version',
            'operationId',
            'channelRef',
            'kind',
            'phase',
            'policyGeneration',
            'groupId',
            'commit',
            'welcome',
          };
    if (json['version'] != 1 ||
        operationId is! String ||
        !RegExp(r'^keo_[A-Za-z0-9_-]{43}$').hasMatch(operationId) ||
        channelRef is! String ||
        channelRef.length > 512 ||
        EntityRef.parse(channelRef).wire != channelRef ||
        (kind != 'activate' && kind != 'rekey') ||
        (phase != 'proposing' && phase != 'activating') ||
        json.keys.toSet().difference(expectedKeys).isNotEmpty ||
        expectedKeys.difference(json.keys.toSet()).isNotEmpty) {
      throw const FormatException('Invalid encrypted-room recovery state.');
    }
    if (phase == 'proposing') {
      return MobilePendingRoomOperation(
        operationId: operationId,
        channelRef: channelRef,
        kind: kind as String,
        phase: phase as String,
      );
    }
    final policyGeneration = json['policyGeneration'];
    final groupId = json['groupId'];
    final commit = json['commit'];
    final welcome = json['welcome'];
    if (policyGeneration is! String ||
        !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(policyGeneration) ||
        groupId is! String ||
        commit is! String ||
        welcome is! String) {
      throw const FormatException('Invalid encrypted-room recovery state.');
    }
    final groupBytes = _decode(groupId, maximum: 32);
    final commitBytes = _decode(commit, maximum: 64 * 1024);
    final welcomeBytes = _decode(welcome, maximum: 64 * 1024);
    try {
      if (groupBytes.length != 32 ||
          commitBytes.isEmpty ||
          welcomeBytes.isEmpty) {
        throw const FormatException('Invalid encrypted-room recovery state.');
      }
    } finally {
      groupBytes.fillRange(0, groupBytes.length, 0);
      commitBytes.fillRange(0, commitBytes.length, 0);
      welcomeBytes.fillRange(0, welcomeBytes.length, 0);
    }
    return MobilePendingRoomOperation(
      operationId: operationId,
      channelRef: channelRef,
      kind: kind as String,
      phase: phase as String,
      policyGeneration: policyGeneration,
      groupId: groupId,
      commit: commit,
      welcome: welcome,
    );
  }

  final String operationId;
  final String channelRef;
  final String kind;
  final String phase;
  final String? policyGeneration;
  final String? groupId;
  final String? commit;
  final String? welcome;

  Map<String, Object?> toJson() => <String, Object?>{
        'version': 1,
        'operationId': operationId,
        'channelRef': channelRef,
        'kind': kind,
        'phase': phase,
        if (phase == 'activating') 'policyGeneration': policyGeneration,
        if (phase == 'activating') 'groupId': groupId,
        if (phase == 'activating') 'commit': commit,
        if (phase == 'activating') 'welcome': welcome,
      };
}

Map<String, MobilePendingRoomOperation> _decodePendingRoomOperations(
  Object? value,
) {
  if (value == null) return const <String, MobilePendingRoomOperation>{};
  if (value is! Map || value.length > _maximumPendingRoomOperations) {
    throw const FormatException('Invalid encrypted-room recovery state.');
  }
  final decoded = <String, MobilePendingRoomOperation>{};
  for (final entry in value.entries) {
    if (entry.key is! String || entry.value is! Map<Object?, Object?>) {
      throw const FormatException('Invalid encrypted-room recovery state.');
    }
    final operation = MobilePendingRoomOperation.fromJson(
      Map<String, Object?>.from(entry.value! as Map<Object?, Object?>),
    );
    if (operation.operationId != entry.key) {
      throw const FormatException('Invalid encrypted-room recovery state.');
    }
    decoded[operation.operationId] = operation;
  }
  return Map<String, MobilePendingRoomOperation>.unmodifiable(decoded);
}

final class MobileE2EEState {
  const MobileE2EEState({
    required this.accountRef,
    required this.deviceId,
    required this.credential,
    required this.mlsState,
    required this.vaultSequence,
    required this.vaultParentChain,
    this.messageCache = const <String, MobileMessageCacheEntry>{},
    this.controlCursors = const <String, String>{},
    this.pendingRoomOperations = const <String, MobilePendingRoomOperation>{},
    this.pendingVaultBaseRevision,
    this.pendingVaultEnvelope,
    this.confirmedVaultRevision,
    this.confirmedVaultDigest,
    this.confirmedVaultChainRoot,
  });

  factory MobileE2EEState.fromJson(Map<String, Object?> json) {
    if (json['schema'] != 2 ||
        json['account_ref'] is! String ||
        json['device_id'] is! String ||
        json['credential'] is! String ||
        json['mls_state'] is! String ||
        json['vault_sequence'] is! String ||
        json['vault_parent_chain'] is! String) {
      throw const FormatException('Invalid mobile encryption state.');
    }
    final rawCache = json['message_cache'];
    final rawControlCursors = json['control_cursors'];
    final rawPendingRoomOperations = json['pending_room_operations'];
    final pendingBase = json['pending_vault_base_revision'];
    final pendingEnvelope = json['pending_vault_envelope'];
    final confirmedRevision = json['confirmed_vault_revision'];
    final confirmedDigest = json['confirmed_vault_digest'];
    final confirmedChainRoot = json['confirmed_vault_chain_root'];
    if ((pendingBase == null) != (pendingEnvelope == null) ||
        (pendingBase != null &&
            (pendingBase is! String ||
                !RegExp(r'^(0|[1-9][0-9]{0,18})$').hasMatch(pendingBase))) ||
        (pendingEnvelope != null && pendingEnvelope is! Map) ||
        ((confirmedRevision == null) != (confirmedDigest == null) ||
            (confirmedRevision == null) != (confirmedChainRoot == null))) {
      throw const FormatException('Invalid mobile vault write journal.');
    }
    final vaultSequence = _vaultSequence(json['vault_sequence']);
    final vaultParentChain = _vaultChain(json['vault_parent_chain']);
    final normalizedPendingEnvelope = pendingEnvelope is Map
        ? Map<String, Object?>.from(pendingEnvelope)
        : null;
    final normalizedConfirmedRevision =
        confirmedRevision == null ? null : _vaultSequence(confirmedRevision);
    final normalizedConfirmedDigest =
        confirmedDigest == null ? null : _vaultDigest(confirmedDigest);
    final normalizedConfirmedChainRoot =
        confirmedChainRoot == null ? null : _vaultChain(confirmedChainRoot);
    if (normalizedPendingEnvelope != null) {
      _validateVaultEnvelope(normalizedPendingEnvelope);
      final base = _vaultSequence(pendingBase, allowZero: true);
      final next = _nextVaultSequence(base);
      if (normalizedPendingEnvelope['sequence'] != next ||
          vaultSequence != next ||
          (normalizedConfirmedRevision == null
              ? base != '0'
              : normalizedConfirmedRevision != base) ||
          (base == '0'
              ? vaultParentChain != mobileZeroVaultChain
              : vaultParentChain != normalizedConfirmedChainRoot)) {
        throw const FormatException('Invalid mobile vault write journal.');
      }
    } else if (normalizedConfirmedRevision != null &&
        normalizedConfirmedRevision != vaultSequence) {
      throw const FormatException('Invalid mobile vault high-water mark.');
    }
    return MobileE2EEState(
      accountRef: json['account_ref']! as String,
      deviceId: json['device_id']! as String,
      credential: json['credential']! as String,
      mlsState: json['mls_state']! as String,
      vaultSequence: vaultSequence,
      vaultParentChain: vaultParentChain,
      messageCache: _decodeMessageCache(rawCache),
      controlCursors: _decodeControlCursors(rawControlCursors),
      pendingRoomOperations:
          _decodePendingRoomOperations(rawPendingRoomOperations),
      pendingVaultBaseRevision: pendingBase as String?,
      pendingVaultEnvelope: normalizedPendingEnvelope,
      confirmedVaultRevision: normalizedConfirmedRevision,
      confirmedVaultDigest: normalizedConfirmedDigest,
      confirmedVaultChainRoot: normalizedConfirmedChainRoot,
    );
  }

  final String accountRef;
  final String deviceId;
  final String credential;
  final String mlsState;
  final String vaultSequence;
  final String vaultParentChain;
  final Map<String, MobileMessageCacheEntry> messageCache;
  final Map<String, String> controlCursors;
  final Map<String, MobilePendingRoomOperation> pendingRoomOperations;
  final String? pendingVaultBaseRevision;
  final Map<String, Object?>? pendingVaultEnvelope;
  final String? confirmedVaultRevision;
  final String? confirmedVaultDigest;
  final String? confirmedVaultChainRoot;

  Map<String, Object?> toJson() => {
        'schema': 2,
        'account_ref': accountRef,
        'device_id': deviceId,
        'credential': credential,
        'mls_state': mlsState,
        'vault_sequence': vaultSequence,
        'vault_parent_chain': vaultParentChain,
        'message_cache': messageCache.map(
          (key, value) => MapEntry(key, value.toJson()),
        ),
        'control_cursors': controlCursors,
        'pending_room_operations': pendingRoomOperations.map(
          (key, value) => MapEntry(key, value.toJson()),
        ),
        if (pendingVaultBaseRevision != null)
          'pending_vault_base_revision': pendingVaultBaseRevision,
        if (pendingVaultEnvelope != null)
          'pending_vault_envelope': pendingVaultEnvelope,
        if (confirmedVaultRevision != null)
          'confirmed_vault_revision': confirmedVaultRevision,
        if (confirmedVaultDigest != null)
          'confirmed_vault_digest': confirmedVaultDigest,
        if (confirmedVaultChainRoot != null)
          'confirmed_vault_chain_root': confirmedVaultChainRoot,
      };

  MobileE2EEState withPendingVaultWrite(
    String baseRevision,
    Map<String, Object?> envelope,
  ) {
    _validateVaultEnvelope(envelope);
    final next = _nextVaultSequence(baseRevision);
    if (vaultSequence != next || envelope['sequence'] != next) {
      throw const FormatException('Invalid mobile vault write journal.');
    }
    if ((baseRevision == '0' &&
            (confirmedVaultRevision != null ||
                confirmedVaultDigest != null ||
                confirmedVaultChainRoot != null ||
                vaultParentChain != mobileZeroVaultChain)) ||
        (baseRevision != '0' &&
            (confirmedVaultRevision != baseRevision ||
                confirmedVaultDigest == null ||
                confirmedVaultChainRoot == null ||
                vaultParentChain != confirmedVaultChainRoot))) {
      throw const FormatException('Invalid mobile vault write journal.');
    }
    return MobileE2EEState(
      accountRef: accountRef,
      deviceId: deviceId,
      credential: credential,
      mlsState: mlsState,
      vaultSequence: vaultSequence,
      vaultParentChain: vaultParentChain,
      messageCache: messageCache,
      controlCursors: controlCursors,
      pendingRoomOperations: pendingRoomOperations,
      pendingVaultBaseRevision: baseRevision,
      pendingVaultEnvelope: Map<String, Object?>.from(envelope),
      confirmedVaultRevision: confirmedVaultRevision,
      confirmedVaultDigest: confirmedVaultDigest,
      confirmedVaultChainRoot: confirmedVaultChainRoot,
    );
  }

  MobileE2EEState confirmed(
    String revision,
    String digest,
    String chainRoot,
  ) {
    final normalizedRevision = _vaultSequence(revision);
    final normalizedDigest = _vaultDigest(digest);
    final normalizedChainRoot = _vaultChain(chainRoot);
    if (vaultSequence != normalizedRevision) {
      throw const FormatException('Invalid mobile vault high-water mark.');
    }
    return MobileE2EEState(
      accountRef: accountRef,
      deviceId: deviceId,
      credential: credential,
      mlsState: mlsState,
      vaultSequence: vaultSequence,
      vaultParentChain: vaultParentChain,
      messageCache: messageCache,
      controlCursors: controlCursors,
      pendingRoomOperations: pendingRoomOperations,
      confirmedVaultRevision: normalizedRevision,
      confirmedVaultDigest: normalizedDigest,
      confirmedVaultChainRoot: normalizedChainRoot,
    );
  }

  MobileE2EEState withConfirmedVault(
    String revision,
    String digest,
    String chainRoot,
  ) =>
      confirmed(revision, digest, chainRoot);

  /// Rebase only after an authenticated password-reset response confirms the
  /// remote opaque vault was deleted. A missing server vault by itself is
  /// never sufficient authority to lower the local rollback high-water mark.
  MobileE2EEState rebasedAfterPasswordReset() => MobileE2EEState(
        accountRef: accountRef,
        deviceId: deviceId,
        credential: credential,
        mlsState: mlsState,
        vaultSequence: '1',
        vaultParentChain: mobileZeroVaultChain,
        messageCache: messageCache,
        controlCursors: controlCursors,
        pendingRoomOperations: pendingRoomOperations,
      );

  /// Portable shape shared with the web/Tauri account vault and recovery
  /// bundle. Keep these camelCase keys wire-compatible across every client.
  Map<String, Object?> toPortableJson() => {
        'schema': 2,
        'accountRef': accountRef,
        'deviceId': deviceId,
        'credential': credential,
        'mlsState': mlsState,
        'vaultSequence': vaultSequence,
        'vaultParentChain': vaultParentChain,
        'messageCache': messageCache.map(
          (key, value) => MapEntry(key, value.toJson()),
        ),
        'controlCursors': controlCursors,
        'pendingRoomOperations': pendingRoomOperations.map(
          (key, value) => MapEntry(key, value.toJson()),
        ),
      };

  static MobileE2EEState fromPortableJson(Map<String, Object?> json) {
    final cache = json['messageCache'];
    final controlCursors = json['controlCursors'];
    final pendingRoomOperations = json['pendingRoomOperations'];
    const fields = <String>{
      'schema',
      'accountRef',
      'deviceId',
      'credential',
      'mlsState',
      'vaultSequence',
      'vaultParentChain',
      'messageCache',
      'controlCursors',
      'pendingRoomOperations',
    };
    if (json.length != fields.length ||
        !json.keys.toSet().containsAll(fields) ||
        json['schema'] != 2 ||
        json['accountRef'] is! String ||
        json['deviceId'] is! String ||
        json['credential'] is! String ||
        json['mlsState'] is! String ||
        json['vaultSequence'] is! String ||
        json['vaultParentChain'] is! String ||
        cache is! Map ||
        controlCursors is! Map) {
      throw const FormatException('Invalid encryption recovery state.');
    }
    final accountRef = json['accountRef']! as String;
    final deviceId = json['deviceId']! as String;
    final credential = json['credential']! as String;
    final mlsState = json['mlsState']! as String;
    if (accountRef.isEmpty ||
        !RegExp(r'^ked_[A-Za-z0-9_-]{43}$').hasMatch(deviceId) ||
        credential.isEmpty ||
        mlsState.isEmpty) {
      throw const FormatException('Invalid encryption recovery state.');
    }
    return MobileE2EEState(
      accountRef: accountRef,
      deviceId: deviceId,
      credential: credential,
      mlsState: mlsState,
      vaultSequence: _vaultSequence(json['vaultSequence']),
      vaultParentChain: _vaultChain(json['vaultParentChain']),
      messageCache: _decodeMessageCache(cache),
      controlCursors: _decodeControlCursors(controlCursors),
      pendingRoomOperations:
          _decodePendingRoomOperations(pendingRoomOperations),
    );
  }
}

Map<String, String> _decodeControlCursors(Object? value) {
  if (value == null) return const <String, String>{};
  if (value is! Map || value.length > 6400) {
    throw const FormatException('Invalid encryption control cursors.');
  }
  final decoded = <String, String>{};
  for (final entry in value.entries) {
    if (entry.key is! String || entry.value is! String) {
      throw const FormatException('Invalid encryption control cursor.');
    }
    final channel = EntityRef.parse(entry.key! as String);
    final cursor = EntityRef.parse(entry.value! as String);
    if (channel.wire != entry.key || cursor.wire != entry.value) {
      throw const FormatException('Invalid encryption control cursor.');
    }
    decoded[channel.wire] = cursor.wire;
  }
  return Map<String, String>.unmodifiable(decoded);
}

/// Encrypted-at-rest MLS state. The wrapping key remains in Keychain or
/// Android Keystore-backed encrypted preferences; the potentially large MLS
/// state is written atomically to app-private storage.
final class MobileE2EEStore {
  const MobileE2EEStore();

  static const _secure = FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
    iOptions: IOSOptions(
      accessibility: KeychainAccessibility.first_unlock_this_device,
      synchronizable: false,
    ),
  );
  static final _aead = AesGcm.with256bits();
  static final _recoveryKdf = Pbkdf2(
    macAlgorithm: Hmac.sha256(),
    iterations: 600000,
    bits: 256,
  );

  Future<String> _accountKey(String accountRef) async {
    final digest = await Sha256().hash(utf8.encode(accountRef));
    return _base64url(digest.bytes);
  }

  Future<String> _checkpointKey(String accountRef) async =>
      'kaede.mobile.e2ee-checkpoint.v2.${await _accountKey(accountRef)}';

  /// Load the non-secret rollback checkpoint. It intentionally survives
  /// logout and ordinary local MLS-state deletion.
  Future<MobileVaultCheckpoint?> loadCheckpoint(String accountRef) async {
    final accountHash = await _accountKey(accountRef);
    final encoded = await _secure.read(
      key: 'kaede.mobile.e2ee-checkpoint.v2.$accountHash',
    );
    if (encoded == null) return null;
    final checkpoint = MobileVaultCheckpoint.fromJson(
      Map<String, Object?>.from(jsonDecode(encoded) as Map),
      accountRef: accountRef,
      accountHash: accountHash,
    );
    if (checkpoint.accountRef != accountRef) {
      throw const FormatException(
        'Account-vault checkpoint belongs to another account.',
      );
    }
    return checkpoint;
  }

  /// Persist a monotonic rollback checkpoint in platform-protected storage.
  /// Only an authenticated encryption/password reset may explicitly clear it.
  Future<void> saveCheckpoint(MobileVaultCheckpoint checkpoint) async {
    final current = await loadCheckpoint(checkpoint.accountRef);
    if (current != null) {
      final comparison = BigInt.parse(checkpoint.revision).compareTo(
        BigInt.parse(current.revision),
      );
      if (comparison < 0 ||
          (comparison == 0 &&
              (checkpoint.digest != current.digest ||
                  checkpoint.chainRoot != current.chainRoot))) {
        throw StateError(
          'Refusing to lower or replace the encryption vault checkpoint.',
        );
      }
    }
    await _secure.write(
      key: await _checkpointKey(checkpoint.accountRef),
      value: jsonEncode(
        checkpoint.toJson(await _accountKey(checkpoint.accountRef)),
      ),
    );
  }

  Future<void> clearCheckpoint(String accountRef) async {
    await _secure.delete(key: await _checkpointKey(accountRef));
  }

  Future<File> _file(String accountRef) async {
    final root = await getApplicationSupportDirectory();
    final directory = Directory('${root.path}/e2ee');
    await directory.create(recursive: true);
    return File('${directory.path}/${await _accountKey(accountRef)}.state');
  }

  Future<SecretKeyData> _wrappingKey(String accountRef) async {
    final name = 'kaede.mobile.e2ee-key.v1.${await _accountKey(accountRef)}';
    final stored = await _secure.read(key: name);
    if (stored != null) {
      final bytes = _decode(stored, maximum: 32);
      if (bytes.length != 32) {
        bytes.fillRange(0, bytes.length, 0);
        throw StateError('The protected encryption key is invalid.');
      }
      return SecretKeyData(
        bytes,
        overwriteWhenDestroyed: true,
        debugLabel: 'Kaede local MLS wrapping key',
      );
    }
    final bytes = _random(32);
    try {
      await _secure.write(key: name, value: _base64url(bytes));
      return SecretKeyData(
        bytes,
        overwriteWhenDestroyed: true,
        debugLabel: 'Kaede local MLS wrapping key',
      );
    } on Object {
      bytes.fillRange(0, bytes.length, 0);
      rethrow;
    }
  }

  Future<MobileE2EEState?> load(String accountRef) async {
    final file = await _file(accountRef);
    if (!await file.exists()) return null;
    final record = Map<String, Object?>.from(
      jsonDecode(await file.readAsString()) as Map,
    );
    if (record['version'] != 1) {
      throw const FormatException('Unsupported encryption state.');
    }
    final ciphertext = _decode(
      '${record['ciphertext']}',
      maximum: _maximumLocalE2eeRecordBytes,
    );
    final nonce = _decode('${record['nonce']}', maximum: 12);
    final mac = _decode('${record['mac']}', maximum: 16);
    if (nonce.length != 12 || mac.length != 16) {
      ciphertext.fillRange(0, ciphertext.length, 0);
      nonce.fillRange(0, nonce.length, 0);
      mac.fillRange(0, mac.length, 0);
      throw const FormatException('Invalid protected encryption state.');
    }
    SecretKeyData? key;
    Uint8List? plaintext;
    try {
      key = await _wrappingKey(accountRef);
      plaintext = Uint8List.fromList(await _aead.decrypt(
        SecretBox(ciphertext, nonce: nonce, mac: Mac(mac)),
        secretKey: key,
        aad: utf8.encode('kaede-mobile-e2ee-state-v1\u0000$accountRef'),
      ));
      final state = MobileE2EEState.fromJson(
        Map<String, Object?>.from(
          jsonDecode(utf8.decode(plaintext, allowMalformed: false)) as Map,
        ),
      );
      if (state.accountRef != accountRef) {
        throw const FormatException(
          'Encryption state belongs to another account.',
        );
      }
      return state;
    } finally {
      plaintext?.fillRange(0, plaintext.length, 0);
      ciphertext.fillRange(0, ciphertext.length, 0);
      nonce.fillRange(0, nonce.length, 0);
      mac.fillRange(0, mac.length, 0);
      key?.destroy();
    }
  }

  Future<void> save(MobileE2EEState state) async {
    final file = await _file(state.accountRef);
    final plaintext = Uint8List.fromList(
      utf8.encode(jsonEncode(state.toJson())),
    );
    if (plaintext.length > _maximumLocalE2eeRecordBytes) {
      plaintext.fillRange(0, plaintext.length, 0);
      throw StateError('Encryption state is too large.');
    }
    SecretKeyData? key;
    try {
      key = await _wrappingKey(state.accountRef);
      final box = await _aead.encrypt(
        plaintext,
        secretKey: key,
        aad: utf8.encode('kaede-mobile-e2ee-state-v1\u0000${state.accountRef}'),
      );
      final temporary = File('${file.path}.new');
      await temporary.writeAsString(
          jsonEncode({
            'version': 1,
            'nonce': _base64url(box.nonce),
            'ciphertext': _base64url(box.cipherText),
            'mac': _base64url(box.mac.bytes),
          }),
          flush: true);
      await temporary.rename(file.path);
    } finally {
      plaintext.fillRange(0, plaintext.length, 0);
      key?.destroy();
    }
  }

  Future<void> clear(String accountRef) async {
    final key = 'kaede.mobile.e2ee-key.v1.${await _accountKey(accountRef)}';
    await _secure.delete(key: key);
    final file = await _file(accountRef);
    if (await file.exists()) await file.delete();
  }

  Future<bool> rebaseAfterPasswordReset(String accountRef) async {
    final state = await load(accountRef);
    await clearCheckpoint(accountRef);
    if (state == null) return false;
    await save(state.rebasedAfterPasswordReset());
    return true;
  }

  Future<Map<String, Object?>> sealAccountVault(
    MobileE2EEState state,
    SecretKey vaultKey, {
    List<int>? nonce,
  }) async {
    final plaintext = Uint8List.fromList(
      utf8.encode(jsonEncode(state.toPortableJson())),
    );
    if (plaintext.length > _maximumE2eeStateBytes) {
      plaintext.fillRange(0, plaintext.length, 0);
      throw StateError('Encryption state is too large.');
    }
    try {
      final sequence = _vaultSequence(state.vaultSequence);
      final box = await _aead.encrypt(
        plaintext,
        secretKey: vaultKey,
        nonce: nonce,
        aad: utf8.encode(
          'kaede account vault v2\u0000${state.accountRef}\u0000$sequence',
        ),
      );
      final sealed = Uint8List.fromList(
        <int>[...box.cipherText, ...box.mac.bytes],
      );
      try {
        return <String, Object?>{
          'version': 2,
          'cipher': 'AES-256-GCM',
          'sequence': sequence,
          'nonce': _base64url(box.nonce),
          'ciphertext': _base64url(sealed),
        };
      } finally {
        sealed.fillRange(0, sealed.length, 0);
      }
    } finally {
      plaintext.fillRange(0, plaintext.length, 0);
    }
  }

  Future<MobileE2EEState> openAccountVault(
    String accountRef,
    SecretKey vaultKey,
    Map<String, Object?> envelope,
  ) async {
    _validateVaultEnvelope(envelope);
    final sequence = _vaultSequence(envelope['sequence']);
    final nonce = _decode('${envelope['nonce']}', maximum: 12);
    final sealed = _decode(
      '${envelope['ciphertext']}',
      maximum: _maximumE2eeStateBytes + 16,
    );
    if (nonce.length != 12 || sealed.length < 17) {
      nonce.fillRange(0, nonce.length, 0);
      sealed.fillRange(0, sealed.length, 0);
      throw const FormatException(
        'The server returned an invalid encryption vault.',
      );
    }
    Uint8List? plaintext;
    try {
      plaintext = Uint8List.fromList(
        await _aead.decrypt(
          SecretBox(
            sealed.sublist(0, sealed.length - 16),
            nonce: nonce,
            mac: Mac(sealed.sublist(sealed.length - 16)),
          ),
          secretKey: vaultKey,
          aad: utf8.encode(
            'kaede account vault v2\u0000$accountRef\u0000$sequence',
          ),
        ),
      );
      if (plaintext.length > _maximumE2eeStateBytes) {
        throw const FormatException('Encryption state is too large.');
      }
      final state = MobileE2EEState.fromPortableJson(
        Map<String, Object?>.from(
          jsonDecode(utf8.decode(plaintext, allowMalformed: false)) as Map,
        ),
      );
      if (state.accountRef != accountRef) {
        throw const FormatException(
          'Encryption vault belongs to another account.',
        );
      }
      if (state.vaultSequence != sequence) {
        throw const FormatException(
          'Encryption vault sequence does not match its ciphertext.',
        );
      }
      return state;
    } finally {
      plaintext?.fillRange(0, plaintext.length, 0);
      nonce.fillRange(0, nonce.length, 0);
      sealed.fillRange(0, sealed.length, 0);
    }
  }

  Future<String> exportRecovery(
      MobileE2EEState state, String passphrase) async {
    if (passphrase.length < 12) {
      throw ArgumentError('Use at least 12 characters.');
    }
    final salt = _random(16);
    final material = SecretKeyData(
      Uint8List.fromList(utf8.encode(passphrase)),
      overwriteWhenDestroyed: true,
      debugLabel: 'Kaede recovery passphrase',
    );
    SecretKey? key;
    final plaintext = Uint8List.fromList(
      utf8.encode(jsonEncode(state.toPortableJson())),
    );
    if (plaintext.length > _maximumE2eeStateBytes) {
      material.destroy();
      salt.fillRange(0, salt.length, 0);
      plaintext.fillRange(0, plaintext.length, 0);
      throw StateError('Encryption state is too large.');
    }
    try {
      key = await _recoveryKdf.deriveKey(secretKey: material, nonce: salt);
      final box = await _aead.encrypt(
        plaintext,
        secretKey: key,
        aad: utf8.encode('kaede recovery v1\u0000${state.accountRef}'),
      );
      return jsonEncode({
        'version': 1,
        'kdf': 'PBKDF2-SHA256',
        'iterations': 600000,
        'salt': _base64url(salt),
        'cipher': 'AES-256-GCM',
        'nonce': _base64url(box.nonce),
        'ciphertext': _base64url(<int>[...box.cipherText, ...box.mac.bytes]),
      });
    } finally {
      plaintext.fillRange(0, plaintext.length, 0);
      salt.fillRange(0, salt.length, 0);
      key?.destroy();
      material.destroy();
    }
  }

  Future<MobileE2EEState> openRecovery(
    String accountRef,
    String bundle,
    String passphrase,
  ) async {
    final record = Map<String, Object?>.from(jsonDecode(bundle) as Map);
    if (record['version'] != 1 ||
        record['kdf'] != 'PBKDF2-SHA256' ||
        record['iterations'] != 600000 ||
        record['cipher'] != 'AES-256-GCM') {
      throw const FormatException('Unsupported recovery bundle.');
    }
    final salt = _decode('${record['salt']}', maximum: 16);
    final sealed = _decode(
      '${record['ciphertext']}',
      maximum: _maximumE2eeStateBytes + 16,
    );
    final nonce = _decode('${record['nonce']}', maximum: 12);
    if (salt.length != 16 || nonce.length != 12 || sealed.length < 17) {
      salt.fillRange(0, salt.length, 0);
      nonce.fillRange(0, nonce.length, 0);
      sealed.fillRange(0, sealed.length, 0);
      throw const FormatException('Invalid recovery bundle.');
    }
    final material = SecretKeyData(
      Uint8List.fromList(utf8.encode(passphrase)),
      overwriteWhenDestroyed: true,
      debugLabel: 'Kaede recovery passphrase',
    );
    SecretKey? key;
    Uint8List? plaintext;
    try {
      key = await _recoveryKdf.deriveKey(secretKey: material, nonce: salt);
      plaintext = Uint8List.fromList(await _aead.decrypt(
        SecretBox(
          sealed.sublist(0, sealed.length - 16),
          nonce: nonce,
          mac: Mac(sealed.sublist(sealed.length - 16)),
        ),
        secretKey: key,
        aad: utf8.encode('kaede recovery v1\u0000$accountRef'),
      ));
      final state = MobileE2EEState.fromPortableJson(
        Map<String, Object?>.from(
          jsonDecode(utf8.decode(plaintext, allowMalformed: false)) as Map,
        ),
      );
      if (state.accountRef != accountRef) {
        throw const FormatException(
          'Recovery bundle belongs to another account.',
        );
      }
      return state;
    } finally {
      plaintext?.fillRange(0, plaintext.length, 0);
      sealed.fillRange(0, sealed.length, 0);
      nonce.fillRange(0, nonce.length, 0);
      salt.fillRange(0, salt.length, 0);
      key?.destroy();
      material.destroy();
    }
  }

  Future<MobileE2EEState> importRecovery(
    String accountRef,
    String bundle,
    String passphrase,
  ) async {
    final state = await openRecovery(accountRef, bundle, passphrase);
    await save(state);
    return state;
  }
}

Uint8List _random(int length) {
  final random = Random.secure();
  return Uint8List.fromList(List.generate(length, (_) => random.nextInt(256)));
}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

Uint8List _decode(String value, {required int maximum}) {
  if (value.isEmpty || value.length > maximum * 2) {
    throw const FormatException('Invalid encoded encryption value.');
  }
  final decoded = base64Url.decode(base64Url.normalize(value));
  if (decoded.length > maximum || _base64url(decoded) != value) {
    decoded.fillRange(0, decoded.length, 0);
    throw const FormatException('Non-canonical encryption value.');
  }
  return Uint8List.fromList(decoded);
}
