import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/native_mls.dart';
import 'package:kaede_mobile/src/e2ee/store.dart';

const mlsProtocol = 'mls10';
const mlsSuite = 'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519';
const _keyPackageBatch = 20;

Uint8List _messageContextBytes(Map<String, Object?> context) {
  final fields = <String>[
    'kaede-message-envelope-v2',
    '${context['channel_ref']}',
    '${context['group_id']}',
    '${context['policy_generation']}',
    '${context['epoch']}',
    '${context['sender_device_id']}',
    '${context['operation']}',
    '${context['target_message'] ?? ''}',
    '${context['attachment_manifest_digest'] ?? ''}',
  ];
  if (fields.any((field) => field.contains('\u0000'))) {
    throw const FormatException('Encrypted message context is invalid.');
  }
  return Uint8List.fromList(utf8.encode(fields.join('\u0000')));
}

bool _sameContext(Map<String, Object?> left, Map<String, Object?> right) {
  const fields = <String>{
    'channel_ref',
    'group_id',
    'policy_generation',
    'epoch',
    'sender_device_id',
    'operation',
    'target_message',
    'attachment_manifest_digest',
  };
  return left.length == fields.length &&
      right.length == fields.length &&
      left.keys.toSet().containsAll(fields) &&
      right.keys.toSet().containsAll(fields) &&
      fields.every((field) => left[field] == right[field]);
}

final class DecryptedE2EEApplication {
  const DecryptedE2EEApplication({
    required this.content,
    required this.attachments,
  });

  final String content;
  final List<Map<String, Object?>> attachments;
}

final class MobileE2EEClient {
  MobileE2EEClient._(
    this.repository,
    this.store,
    this.accountRef,
    this.deviceId,
    this._credential,
    this._mls,
    Map<String, String> cache,
  ) : _messageCache = Map.of(cache);

  final KaedeRepository repository;
  final MobileE2EEStore store;
  final String accountRef;
  final String deviceId;
  final String _credential;
  final NativeMlsClient _mls;
  final Map<String, String> _messageCache;
  final Map<String, DecryptedE2EEApplication?> _processed = {};
  bool _closed = false;

  static Future<MobileE2EEClient> initialize(
    KaedeRepository repository,
    KaedeUser user, {
    MobileE2EEStore store = const MobileE2EEStore(),
  }) async {
    final accountRef = user.ref.wire;
    final existing = await store.load(accountRef);
    if (existing != null) {
      final devices = await repository.e2eeDevices();
      final listed = (devices['devices'] as List? ?? const [])
          .whereType<Map<Object?, Object?>>()
          .map((item) => Map<String, Object?>.from(item));
      final device =
          listed.where((item) => item['id'] == existing.deviceId).firstOrNull;
      if (device == null || device['revoked_at'] != null) {
        throw StateError(
            'This encryption device was revoked. Clear its local keys before registering again.');
      }
      final client = MobileE2EEClient._(
        repository,
        store,
        accountRef,
        existing.deviceId,
        existing.credential,
        NativeMlsClient.restore(_decode(existing.mlsState, 32 * 1024 * 1024)),
        existing.messageCache,
      );
      await client.replenishKeyPackages(
        (device['available_key_packages'] as num?)?.toInt() ?? 0,
      );
      return client;
    }

    final credential = Uint8List.fromList(utf8.encode(jsonEncode({
      'version': 1,
      'account': accountRef,
      'nonce': _base64url(_random(32)),
    })));
    final mls = NativeMlsClient.generate(credential);
    try {
      final identity = mls.publicIdentityKey();
      final digest = await Sha256().hash(credential);
      final challenge = await repository.e2eeDeviceChallenge(
        identityKey: _base64url(identity),
        credentialDigest: _base64url(digest.bytes),
      );
      final signingInput = _decode('${challenge['signing_input']}', 2048);
      final signature = mls.sign(signingInput);
      final registered = await repository.registerE2eeDevice(
        challengeId: '${challenge['challenge_id']}',
        identityKey: _base64url(identity),
        credential: _base64url(credential),
        signature: _base64url(signature),
        deviceName: '${Platform.operatingSystem} mobile',
        platform: Platform.isIOS ? 'ios' : 'android',
      );
      final client = MobileE2EEClient._(
        repository,
        store,
        accountRef,
        '${registered['id']}',
        _base64url(credential),
        mls,
        const {},
      );
      await client._persist();
      await client.replenishKeyPackages(0);
      return client;
    } catch (_) {
      mls.close();
      rethrow;
    } finally {
      credential.fillRange(0, credential.length, 0);
    }
  }

  Future<void> _persist() async {
    final state = _mls.exportState();
    try {
      final entries = _messageCache.entries.toList();
      final recent = entries.length > 2000
          ? entries.sublist(entries.length - 2000)
          : entries;
      await store.save(MobileE2EEState(
        accountRef: accountRef,
        deviceId: deviceId,
        credential: _credential,
        mlsState: _base64url(state),
        messageCache: Map.fromEntries(recent),
      ));
    } finally {
      state.fillRange(0, state.length, 0);
    }
  }

  Future<void> replenishKeyPackages(int available) async {
    final count = max(0, _keyPackageBatch - available);
    if (count == 0) return;
    final packages = List.generate(count, (_) => _mls.generateKeyPackage());
    final digests = <Uint8List>[];
    for (final package in packages) {
      digests.add(Uint8List.fromList((await Sha256().hash(package)).bytes));
    }
    final rawExpiry = DateTime.now().toUtc().add(const Duration(days: 28));
    final expiresAt = DateTime.utc(
      rawExpiry.year,
      rawExpiry.month,
      rawExpiry.day,
      rawExpiry.hour,
      rawExpiry.minute,
      rawExpiry.second,
      rawExpiry.millisecond,
    ).toIso8601String().replaceFirst(RegExp(r'Z$'), '+00:00');
    final fields = <List<int>>[
      utf8.encode('kaede-key-package-upload-v1'),
      utf8.encode(deviceId),
      utf8.encode(mlsSuite),
      utf8.encode(expiresAt),
      ...digests,
    ];
    final signingInput = <int>[];
    for (var index = 0; index < fields.length; index++) {
      if (index != 0) signingInput.add(0);
      signingInput.addAll(fields[index]);
    }
    final signature = _mls.sign(Uint8List.fromList(signingInput));
    try {
      await repository.uploadE2eeKeyPackages(
        deviceId,
        expiresAt: expiresAt,
        packages: packages.map(_base64url).toList(growable: false),
        signature: _base64url(signature),
      );
      await _persist();
    } finally {
      for (final bytes in [...packages, ...digests, signature]) {
        bytes.fillRange(0, bytes.length, 0);
      }
    }
  }

  Future<KaedeChannel> enableRoom(KaedeChannel channel) async {
    final proposal = await repository.proposeE2eeRoom(channel.ref, deviceId);
    return _activate(channel.ref, proposal);
  }

  Future<KaedeChannel> rekeyRoom(KaedeChannel channel) async {
    final proposal = await repository.proposeE2eeRekey(channel.ref, deviceId);
    return _activate(channel.ref, proposal);
  }

  Future<KaedeChannel> _activate(
    EntityRef channel,
    Map<String, Object?> proposal,
  ) async {
    final policy = Map<String, Object?>.from(
      proposal['policy']! as Map<Object?, Object?>,
    );
    final groupId = _decode('${policy['group_id']}', 128);
    _mls.createGroup(groupId);
    final packages = (proposal['key_packages'] as List? ?? const [])
        .whereType<Map<Object?, Object?>>()
        .where((item) => item['device_id'] != deviceId)
        .map((item) => _decode('${item['key_package']}', 32 * 1024))
        .toList();
    if (packages.isEmpty) {
      throw StateError('An encrypted room requires another active device.');
    }
    final pending = _mls.addMembers(groupId, packages);
    final updated = await repository.activateE2eeRoom(
      channel,
      deviceId: deviceId,
      generation: '${policy['generation']}',
      commit: _base64url(pending.commit),
      welcome: _base64url(pending.welcome),
      proposalId: proposal['proposal_id'] as String?,
    );
    _mls.mergePendingCommit(groupId);
    await _persist();
    return updated;
  }

  Future<Map<String, Object?>> encryptMessage(
    KaedeChannel channel,
    String content, {
    String operation = 'create',
    EntityRef? targetMessage,
    List<Map<String, Object?>> attachments = const [],
  }) async {
    _requireActive(channel);
    if (operation == 'edit' && targetMessage == null) {
      throw ArgumentError('Encrypted edits require a target message.');
    }
    final attachmentDigest = attachments.isEmpty
        ? null
        : _base64url(
            (await Sha256().hash(utf8.encode(jsonEncode(attachments)))).bytes,
          );
    final context = <String, Object?>{
      'channel_ref': channel.ref.wire,
      'group_id': channel.encryptionGroupId,
      'policy_generation': '${channel.encryptionPolicyGeneration}',
      'epoch': '${channel.encryptionEpoch}',
      'sender_device_id': deviceId,
      'operation': operation,
      'target_message': targetMessage?.wire,
      'attachment_manifest_digest': attachmentDigest,
    };
    final plaintext = jsonEncode({
      'version': 1,
      'kind': 'message',
      'content': content,
      'attachments': attachments,
      'context': context,
    });
    final groupId = _decode(channel.encryptionGroupId!, 128);
    final encoded = Uint8List.fromList(utf8.encode(plaintext));
    final ciphertext = _mls.encrypt(
      groupId,
      encoded,
      _messageContextBytes(context),
    );
    final envelope = <String, Object?>{
      'version': 2,
      'protocol': mlsProtocol,
      'suite': mlsSuite,
      'group_id': channel.encryptionGroupId,
      'policy_generation': '${channel.encryptionPolicyGeneration}',
      'epoch': '${channel.encryptionEpoch}',
      'sender_device_id': deviceId,
      'operation': operation,
      'ciphertext': _base64url(ciphertext),
      if (targetMessage != null) 'target_message': targetMessage.wire,
      if (attachmentDigest != null)
        'attachment_manifest_digest': attachmentDigest,
    };
    _messageCache['${envelope['ciphertext']}'] = plaintext;
    encoded.fillRange(0, encoded.length, 0);
    ciphertext.fillRange(0, ciphertext.length, 0);
    await _persist();
    return envelope;
  }

  Future<DecryptedE2EEApplication?> decryptMessage(
    KaedeChannel channel,
    KaedeMessage message,
  ) async {
    _requireEncrypted(channel);
    final envelope = message.e2ee;
    if (envelope == null || envelope['version'] != 2) return null;
    if (envelope['protocol'] != mlsProtocol ||
        envelope['suite'] != mlsSuite ||
        '${envelope['policy_generation']}' !=
            '${message.encryptionPolicyGeneration}' ||
        '${envelope['epoch']}' != '${message.encryptionEpoch}') {
      throw const FormatException(
          'Encrypted message context does not match this conversation.');
    }
    final ciphertextText = '${envelope['ciphertext']}';
    if (_processed.containsKey(ciphertextText)) {
      return _processed[ciphertextText];
    }
    final groupId = _decode('${envelope['group_id']}', 128);
    final ciphertext = _decode(ciphertextText, 64 * 1024);
    if (envelope['operation'] == 'welcome') {
      if (!_mls.hasGroup(groupId)) {
        _mls.joinGroup(ciphertext);
      }
      _processed[ciphertextText] = null;
      await _persist();
      return null;
    }
    if (!{'create', 'edit'}.contains(envelope['operation'])) {
      throw const FormatException('Encrypted message operation is invalid.');
    }
    final expectedContext = <String, Object?>{
      'channel_ref': channel.ref.wire,
      'group_id': '${envelope['group_id']}',
      'policy_generation': '${envelope['policy_generation']}',
      'epoch': '${envelope['epoch']}',
      'sender_device_id': '${envelope['sender_device_id']}',
      'operation': '${envelope['operation']}',
      'target_message': envelope['target_message'],
      'attachment_manifest_digest': envelope['attachment_manifest_digest'],
    };
    String? plaintext = _messageCache[ciphertextText];
    if (plaintext == null) {
      final processed = _mls.process(groupId, ciphertext);
      if (processed.kind != 'application' ||
          processed.application == null ||
          processed.aad == null ||
          processed.credential == null) {
        await _persist();
        return null;
      }
      final expectedAad = _messageContextBytes(expectedContext);
      if (!_constantTimeEquals(processed.aad!, expectedAad)) {
        throw const FormatException(
            'Encrypted message authenticated context was modified.');
      }
      final credential = Map<String, Object?>.from(
        jsonDecode(utf8.decode(processed.credential!, allowMalformed: false))
            as Map,
      );
      if (credential['version'] != 1 ||
          credential['account'] != message.authorRef.wire ||
          credential['nonce'] is! String ||
          !RegExp(r'^[A-Za-z0-9_-]{43}$')
              .hasMatch(credential['nonce']! as String)) {
        throw const FormatException(
            'Encrypted message sender identity does not match its author.');
      }
      plaintext = utf8.decode(processed.application!, allowMalformed: false);
    }
    final decoded = Map<String, Object?>.from(jsonDecode(plaintext) as Map);
    final rawAttachments = decoded['attachments'];
    final rawContext = decoded['context'];
    if (decoded['version'] != 1 ||
        decoded['kind'] != 'message' ||
        decoded['content'] is! String ||
        rawAttachments is! List ||
        rawContext is! Map ||
        !_sameContext(
          Map<String, Object?>.from(rawContext),
          expectedContext,
        )) {
      throw const FormatException('Encrypted message plaintext is invalid.');
    }
    final attachments = rawAttachments
        .whereType<Map<Object?, Object?>>()
        .map((item) => Map<String, Object?>.from(item))
        .toList(growable: false);
    if (envelope['attachment_manifest_digest'] case final String expected) {
      final actual = _base64url(
        (await Sha256().hash(utf8.encode(jsonEncode(attachments)))).bytes,
      );
      if (actual != expected) {
        throw const FormatException(
            'Encrypted attachment manifest was modified.');
      }
    } else if (attachments.isNotEmpty) {
      throw const FormatException(
          'Encrypted attachment manifest is unauthenticated.');
    }
    final result = DecryptedE2EEApplication(
      content: decoded['content']! as String,
      attachments: attachments,
    );
    _messageCache[ciphertextText] = plaintext;
    _processed[ciphertextText] = result;
    await _persist();
    return result;
  }

  Future<List<KaedeMessage>> decryptMessages(
    KaedeChannel channel,
    Iterable<KaedeMessage> messages,
  ) async {
    final result = <KaedeMessage>[];
    for (final message in messages) {
      if (message.e2ee == null) {
        result.add(message);
        continue;
      }
      try {
        final decrypted = await decryptMessage(channel, message);
        result.add(decrypted == null
            ? message
            : message.copyWith(
                content: decrypted.content,
                decryptedAttachments: decrypted.attachments,
              ));
      } on Object {
        result.add(message);
      }
    }
    return result;
  }

  Future<void> syncRoomState(KaedeChannel channel) async {
    _requireEncrypted(channel);
    final messages = await repository.messages(channel.ref, limit: 100);
    for (final message in messages.reversed) {
      if (message.e2ee == null) continue;
      try {
        await decryptMessage(channel, message);
      } on Object {
        // A new device cannot decrypt generations before its Welcome. Later
        // control messages still need to be processed independently.
      }
    }
  }

  Future<Uint8List> mediaKey(KaedeChannel channel, String context) async {
    _requireActive(channel);
    if (context.isEmpty || context.length > 256) {
      throw ArgumentError('Invalid encrypted media context.');
    }
    return _mls.exportEpochSecret(
      _decode(channel.encryptionGroupId!, 128),
      'kaede livekit v1',
      Uint8List.fromList(utf8.encode(context)),
      32,
    );
  }

  Future<String> safetyNumber(KaedeChannel channel) async {
    _requireEncrypted(channel);
    final roster = _mls.memberRoster(_decode(channel.encryptionGroupId!, 128));
    final digest = (await Sha256().hash(roster)).bytes.take(15);
    final digits =
        digest.map((value) => value.toString().padLeft(3, '0')).join();
    return RegExp(r'.{1,5}')
        .allMatches(digits)
        .map((match) => match.group(0))
        .join(' ');
  }

  Future<String> exportRecovery(String passphrase) async {
    final state = await store.load(accountRef);
    if (state == null) {
      throw StateError('No encryption state exists on this device.');
    }
    return store.exportRecovery(state, passphrase);
  }

  void close() {
    if (_closed) return;
    _closed = true;
    _mls.close();
  }
}

void _requireEncrypted(KaedeChannel channel) {
  if (channel.encryptionMode != 'e2ee' ||
      channel.encryptionProtocol != mlsProtocol ||
      channel.encryptionSuite != mlsSuite ||
      channel.encryptionGroupId == null ||
      channel.encryptionEpoch == null) {
    throw StateError('This encrypted conversation is not ready.');
  }
}

void _requireActive(KaedeChannel channel) {
  _requireEncrypted(channel);
  if (channel.encryptionState != 'active') {
    throw StateError(
        'Encrypted messaging is paused while participant keys are secured.');
  }
}

Uint8List _random(int length) {
  final random = Random.secure();
  return Uint8List.fromList(List.generate(length, (_) => random.nextInt(256)));
}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

Uint8List _decode(String value, int maximum) {
  final decoded = base64Url.decode(base64Url.normalize(value));
  if (decoded.isEmpty ||
      decoded.length > maximum ||
      _base64url(decoded) != value) {
    throw const FormatException('Invalid canonical base64url value.');
  }
  return Uint8List.fromList(decoded);
}

bool _constantTimeEquals(List<int> left, List<int> right) {
  if (left.length != right.length) return false;
  var difference = 0;
  for (var index = 0; index < left.length; index++) {
    difference |= left[index] ^ right[index];
  }
  return difference == 0;
}
