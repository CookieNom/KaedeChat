import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/native_mls.dart';
import 'package:kaede_mobile/src/e2ee/store.dart';

const mlsProtocol = 'mls10';
const mlsSuite = 'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519';
const _keyPackageBatch = 20;
const _vaultLeaseAttempts = 8;
const _controlLogPageSize = 25;
const _maximumControlLogPages = 256;
const _maximumControlCursors = 6400;
const _maximumPendingRoomOperations = 32;
const _maximumVaultDigestPages = 16384;

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

String _processedCacheKey(
  EntityRef messageRef,
  EntityRef channelRef,
  EntityRef authorRef,
  Map<String, Object?> envelope,
) {
  final fields = <String>[
    messageRef.wire,
    channelRef.wire,
    authorRef.wire,
    '${envelope['group_id']}',
    '${envelope['policy_generation']}',
    '${envelope['epoch']}',
    '${envelope['ciphertext']}',
  ];
  if (fields.any((field) => field.contains('\u0000'))) {
    throw const FormatException('Encrypted message context is invalid.');
  }
  return fields.join('\u0000');
}

/// Bind an authenticated MLS application operation to the server message
/// projection. Controls are accepted only through the durable control log.
void validateMobileE2EEMessageProjection(
  KaedeMessage message,
  Map<String, Object?> envelope,
) {
  if (message.editedAt == null) {
    if (envelope['operation'] != 'create' ||
        envelope.containsKey('target_message')) {
      throw const FormatException(
        'Encrypted create does not match this message projection.',
      );
    }
    return;
  }
  if (envelope['operation'] != 'edit' ||
      envelope['target_message'] != message.ref.wire) {
    throw const FormatException(
      'Encrypted edit does not target this message projection.',
    );
  }
}

final class DecryptedE2EEApplication {
  const DecryptedE2EEApplication({
    required this.content,
    required this.attachments,
  });

  final String content;
  final List<Map<String, Object?>> attachments;
}

final class MobileE2EEControlRecord {
  const MobileE2EEControlRecord({
    required this.ref,
    required this.channelRef,
    required this.authorRef,
    required this.policyGeneration,
    required this.epoch,
    required this.apply,
    required this.roomOperationId,
    required this.roomOperationDomain,
    required this.envelope,
  });

  final EntityRef ref;
  final EntityRef channelRef;
  final EntityRef authorRef;
  final String policyGeneration;
  final String epoch;
  final bool apply;
  final String roomOperationId;
  final Domain roomOperationDomain;
  final Map<String, Object?> envelope;
}

final class MobileE2EEControlPage {
  const MobileE2EEControlPage({
    required this.controls,
    required this.nextAfter,
  });

  final List<MobileE2EEControlRecord> controls;
  final EntityRef? nextAfter;
}

final class _PreparedKeyPackageBatch {
  const _PreparedKeyPackageBatch({
    required this.expiresAt,
    required this.packages,
    required this.digests,
    required this.signature,
  });

  final String expiresAt;
  final List<Uint8List> packages;
  final List<Uint8List> digests;
  final Uint8List signature;

  void clear() {
    for (final bytes in <Uint8List>[...packages, ...digests, signature]) {
      bytes.fillRange(0, bytes.length, 0);
    }
  }
}

int _compareRefs(EntityRef left, EntityRef right) {
  final id =
      BigInt.parse(left.id.value).compareTo(BigInt.parse(right.id.value));
  return id != 0 ? id : left.domain.value.compareTo(right.domain.value);
}

/// Computes the stable device reference used by the home server for one MLS
/// account identity. Keeping this derivation client-side makes an interrupted
/// vault-first enrollment safely retryable without publishing a second
/// identity.
Future<String> mobileE2eeDeviceId(
  String accountRef,
  List<int> identityKey,
) async {
  final parsedAccount = EntityRef.parse(accountRef);
  if (parsedAccount.wire != accountRef || identityKey.length != 32) {
    throw const FormatException('The encryption identity is invalid.');
  }
  final digest = await Sha256().hash(<int>[
    ...utf8.encode('$accountRef\u0000'),
    ...identityKey,
  ]);
  return 'ked_${_base64url(digest.bytes)}';
}

/// Strictly decodes one ascending control-log page and rejects cursors that
/// could make a malicious or broken server loop or skip backwards.
MobileE2EEControlPage parseMobileE2EEControlPage(
  Map<String, Object?> response, {
  EntityRef? after,
  required EntityRef channel,
}) {
  final rawControls = response['controls'];
  if (rawControls is! List || rawControls.length > _controlLogPageSize) {
    throw const FormatException('The encryption control log is invalid.');
  }
  final controls = <MobileE2EEControlRecord>[];
  var previous = after;
  for (final raw in rawControls) {
    if (raw is! Map<Object?, Object?>) {
      throw const FormatException('The encryption control record is invalid.');
    }
    final item = Map<String, Object?>.from(raw);
    final id = _requiredString(item, 'id');
    final originDomain = _requiredString(item, 'origin_domain');
    final channelId = _requiredString(item, 'channel_id');
    final channelDomain = _requiredString(item, 'channel_domain');
    final authorId = _requiredString(item, 'author_id');
    final authorDomain = _requiredString(item, 'author_domain');
    final recordRef = EntityRef(
      Snowflake(id),
      Domain(originDomain),
    );
    final channelRef = EntityRef(
      Snowflake(channelId),
      Domain(channelDomain),
    );
    final authorRef = EntityRef(
      Snowflake(authorId),
      Domain(authorDomain),
    );
    final policyGeneration = item['encryption_policy_generation'];
    final epoch = item['encryption_epoch'];
    final apply = item['apply'];
    final roomOperationId = item['room_operation_id'];
    final roomOperationDomain = item['room_operation_domain'];
    final envelope = item['e2ee'];
    if (recordRef.wire != '$id@$originDomain' ||
        channelRef.wire != '$channelId@$channelDomain' ||
        authorRef.wire != '$authorId@$authorDomain' ||
        channelRef != channel ||
        policyGeneration is! String ||
        !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(policyGeneration) ||
        epoch is! String ||
        !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(epoch) ||
        apply is! bool ||
        roomOperationId is! String ||
        !RegExp(r'^keo_[A-Za-z0-9_-]{43}$').hasMatch(roomOperationId) ||
        roomOperationDomain is! String ||
        Domain(roomOperationDomain).value != roomOperationDomain ||
        roomOperationDomain != originDomain ||
        envelope is! Map<Object?, Object?> ||
        (previous != null && _compareRefs(recordRef, previous) <= 0)) {
      throw const FormatException('The encryption control record is invalid.');
    }
    controls.add(MobileE2EEControlRecord(
      ref: recordRef,
      channelRef: channelRef,
      authorRef: authorRef,
      policyGeneration: policyGeneration,
      epoch: epoch,
      apply: apply,
      roomOperationId: roomOperationId,
      roomOperationDomain: Domain(roomOperationDomain),
      envelope: Map<String, Object?>.from(envelope),
    ));
    previous = recordRef;
  }
  final rawNext = response['next_after'];
  final next = rawNext == null
      ? null
      : rawNext is String
          ? EntityRef.parse(rawNext)
          : throw const FormatException(
              'The encryption control cursor is invalid.',
            );
  if (next != null &&
      (next.wire != rawNext ||
          (after != null && _compareRefs(next, after) <= 0) ||
          (controls.isNotEmpty && _compareRefs(next, controls.last.ref) < 0))) {
    throw const FormatException('The encryption control cursor is invalid.');
  }
  return MobileE2EEControlPage(
    controls: List<MobileE2EEControlRecord>.unmodifiable(controls),
    nextAfter: next,
  );
}

Map<String, Object?>? _optionalMap(Object? value) => value is Map
    ? Map<String, Object?>.from(value as Map<Object?, Object?>)
    : null;

Map<String, Object?> _requiredMap(
  Map<String, Object?> source,
  String field,
) {
  final value = _optionalMap(source[field]);
  if (value == null) {
    throw FormatException('The encryption vault is missing $field.');
  }
  return value;
}

String _requiredString(Map<String, Object?> source, String field) {
  final value = source[field];
  if (value is! String || value.isEmpty || value.length > 256) {
    throw FormatException('The encryption vault has an invalid $field.');
  }
  if (field == 'lease_token' &&
      !RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(value)) {
    throw const FormatException('The encryption vault lease is invalid.');
  }
  if (field == 'revision' &&
      !RegExp(r'^(0|[1-9][0-9]{0,18})$').hasMatch(value)) {
    throw const FormatException('The encryption vault revision is invalid.');
  }
  return value;
}

String _requiredVaultDigest(Map<String, Object?> source) {
  final value = _requiredString(source, 'digest');
  final decoded = _decode(value, 32);
  try {
    if (decoded.length != 32) {
      throw const FormatException('The encryption vault digest is invalid.');
    }
  } finally {
    decoded.fillRange(0, decoded.length, 0);
  }
  return value;
}

String _roomOperationId() {
  final bytes = _random(32);
  try {
    return 'keo_${_base64url(bytes)}';
  } finally {
    bytes.fillRange(0, bytes.length, 0);
  }
}

Map<String, Object?> _validateRoomProposal(
  Map<String, Object?> value,
  MobilePendingRoomOperation operation,
) {
  final policy = _optionalMap(value['policy']);
  final packages = value['key_packages'];
  final expectedMode = operation.kind == 'activate' ? 'plaintext' : 'e2ee';
  final expectedState = operation.kind == 'activate' ? 'proposed' : 'rekeying';
  if (value['operation_id'] != operation.operationId ||
      value['status'] != 'prepared' ||
      policy == null ||
      policy['mode'] != expectedMode ||
      policy['state'] != expectedState ||
      policy['protocol'] != mlsProtocol ||
      policy['suite'] != mlsSuite ||
      policy['epoch'] != null ||
      policy['generation'] is! String ||
      !RegExp(r'^[1-9][0-9]{0,18}$')
          .hasMatch(policy['generation']! as String) ||
      policy['group_id'] is! String ||
      (operation.policyGeneration != null &&
          policy['generation'] != operation.policyGeneration) ||
      (operation.groupId != null && policy['group_id'] != operation.groupId) ||
      packages is! List ||
      packages.isEmpty) {
    throw const FormatException(
      'The encrypted-room authority returned an invalid proposal.',
    );
  }
  final groupId = _decode(policy['group_id']! as String, 32);
  try {
    if (groupId.length != 32) {
      throw const FormatException(
        'The encrypted-room authority returned an invalid group identifier.',
      );
    }
  } finally {
    groupId.fillRange(0, groupId.length, 0);
  }
  return value;
}

KaedeChannel _validateCommittedRoomOperation(
  Map<String, Object?> value,
  MobilePendingRoomOperation operation,
) {
  final controls = value['controls'];
  if (value['operation_id'] != operation.operationId ||
      value['operation_status'] != 'committed' ||
      controls is! List ||
      controls.length != 2) {
    throw const FormatException(
      'The encrypted-room authority returned an invalid commit result.',
    );
  }
  final welcome = _optionalMap(controls[0]);
  final commit = _optionalMap(controls[1]);
  const controlFields = <String>{
    'id',
    'origin_domain',
    'operation',
    'apply',
  };
  if (welcome == null ||
      commit == null ||
      welcome.length != controlFields.length ||
      !welcome.keys.toSet().containsAll(controlFields) ||
      commit.length != controlFields.length ||
      !commit.keys.toSet().containsAll(controlFields) ||
      welcome['operation'] != 'welcome' ||
      welcome['apply'] != true ||
      commit['operation'] != 'commit' ||
      commit['apply'] != false) {
    throw const FormatException(
      'The encrypted-room authority returned an invalid commit result.',
    );
  }
  final expectedAuthority = EntityRef.parse(operation.channelRef).domain.value;
  if (welcome['id'] == commit['id'] ||
      welcome['origin_domain'] != expectedAuthority ||
      commit['origin_domain'] != expectedAuthority) {
    throw const FormatException(
      'The encrypted-room authority returned invalid control metadata.',
    );
  }
  for (final control in <Map<String, Object?>>[welcome, commit]) {
    final id = control['id'];
    final domain = control['origin_domain'];
    if (id is! String ||
        domain is! String ||
        EntityRef(Snowflake(id), Domain(domain)).wire != '$id@$domain') {
      throw const FormatException(
        'The encrypted-room authority returned an invalid control reference.',
      );
    }
  }
  final channel = KaedeChannel.fromJson(value);
  if (channel.ref.wire != operation.channelRef ||
      channel.encryptionMode != 'e2ee' ||
      channel.encryptionState != 'active' ||
      channel.encryptionProtocol != mlsProtocol ||
      channel.encryptionSuite != mlsSuite ||
      '${channel.encryptionPolicyGeneration}' != operation.policyGeneration ||
      channel.encryptionGroupId != operation.groupId ||
      channel.encryptionEpoch != 1) {
    throw const FormatException(
      'The encrypted-room authority committed a different room policy.',
    );
  }
  return channel;
}

Map<String, Object?> _validateRoomOperationStatus(
  Map<String, Object?> value,
  MobilePendingRoomOperation operation,
) {
  final status = value['status'];
  final expiresAt = value['expires_at'];
  final committedAt = value['committed_at'];
  final prepared = _optionalMap(value['prepared']);
  final committed = _optionalMap(value['committed']);
  if (value['operation_id'] != operation.operationId ||
      value['kind'] != operation.kind ||
      !{'claiming', 'prepared', 'committed', 'failed'}.contains(status) ||
      expiresAt is! String ||
      DateTime.tryParse(expiresAt) == null ||
      (committedAt != null &&
          (committedAt is! String || DateTime.tryParse(committedAt) == null)) ||
      (value['prepared'] != null && prepared == null) ||
      (value['committed'] != null && committed == null) ||
      (status == 'prepared' && (prepared == null || committed != null)) ||
      (status == 'committed' && (prepared == null || committed == null))) {
    throw const FormatException(
      'The encrypted-room authority returned an invalid recovery status.',
    );
  }
  if (prepared != null) _validateRoomProposal(prepared, operation);
  return value;
}

List<Map<String, Object?>> _deviceList(Map<String, Object?> response) =>
    (response['devices'] as List? ?? const <Object?>[])
        .whereType<Map<Object?, Object?>>()
        .map((item) => Map<String, Object?>.from(item))
        .toList(growable: false);

Future<Map<String, Object?>> _acquireVaultLease(
  KaedeRepository repository,
) async {
  for (var attempt = 0; attempt < _vaultLeaseAttempts; attempt++) {
    try {
      return await repository.acquireE2eeVaultLease();
    } on KaedeException catch (error) {
      if (error.code != 'E2EE_ACCOUNT_VAULT_BUSY' ||
          attempt == _vaultLeaseAttempts - 1) {
        rethrow;
      }
      await Future<void>.delayed(
        Duration(
          milliseconds: 150 + attempt * 100 + Random.secure().nextInt(100),
        ),
      );
    }
  }
  throw StateError('The encryption vault is busy. Try again.');
}

bool _sameVaultEnvelope(
  Map<String, Object?> left,
  Map<String, Object?> right,
) =>
    left['version'] == right['version'] &&
    left['cipher'] == right['cipher'] &&
    left['sequence'] == right['sequence'] &&
    left['nonce'] == right['nonce'] &&
    left['ciphertext'] == right['ciphertext'];

String _nextRevision(String revision) =>
    (BigInt.parse(revision) + BigInt.one).toString();

Future<Map<String, Object?>> _verifiedVaultRecord(
  Map<String, Object?> record,
) async {
  final revision = _requiredString(record, 'revision');
  final envelope = _requiredMap(record, 'envelope');
  final actualDigest = await mobileAccountVaultDigest(
    revision: revision,
    envelope: envelope,
  );
  final claimedDigest = _decode(_requiredVaultDigest(record), 32);
  try {
    if (!_constantTimeEquals(actualDigest, claimedDigest)) {
      throw const FormatException('The encryption vault digest is invalid.');
    }
    return record;
  } finally {
    actualDigest.fillRange(0, actualDigest.length, 0);
    claimedDigest.fillRange(0, claimedDigest.length, 0);
  }
}

/// Exact SHA-256 digest bytes shared with the backend and web vault protocol.
/// The caller owns and must clear the returned buffer after use.
Future<Uint8List> mobileAccountVaultDigest({
  required String revision,
  required Map<String, Object?> envelope,
}) async {
  if (!RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(revision)) {
    throw const FormatException('The encryption vault revision is invalid.');
  }
  final revisionValue = BigInt.parse(revision);
  if (revisionValue <= BigInt.zero ||
      revisionValue > BigInt.from(0x7fffffffffffffff)) {
    throw const FormatException('The encryption vault revision is invalid.');
  }
  const envelopeFields = <String>{
    'version',
    'cipher',
    'sequence',
    'nonce',
    'ciphertext',
  };
  if (envelope.length != envelopeFields.length ||
      !envelope.keys.toSet().containsAll(envelopeFields) ||
      envelope['version'] != 2 ||
      envelope['cipher'] != 'AES-256-GCM' ||
      envelope['sequence'] != revision ||
      envelope['nonce'] is! String ||
      envelope['ciphertext'] is! String) {
    throw const FormatException('The encryption vault envelope is invalid.');
  }
  final nonce = _decode(envelope['nonce']! as String, 12);
  final ciphertext = _decode(
    envelope['ciphertext']! as String,
    32 * 1024 * 1024 + 16,
  );
  Uint8List? digestInput;
  try {
    if (nonce.length != 12 || ciphertext.length < 17) {
      throw const FormatException('The encryption vault envelope is invalid.');
    }
    final label = utf8.encode('kaede-account-vault-envelope-v2\u0000');
    digestInput = Uint8List(
      label.length + 2 + 8 + nonce.length + ciphertext.length,
    );
    var offset = 0;
    digestInput.setRange(offset, offset + label.length, label);
    offset += label.length;
    digestInput[offset++] = 0;
    digestInput[offset++] = 2;
    final revisionBytes = ByteData(8)
      ..setUint64(0, revisionValue.toInt(), Endian.big);
    final revisionList = revisionBytes.buffer.asUint8List();
    digestInput.setRange(offset, offset + 8, revisionList);
    offset += 8;
    digestInput.setRange(offset, offset + nonce.length, nonce);
    offset += nonce.length;
    digestInput.setRange(offset, offset + ciphertext.length, ciphertext);
    return Uint8List.fromList((await Sha256().hash(digestInput)).bytes);
  } finally {
    nonce.fillRange(0, nonce.length, 0);
    ciphertext.fillRange(0, ciphertext.length, 0);
    digestInput?.fillRange(0, digestInput.length, 0);
  }
}

/// Extend the authenticated compact vault ancestry chain.
///
/// R_n = SHA256(label || R_(n-1) || u64BE(n) || D_n). The caller owns and
/// must clear the returned buffer.
Future<Uint8List> mobileAccountVaultChainRoot({
  required List<int> parentChain,
  required String revision,
  required List<int> digest,
}) async {
  if (parentChain.length != 32 || digest.length != 32) {
    throw const FormatException('The encryption vault chain is invalid.');
  }
  if (!RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(revision)) {
    throw const FormatException('The encryption vault revision is invalid.');
  }
  final revisionValue = BigInt.parse(revision);
  if (revisionValue <= BigInt.zero ||
      revisionValue > BigInt.from(0x7fffffffffffffff)) {
    throw const FormatException('The encryption vault revision is invalid.');
  }
  final label = utf8.encode('kaede-account-vault-chain-v2\u0000');
  final input = Uint8List(label.length + 32 + 8 + 32);
  try {
    var offset = 0;
    input.setRange(offset, offset + label.length, label);
    offset += label.length;
    input.setRange(offset, offset + 32, parentChain);
    offset += 32;
    final revisionBytes = ByteData(8)
      ..setUint64(0, revisionValue.toInt(), Endian.big);
    input.setRange(offset, offset + 8, revisionBytes.buffer.asUint8List());
    offset += 8;
    input.setRange(offset, offset + 32, digest);
    return Uint8List.fromList((await Sha256().hash(input)).bytes);
  } finally {
    input.fillRange(0, input.length, 0);
  }
}

MobileVaultCheckpoint? _localVaultCheckpoint(MobileE2EEState? state) {
  final revision = state?.confirmedVaultRevision;
  final digest = state?.confirmedVaultDigest;
  final chainRoot = state?.confirmedVaultChainRoot;
  if (revision == null && digest == null && chainRoot == null) return null;
  if (state == null ||
      revision == null ||
      digest == null ||
      chainRoot == null) {
    throw StateError('The local encryption vault high-water mark is invalid.');
  }
  return MobileVaultCheckpoint(
    accountRef: state.accountRef,
    revision: revision,
    digest: digest,
    chainRoot: chainRoot,
  );
}

MobileVaultCheckpoint? _strongestVaultCheckpoint(
  MobileE2EEState? localState,
  MobileVaultCheckpoint? persisted,
) =>
    _strongestCheckpoints(_localVaultCheckpoint(localState), persisted);

MobileVaultCheckpoint? _strongestCheckpoints(
  MobileVaultCheckpoint? left,
  MobileVaultCheckpoint? right,
) {
  if (left == null) return right;
  if (right == null) return left;
  if (left.accountRef != right.accountRef) {
    throw StateError('The encryption vault checkpoint is invalid.');
  }
  final comparison = BigInt.parse(left.revision).compareTo(
    BigInt.parse(right.revision),
  );
  if (comparison == 0 &&
      (left.digest != right.digest || left.chainRoot != right.chainRoot)) {
    throw StateError('The encryption vault checkpoints conflict.');
  }
  return comparison > 0 ? left : right;
}

Future<Map<String, Object?>?> _verifyVaultHighWater(
  MobileE2EEState? localState,
  MobileVaultCheckpoint? persistedCheckpoint,
  Map<String, Object?>? remoteVault,
) async {
  final verified =
      remoteVault == null ? null : await _verifiedVaultRecord(remoteVault);
  final checkpoint = _strongestVaultCheckpoint(localState, persistedCheckpoint);
  if (checkpoint == null) return verified;
  if (verified == null) {
    throw StateError(
      'The server returned an older or missing encryption vault. Encrypted changes are paused to prevent rollback.',
    );
  }
  final remoteRevision = _requiredString(verified, 'revision');
  final comparison = BigInt.parse(remoteRevision).compareTo(
    BigInt.parse(checkpoint.revision),
  );
  if (comparison < 0 ||
      (comparison == 0 &&
          _requiredVaultDigest(verified) != checkpoint.digest)) {
    throw StateError(
      'The server returned an older or conflicting encryption vault. Encrypted changes are paused to prevent rollback.',
    );
  }
  return verified;
}

final class _OpenedVault {
  const _OpenedVault(this.state, this.checkpoint);

  final MobileE2EEState state;
  final MobileVaultCheckpoint checkpoint;
}

Future<_OpenedVault> _openVerifiedVaultAncestry({
  required KaedeRepository repository,
  required MobileE2EEStore store,
  required String accountRef,
  required SecretKey vaultKey,
  required Map<String, Object?> vault,
  required MobileVaultCheckpoint? checkpoint,
}) async {
  final verified = await _verifiedVaultRecord(vault);
  final targetRevision = _requiredString(verified, 'revision');
  final targetDigest = _requiredVaultDigest(verified);
  final opened = await store.openAccountVault(
    accountRef,
    vaultKey,
    _requiredMap(verified, 'envelope'),
  );
  if (checkpoint != null && checkpoint.accountRef != accountRef) {
    throw StateError('The encryption vault checkpoint is invalid.');
  }
  if (checkpoint != null) {
    final comparison = BigInt.parse(targetRevision).compareTo(
      BigInt.parse(checkpoint.revision),
    );
    if (comparison < 0 ||
        (comparison == 0 && targetDigest != checkpoint.digest)) {
      throw StateError(
        'The server returned an older or conflicting encryption vault. Encrypted changes are paused to prevent rollback.',
      );
    }
    if (comparison == 0) {
      return _OpenedVault(opened, checkpoint);
    }
  }

  var after = checkpoint?.revision ?? '0';
  var chain = _decode(checkpoint?.chainRoot ?? mobileZeroVaultChain, 32);
  final expectedParent = _decode(opened.vaultParentChain, 32);
  try {
    if (chain.length != 32 || expectedParent.length != 32) {
      throw const FormatException('The encryption vault chain is invalid.');
    }
    var reachedTarget = false;
    for (var pageIndex = 0; pageIndex < _maximumVaultDigestPages; pageIndex++) {
      final response = await repository.e2eeVaultDigests(after: after);
      const responseFields = <String>{'digests', 'next_after'};
      final rawDigests = response['digests'];
      final nextAfter = response['next_after'];
      if (response.length != responseFields.length ||
          !response.keys.toSet().containsAll(responseFields) ||
          rawDigests is! List ||
          rawDigests.length > 256 ||
          (nextAfter != null && nextAfter is! String)) {
        throw const FormatException(
          'The encryption vault ancestry response is invalid.',
        );
      }
      var expectedRevision = BigInt.parse(after) + BigInt.one;
      String? lastRevision;
      for (final raw in rawDigests) {
        if (raw is! Map<Object?, Object?>) {
          throw const FormatException(
            'The encryption vault ancestry response is invalid.',
          );
        }
        final item = Map<String, Object?>.from(raw);
        if (item.length != 2 ||
            !item.containsKey('revision') ||
            !item.containsKey('digest')) {
          throw const FormatException(
            'The encryption vault ancestry response is invalid.',
          );
        }
        final revision = _requiredString(item, 'revision');
        final digestText = _requiredVaultDigest(item);
        if (revision != expectedRevision.toString() ||
            BigInt.parse(revision) > BigInt.parse(targetRevision)) {
          throw const FormatException(
            'The encryption vault ancestry is not consecutive.',
          );
        }
        final digest = _decode(digestText, 32);
        try {
          if (revision == targetRevision) {
            if (digestText != targetDigest ||
                !_constantTimeEquals(chain, expectedParent)) {
              throw StateError(
                'The encrypted account vault does not descend from the trusted local checkpoint.',
              );
            }
            reachedTarget = true;
          }
          final nextChain = await mobileAccountVaultChainRoot(
            parentChain: chain,
            revision: revision,
            digest: digest,
          );
          chain.fillRange(0, chain.length, 0);
          chain = nextChain;
        } finally {
          digest.fillRange(0, digest.length, 0);
        }
        lastRevision = revision;
        expectedRevision += BigInt.one;
      }
      if (reachedTarget) {
        if (nextAfter != null) {
          throw const FormatException(
            'The encryption vault ancestry extends beyond the current vault.',
          );
        }
        final result = MobileVaultCheckpoint(
          accountRef: accountRef,
          revision: targetRevision,
          digest: targetDigest,
          chainRoot: _base64url(chain),
        );
        return _OpenedVault(opened, result);
      }
      if (rawDigests.isEmpty ||
          nextAfter == null ||
          nextAfter != lastRevision) {
        throw StateError(
          'The server did not provide a complete encryption vault ancestry.',
        );
      }
      after = nextAfter as String;
    }
    throw StateError('The encryption vault ancestry is too long to verify.');
  } finally {
    chain.fillRange(0, chain.length, 0);
    expectedParent.fillRange(0, expectedParent.length, 0);
  }
}

/// Reject a rollback or lateral same-revision substitution before replacing
/// live native MLS state. Public for cross-client protocol tests.
Future<void> validateMobileVaultRestoreHighWater({
  required String currentRevision,
  required String? currentDigest,
  required Map<String, Object?> candidate,
}) async {
  final verified = await _verifiedVaultRecord(candidate);
  if (currentRevision == '0') {
    if (currentDigest != null) {
      throw StateError('The encryption vault high-water mark is invalid.');
    }
    return;
  }
  if (currentDigest == null) {
    throw StateError('The encryption vault high-water mark is invalid.');
  }
  final candidateRevision = _requiredString(verified, 'revision');
  final comparison = BigInt.parse(candidateRevision).compareTo(
    BigInt.parse(currentRevision),
  );
  if (comparison < 0 ||
      (comparison == 0 && _requiredVaultDigest(verified) != currentDigest)) {
    throw StateError(
      'The server returned an older or conflicting encryption vault. Encrypted changes are paused to prevent rollback.',
    );
  }
}

enum MobileVaultJournalDisposition { none, confirmed, replay, conflict }

/// Classifies a locally journaled account-vault write without ever allowing a
/// stale local snapshot to overwrite a newer remote revision.
///
/// This is public only so the cross-client protocol invariant can be tested
/// without a native MLS runtime.
MobileVaultJournalDisposition classifyMobileVaultJournal(
  MobileE2EEState? localState,
  Map<String, Object?>? remoteVault,
) {
  final baseRevision = localState?.pendingVaultBaseRevision;
  final envelope = localState?.pendingVaultEnvelope;
  if (localState == null || baseRevision == null || envelope == null) {
    return MobileVaultJournalDisposition.none;
  }
  if (remoteVault != null &&
      _requiredString(remoteVault, 'revision') == _nextRevision(baseRevision) &&
      _sameVaultEnvelope(_requiredMap(remoteVault, 'envelope'), envelope)) {
    return MobileVaultJournalDisposition.confirmed;
  }
  final remoteRevision =
      remoteVault == null ? '0' : _requiredString(remoteVault, 'revision');
  return remoteRevision == baseRevision
      ? MobileVaultJournalDisposition.replay
      : MobileVaultJournalDisposition.conflict;
}

Future<Map<String, Object?>> _writePendingVault(
  KaedeRepository repository, {
  required String leaseToken,
  required String baseRevision,
  required Map<String, Object?> envelope,
}) async {
  for (var attempt = 0; attempt < 3; attempt++) {
    try {
      final response = await repository.updateE2eeVault(
        leaseToken: leaseToken,
        expectedRevision: baseRevision,
        envelope: envelope,
      );
      final written = _requiredMap(response, 'vault');
      await _verifiedVaultRecord(written);
      if (_requiredString(written, 'revision') != _nextRevision(baseRevision) ||
          !_sameVaultEnvelope(_requiredMap(written, 'envelope'), envelope)) {
        throw StateError(
          'The server committed a different encryption vault update.',
        );
      }
      return written;
    } on Object {
      try {
        final current = _optionalMap((await repository.e2eeVault())['vault']);
        if (current != null) {
          await _verifiedVaultRecord(current);
          if (_requiredString(current, 'revision') ==
                  _nextRevision(baseRevision) &&
              _sameVaultEnvelope(
                _requiredMap(current, 'envelope'),
                envelope,
              )) {
            // The CAS write committed and only its response was lost.
            return current;
          }
        }
        final currentRevision =
            current == null ? '0' : _requiredString(current, 'revision');
        if (currentRevision != baseRevision) {
          throw StateError(
            'The encryption vault changed while a local update was pending. Sign in again before changing encrypted state.',
          );
        }
      } on StateError {
        rethrow;
      } on Object {
        // The exact-revision CAS makes a bounded retry safe. If the first PUT
        // committed, the next conflict is reconciled by the read above.
      }
      if (attempt == 2) rethrow;
      await Future<void>.delayed(Duration(milliseconds: 150 * (attempt + 1)));
    }
  }
  throw StateError('Could not save the encrypted account vault.');
}

Future<Map<String, Object?>?> _reconcilePendingVault(
  KaedeRepository repository,
  MobileE2EEState? localState,
  MobileVaultCheckpoint? checkpoint,
  Map<String, Object?>? remoteVault,
  String leaseToken,
) async {
  remoteVault = await _verifyVaultHighWater(
    localState,
    checkpoint,
    remoteVault,
  );
  final baseRevision = localState?.pendingVaultBaseRevision;
  final envelope = localState?.pendingVaultEnvelope;
  if (localState == null || baseRevision == null || envelope == null) {
    return remoteVault;
  }
  switch (classifyMobileVaultJournal(localState, remoteVault)) {
    case MobileVaultJournalDisposition.none:
      return remoteVault;
    case MobileVaultJournalDisposition.confirmed:
      return remoteVault;
    case MobileVaultJournalDisposition.conflict:
      throw StateError(
        'A newer encrypted account vault conflicts with an unfinished local update. Encrypted changes are paused to avoid overwriting either state.',
      );
    case MobileVaultJournalDisposition.replay:
      break;
  }
  final written = await _writePendingVault(
    repository,
    leaseToken: leaseToken,
    baseRevision: baseRevision,
    envelope: envelope,
  );
  return written;
}

final class MobileE2EEClient {
  MobileE2EEClient._(
    this.repository,
    this.store,
    this.accountRef,
    this.deviceId,
    this._credential,
    this._mls,
    Map<String, MobileMessageCacheEntry> cache,
    Map<String, String> controlCursors,
    Map<String, MobilePendingRoomOperation> pendingRoomOperations,
    this._vaultKey,
    this._vaultRevision,
    this._vaultDigest,
    this._vaultChainRoot,
  )   : _messageCache = Map.of(cache),
        _controlCursors = Map.of(controlCursors),
        _pendingRoomOperations = Map.of(pendingRoomOperations);

  final KaedeRepository repository;
  final MobileE2EEStore store;
  final String accountRef;
  final String deviceId;
  final String _credential;
  NativeMlsClient _mls;
  final Map<String, MobileMessageCacheEntry> _messageCache;
  final Map<String, String> _controlCursors;
  final Map<String, MobilePendingRoomOperation> _pendingRoomOperations;
  final Map<String, KaedeChannel> _reconciledRoomChannels = {};
  final Map<String, DecryptedE2EEApplication?> _processed = {};
  final SecretKeyData _vaultKey;
  String _vaultRevision;
  String? _vaultDigest;
  String? _vaultChainRoot;
  Future<void> _operationTail = Future<void>.value();
  Future<void>? _closeFuture;
  bool _closed = false;

  static Future<MobileE2EEClient> initialize(
    KaedeRepository repository,
    KaedeUser user, {
    MobileE2EEStore store = const MobileE2EEStore(),
  }) async {
    final accountRef = user.ref.wire;
    final vaultKey = await repository.passwordVault.read(accountRef);
    if (vaultKey == null) {
      throw StateError(
        'Sign out and sign in again to unlock end-to-end encryption on this device.',
      );
    }
    Map<String, Object?>? lease;
    MobileE2EEClient? client;
    var availableKeyPackages = 0;
    try {
      lease = await _acquireVaultLease(repository);
      final leaseToken = _requiredString(lease, 'lease_token');
      final local = await store.load(accountRef);
      final persistedCheckpoint = await store.loadCheckpoint(accountRef);
      final vault = await _reconcilePendingVault(
        repository,
        local,
        persistedCheckpoint,
        _optionalMap(lease['vault']),
        leaseToken,
      );
      final ancestry = vault == null
          ? null
          : await _openVerifiedVaultAncestry(
              repository: repository,
              store: store,
              accountRef: accountRef,
              vaultKey: vaultKey,
              vault: vault,
              checkpoint: _strongestVaultCheckpoint(
                local,
                persistedCheckpoint,
              ),
            );
      final existing = ancestry == null
          ? local
          : ancestry.state.withConfirmedVault(
              ancestry.checkpoint.revision,
              ancestry.checkpoint.digest,
              ancestry.checkpoint.chainRoot,
            );
      if (existing != null) {
        await store.save(existing);
        if (ancestry != null) {
          await store.saveCheckpoint(ancestry.checkpoint);
        }
        final devices = await repository.e2eeDevices();
        var device = _deviceList(devices)
            .where((item) => item['id'] == existing.deviceId)
            .firstOrNull;
        if (device != null && device['revoked_at'] != null) {
          // A recovery import first performs authenticated /e2ee/reset, then
          // reseeds the same deterministic private identity at sequence 1.
          // Its fresh proof-of-possession challenge re-enrolls this row.
          device = null;
        }
        final encodedState = _decode(existing.mlsState, 32 * 1024 * 1024);
        late final NativeMlsClient mls;
        try {
          mls = NativeMlsClient.restore(encodedState);
        } finally {
          encodedState.fillRange(0, encodedState.length, 0);
        }
        try {
          final identity = mls.publicIdentityKey();
          try {
            final expectedDeviceId = await mobileE2eeDeviceId(
              accountRef,
              identity,
            );
            if (existing.deviceId != expectedDeviceId) {
              throw StateError(
                'The portable encryption identity has an invalid device reference.',
              );
            }
            if (device != null &&
                (device['user_id'] != user.ref.id.value ||
                    device['user_domain'] != user.ref.domain.value ||
                    device['identity_key'] != _base64url(identity) ||
                    device['credential'] != existing.credential)) {
              throw StateError(
                'The server returned different encryption identity metadata.',
              );
            }
          } finally {
            identity.fillRange(0, identity.length, 0);
          }
        } on Object {
          mls.close();
          rethrow;
        }
        client = MobileE2EEClient._(
          repository,
          store,
          accountRef,
          existing.deviceId,
          existing.credential,
          mls,
          existing.messageCache,
          existing.controlCursors,
          existing.pendingRoomOperations,
          vaultKey,
          vault == null ? '0' : _requiredString(vault, 'revision'),
          vault == null ? null : _requiredVaultDigest(vault),
          ancestry?.checkpoint.chainRoot,
        );
        if (vault == null) await client._persist(leaseToken);
        if (device == null) {
          // A prior client committed this deterministic private identity to
          // the vault but did not receive/finish the device registration. The
          // same identity can be safely published again without forking MLS.
          await client._registerIdentity();
        } else {
          availableKeyPackages =
              (device['available_key_packages'] as num?)?.toInt() ?? 0;
        }
      } else {
        final devices = await repository.e2eeDevices();
        if (_deviceList(devices)
            .any((device) => device['revoked_at'] == null)) {
          throw StateError(
            'Your encrypted account vault needs recovery. Restore a recovery backup or explicitly start a new encryption identity.',
          );
        }
        final credential = Uint8List.fromList(utf8.encode(jsonEncode({
          'version': 1,
          'account': accountRef,
          'nonce': _base64url(_random(32)),
        })));
        final mls = NativeMlsClient.generate(credential);
        try {
          final identity = mls.publicIdentityKey();
          try {
            final deviceId = await mobileE2eeDeviceId(accountRef, identity);
            client = MobileE2EEClient._(
              repository,
              store,
              accountRef,
              deviceId,
              _base64url(credential),
              mls,
              const {},
              const {},
              const {},
              vaultKey,
              '0',
              null,
              null,
            );
            // The server must never advertise a key package or device whose
            // private state exists only in one process. Commit the portable
            // identity first; publication is deterministic and retryable.
            await client._persist(leaseToken);
            await client._registerIdentity();
          } finally {
            identity.fillRange(0, identity.length, 0);
          }
        } catch (_) {
          if (client == null) mls.close();
          rethrow;
        } finally {
          credential.fillRange(0, credential.length, 0);
        }
      }
      await client._reconcileRoomOperationsUnlocked(leaseToken);
    } on Object {
      if (client == null) {
        vaultKey.destroy();
      } else {
        await client.close();
      }
      rethrow;
    } finally {
      final leaseToken = lease?['lease_token'];
      if (leaseToken is String) {
        await repository.releaseE2eeVaultLease(leaseToken);
      }
    }
    try {
      await client.replenishKeyPackages(availableKeyPackages);
      return client;
    } on Object {
      await client.close();
      rethrow;
    }
  }

  Future<Map<String, Object?>> _persist(String leaseToken) async {
    final state = _mls.exportState();
    try {
      trimMobileMessageCache(_messageCache);
      final cursorEntries = _controlCursors.entries.toList();
      final recentCursors = cursorEntries.length > _maximumControlCursors
          ? cursorEntries.sublist(
              cursorEntries.length - _maximumControlCursors,
            )
          : cursorEntries;
      _controlCursors
        ..clear()
        ..addEntries(recentCursors);
      final pendingEntries = _pendingRoomOperations.entries.toList();
      final recentPending =
          pendingEntries.length > _maximumPendingRoomOperations
              ? pendingEntries.sublist(
                  pendingEntries.length - _maximumPendingRoomOperations,
                )
              : pendingEntries;
      _pendingRoomOperations
        ..clear()
        ..addEntries(recentPending);
      final encodedState = _base64url(state);
      if ((_vaultRevision == '0') !=
          (_vaultDigest == null && _vaultChainRoot == null)) {
        throw StateError('The encryption vault high-water mark is invalid.');
      }
      if (_vaultRevision != '0' &&
          (_vaultDigest == null || _vaultChainRoot == null)) {
        throw StateError('The encryption vault high-water mark is invalid.');
      }
      MobileE2EEState recordWith(
        Map<String, MobileMessageCacheEntry> cache,
      ) =>
          MobileE2EEState(
            accountRef: accountRef,
            deviceId: deviceId,
            credential: _credential,
            mlsState: encodedState,
            vaultSequence: _nextRevision(_vaultRevision),
            vaultParentChain: _vaultChainRoot ?? mobileZeroVaultChain,
            messageCache: cache,
            controlCursors: Map<String, String>.from(_controlCursors),
            pendingRoomOperations: Map<String, MobilePendingRoomOperation>.from(
              _pendingRoomOperations,
            ),
            confirmedVaultRevision:
                _vaultRevision == '0' ? null : _vaultRevision,
            confirmedVaultDigest: _vaultRevision == '0' ? null : _vaultDigest,
            confirmedVaultChainRoot:
                _vaultRevision == '0' ? null : _vaultChainRoot,
          );

      final baseRecord = recordWith(const <String, MobileMessageCacheEntry>{});
      final baseBytes = mobilePortableStateSerializedBytes(baseRecord);
      if (baseBytes > maximumMobileOutboundVaultBytes) {
        throw StateError(
          'Encryption state is too large even after its plaintext cache is removed.',
        );
      }
      final dynamicCacheBudget = min(
        maximumMobileMessageCacheBytes,
        maximumMobileOutboundVaultBytes - baseBytes + 2,
      );
      trimMobileMessageCache(
        _messageCache,
        maximumBytes: dynamicCacheBudget,
      );
      final record = recordWith(
        Map<String, MobileMessageCacheEntry>.from(_messageCache),
      );
      if (mobilePortableStateSerializedBytes(record) >
          maximumMobileOutboundVaultBytes) {
        throw StateError('Encryption state is too large.');
      }
      final envelope = await store.sealAccountVault(record, _vaultKey);
      await store.save(
        record.withPendingVaultWrite(_vaultRevision, envelope),
      );
      final written = await _writePendingVault(
        repository,
        leaseToken: leaseToken,
        baseRevision: _vaultRevision,
        envelope: envelope,
      );
      _vaultRevision = _requiredString(written, 'revision');
      _vaultDigest = _requiredVaultDigest(written);
      final parentChain = _decode(record.vaultParentChain, 32);
      final digestBytes = _decode(_vaultDigest!, 32);
      Uint8List? chainRoot;
      try {
        chainRoot = await mobileAccountVaultChainRoot(
          parentChain: parentChain,
          revision: _vaultRevision,
          digest: digestBytes,
        );
        _vaultChainRoot = _base64url(chainRoot);
      } finally {
        parentChain.fillRange(0, parentChain.length, 0);
        digestBytes.fillRange(0, digestBytes.length, 0);
        chainRoot?.fillRange(0, chainRoot.length, 0);
      }
      final confirmed = record.confirmed(
        _vaultRevision,
        _vaultDigest!,
        _vaultChainRoot!,
      );
      await store.save(confirmed);
      await store.saveCheckpoint(MobileVaultCheckpoint(
        accountRef: accountRef,
        revision: _vaultRevision,
        digest: _vaultDigest!,
        chainRoot: _vaultChainRoot!,
      ));
      return written;
    } finally {
      state.fillRange(0, state.length, 0);
    }
  }

  Future<void> _registerIdentity() async {
    final identity = _mls.publicIdentityKey();
    final credential = _decode(_credential, 16 * 1024);
    Uint8List? credentialDigest;
    Uint8List? signingInput;
    Uint8List? signature;
    try {
      final expectedDeviceId = await mobileE2eeDeviceId(accountRef, identity);
      if (deviceId != expectedDeviceId) {
        throw StateError(
          'The portable encryption identity has an invalid device reference.',
        );
      }
      credentialDigest = Uint8List.fromList(
        (await Sha256().hash(credential)).bytes,
      );
      final challenge = await repository.e2eeDeviceChallenge(
        identityKey: _base64url(identity),
        credentialDigest: _base64url(credentialDigest),
      );
      final challengeId = challenge['challenge_id'];
      if (challengeId is! String ||
          !RegExp(r'^[A-Za-z0-9_-]{32,64}$').hasMatch(challengeId)) {
        throw const FormatException(
          'The encryption identity challenge is invalid.',
        );
      }
      signingInput = _decode('${challenge['signing_input']}', 2048);
      signature = _mls.sign(signingInput);
      final registered = await repository.registerE2eeDevice(
        challengeId: challengeId,
        identityKey: _base64url(identity),
        credential: _credential,
        signature: _base64url(signature),
        deviceName: 'Account encryption identity',
        platform: Platform.isIOS ? 'ios' : 'android',
      );
      if (registered['id'] != deviceId ||
          registered['identity_key'] != _base64url(identity) ||
          registered['credential'] != _credential ||
          registered['revoked_at'] != null) {
        throw StateError(
          'The server registered a different encryption identity.',
        );
      }
    } finally {
      identity.fillRange(0, identity.length, 0);
      credential.fillRange(0, credential.length, 0);
      credentialDigest?.fillRange(0, credentialDigest.length, 0);
      signingInput?.fillRange(0, signingInput.length, 0);
      signature?.fillRange(0, signature.length, 0);
    }
  }

  Future<void> _restoreVault(Map<String, Object?> vault) async {
    if ((_vaultRevision == '0' &&
            (_vaultDigest != null || _vaultChainRoot != null)) ||
        (_vaultRevision != '0' &&
            (_vaultDigest == null || _vaultChainRoot == null))) {
      throw StateError('The encryption vault high-water mark is invalid.');
    }
    await validateMobileVaultRestoreHighWater(
      currentRevision: _vaultRevision,
      currentDigest: _vaultDigest,
      candidate: vault,
    );
    final persistedCheckpoint = await store.loadCheckpoint(accountRef);
    final inMemoryCheckpoint = _vaultRevision == '0'
        ? null
        : MobileVaultCheckpoint(
            accountRef: accountRef,
            revision: _vaultRevision,
            digest: _vaultDigest!,
            chainRoot: _vaultChainRoot!,
          );
    final ancestry = await _openVerifiedVaultAncestry(
      repository: repository,
      store: store,
      accountRef: accountRef,
      vaultKey: _vaultKey,
      vault: vault,
      checkpoint: _strongestCheckpoints(
        inMemoryCheckpoint,
        persistedCheckpoint,
      ),
    );
    final state = ancestry.state.withConfirmedVault(
      ancestry.checkpoint.revision,
      ancestry.checkpoint.digest,
      ancestry.checkpoint.chainRoot,
    );
    if (state.deviceId != deviceId || state.credential != _credential) {
      throw StateError(
        'The encrypted account identity changed unexpectedly. Sign in again.',
      );
    }
    final encodedState = _decode(state.mlsState, 32 * 1024 * 1024);
    late final NativeMlsClient restored;
    try {
      restored = NativeMlsClient.restore(encodedState);
    } finally {
      encodedState.fillRange(0, encodedState.length, 0);
    }
    _mls.close();
    _mls = restored;
    _messageCache
      ..clear()
      ..addAll(state.messageCache);
    _controlCursors
      ..clear()
      ..addAll(state.controlCursors);
    _pendingRoomOperations
      ..clear()
      ..addAll(state.pendingRoomOperations);
    _processed.clear();
    _vaultRevision = ancestry.checkpoint.revision;
    _vaultDigest = ancestry.checkpoint.digest;
    _vaultChainRoot = ancestry.checkpoint.chainRoot;
    await store.save(state);
    await store.saveCheckpoint(ancestry.checkpoint);
  }

  Future<T> _synchronized<T>(
    Future<T> Function() operation, {
    bool persist = true,
    bool rollbackOnOperationError = true,
  }) =>
      _synchronizedWithLease(
        (_) => operation(),
        persist: persist,
        rollbackOnOperationError: rollbackOnOperationError,
      );

  Future<T> _synchronizedWithLease<T>(
    Future<T> Function(String leaseToken) operation, {
    bool persist = true,
    bool rollbackOnOperationError = true,
  }) async {
    if (_closed) throw StateError('The encryption client is closed.');
    final previous = _operationTail;
    final completed = Completer<void>();
    _operationTail = completed.future;
    await previous;
    try {
      final lease = await _acquireVaultLease(repository);
      final leaseToken = _requiredString(lease, 'lease_token');
      try {
        final vault = await _reconcilePendingVault(
          repository,
          await store.load(accountRef),
          await store.loadCheckpoint(accountRef),
          _optionalMap(lease['vault']),
          leaseToken,
        );
        if (vault == null) {
          throw StateError(
            'The encrypted account vault is missing. Sign in again to recover it.',
          );
        }
        await _restoreVault(vault);
        await _reconcileRoomOperationsUnlocked(leaseToken);
        final operationBaseVault =
            _vaultRevision == _requiredString(vault, 'revision')
                ? vault
                : _optionalMap((await repository.e2eeVault())['vault']);
        if (operationBaseVault == null ||
            _requiredString(operationBaseVault, 'revision') != _vaultRevision ||
            _requiredVaultDigest(operationBaseVault) != _vaultDigest) {
          throw StateError(
            'The encrypted account vault changed during synchronization.',
          );
        }
        late T result;
        try {
          result = await operation(leaseToken);
        } on Object {
          if (rollbackOnOperationError) {
            await _restoreVault(operationBaseVault);
          }
          rethrow;
        }
        // A failed write intentionally leaves the encrypted local pending
        // journal intact. The next lease can replay it only from this exact
        // base revision, or recognize an ambiguous successful response.
        if (persist) await _persist(leaseToken);
        return result;
      } finally {
        await repository.releaseE2eeVaultLease(leaseToken);
      }
    } finally {
      completed.complete();
    }
  }

  Future<void> replenishKeyPackages(int available) async {
    final count = max(0, _keyPackageBatch - available);
    if (count == 0) return;
    _PreparedKeyPackageBatch? batch;
    try {
      // Key-package generation mutates the MLS private key store. Seal that
      // state into the shared account vault before publishing packages, so a
      // different client can consume a package immediately without depending
      // on this device surviving a later vault write.
      await _synchronized(() async {
        batch = await _prepareKeyPackages(count);
      });
      final prepared = batch!;
      await repository.uploadE2eeKeyPackages(
        deviceId,
        expiresAt: prepared.expiresAt,
        packages: prepared.packages.map(_base64url).toList(growable: false),
        signature: _base64url(prepared.signature),
      );
    } finally {
      batch?.clear();
    }
  }

  Future<_PreparedKeyPackageBatch> _prepareKeyPackages(int count) async {
    final packages = List.generate(count, (_) => _mls.generateKeyPackage());
    final digests = <Uint8List>[];
    try {
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
      return _PreparedKeyPackageBatch(
        expiresAt: expiresAt,
        packages: packages,
        digests: digests,
        signature: _mls.sign(Uint8List.fromList(signingInput)),
      );
    } on Object {
      for (final bytes in <Uint8List>[...packages, ...digests]) {
        bytes.fillRange(0, bytes.length, 0);
      }
      rethrow;
    }
  }

  Future<KaedeChannel> enableRoom(KaedeChannel channel) async {
    return _synchronizedWithLease(
      (leaseToken) => _startRoomOperation(
        channel.ref,
        'activate',
        leaseToken,
      ),
      persist: false,
      rollbackOnOperationError: false,
    );
  }

  Future<KaedeChannel> rekeyRoom(KaedeChannel channel) async {
    return _synchronizedWithLease(
      (leaseToken) => _startRoomOperation(
        channel.ref,
        'rekey',
        leaseToken,
      ),
      persist: false,
      rollbackOnOperationError: false,
    );
  }

  Future<KaedeChannel> _startRoomOperation(
    EntityRef channel,
    String kind,
    String leaseToken,
  ) async {
    final reconciled = _reconciledRoomChannels.remove(channel.wire);
    if (reconciled != null) return reconciled;
    final existing = _pendingRoomOperations.values
        .where((candidate) => candidate.channelRef == channel.wire)
        .firstOrNull;
    if (existing != null) {
      if (existing.kind != kind) {
        throw StateError(
          'A different encrypted-room update is already being recovered.',
        );
      }
      return _continueRoomOperation(existing, leaseToken);
    }
    if (_pendingRoomOperations.length >= _maximumPendingRoomOperations) {
      throw StateError(
        'Too many encrypted-room updates are waiting for recovery.',
      );
    }
    final operation = MobilePendingRoomOperation(
      operationId: _roomOperationId(),
      channelRef: channel.wire,
      kind: kind,
      phase: 'proposing',
    );
    _pendingRoomOperations[operation.operationId] = operation;
    // Persist the idempotency key before an authority can claim any one-use
    // KeyPackages. A crash can then resume the exact same distributed claim.
    await _persist(leaseToken);
    return _continueRoomOperation(operation, leaseToken);
  }

  Future<KaedeChannel> _continueRoomOperation(
    MobilePendingRoomOperation operation,
    String leaseToken,
  ) async {
    if (operation.phase == 'activating') {
      return _activatePendingRoomOperation(operation, leaseToken);
    }
    final channel = EntityRef.parse(operation.channelRef);
    final response = operation.kind == 'activate'
        ? await repository.proposeE2eeRoom(
            channel,
            deviceId,
            operation.operationId,
          )
        : await repository.proposeE2eeRekey(
            channel,
            deviceId,
            operation.operationId,
          );
    final proposal = _validateRoomProposal(response, operation);
    return _prepareRoomOperation(operation, proposal, leaseToken);
  }

  Future<KaedeChannel> _prepareRoomOperation(
    MobilePendingRoomOperation operation,
    Map<String, Object?> proposal,
    String leaseToken,
  ) async {
    _validateRoomProposal(proposal, operation);
    final policy = _requiredMap(proposal, 'policy');
    final groupId = _decode(policy['group_id']! as String, 32);
    final packages = <Uint8List>[];
    Uint8List? commit;
    Uint8List? welcome;
    NativeMlsPendingCommit? pendingCommit;
    try {
      if (groupId.length != 32 || _mls.hasGroup(groupId)) {
        throw StateError(
          'The encrypted-room group conflicts with local encryption state.',
        );
      }
      final seenDevices = <String>{};
      final seenAccounts = <String>{};
      final seenPackages = <String>{};
      final seenIdentities = <String>{};
      for (final raw in proposal['key_packages']! as List) {
        final item = _optionalMap(raw);
        final claimedDevice = item?['device_id'];
        final keyPackage = item?['key_package'];
        final identityKey = item?['identity_key'];
        final credential = item?['credential'];
        final userId = item?['user_id'];
        final userDomain = item?['user_domain'];
        const packageFields = <String>{
          'user_id',
          'user_domain',
          'device_id',
          'identity_key',
          'credential',
          'key_package',
        };
        if (item == null ||
            item.length != packageFields.length ||
            !item.keys.toSet().containsAll(packageFields) ||
            claimedDevice is! String ||
            !RegExp(r'^ked_[A-Za-z0-9_-]{43}$').hasMatch(claimedDevice) ||
            claimedDevice == deviceId ||
            !seenDevices.add(claimedDevice) ||
            keyPackage is! String ||
            !seenPackages.add(keyPackage) ||
            identityKey is! String ||
            !seenIdentities.add(identityKey) ||
            credential is! String ||
            userId is! String ||
            userDomain is! String) {
          throw const FormatException(
            'The encrypted-room authority returned an invalid key package.',
          );
        }
        late final EntityRef claimedAccount;
        try {
          claimedAccount = EntityRef(Snowflake(userId), Domain(userDomain));
        } on FormatException {
          throw const FormatException(
            'A claimed key package has a non-canonical participant.',
          );
        }
        if (claimedAccount.wire != '$userId@$userDomain' ||
            !seenAccounts.add(claimedAccount.wire)) {
          throw const FormatException(
            'A claimed key package has a duplicate or non-canonical participant.',
          );
        }
        final packageBytes = _decode(keyPackage, 32 * 1024);
        packages.add(packageBytes);
        Uint8List? expectedIdentity;
        Uint8List? expectedCredential;
        Uint8List? credentialNonce;
        NativeMlsKeyPackageIdentity? inspected;
        try {
          expectedIdentity = _decode(identityKey, 32);
          expectedCredential = _decode(credential, 16 * 1024);
          inspected = _mls.inspectKeyPackage(packageBytes);
          final credentialPayload = Map<String, Object?>.from(
            jsonDecode(
              utf8.decode(expectedCredential, allowMalformed: false),
            ) as Map,
          );
          final expectedDeviceId = await mobileE2eeDeviceId(
            claimedAccount.wire,
            expectedIdentity,
          );
          final nonce = credentialPayload['nonce'];
          if (nonce is! String) {
            throw const FormatException(
              'A claimed key package does not authenticate its participant.',
            );
          }
          credentialNonce = _decode(nonce, 32);
          const credentialFields = <String>{'version', 'account', 'nonce'};
          if (credentialPayload.length != credentialFields.length ||
              !credentialPayload.keys.toSet().containsAll(credentialFields) ||
              expectedIdentity.length != 32 ||
              !_constantTimeEquals(
                inspected.signatureKey,
                expectedIdentity,
              ) ||
              !_constantTimeEquals(
                inspected.credential,
                expectedCredential,
              ) ||
              expectedDeviceId != claimedDevice ||
              credentialPayload['version'] != 1 ||
              credentialPayload['account'] != claimedAccount.wire ||
              credentialNonce.length != 32) {
            throw const FormatException(
              'A claimed key package does not authenticate its participant.',
            );
          }
        } finally {
          expectedIdentity?.fillRange(0, expectedIdentity.length, 0);
          expectedCredential?.fillRange(0, expectedCredential.length, 0);
          credentialNonce?.fillRange(0, credentialNonce.length, 0);
          inspected?.credential.fillRange(0, inspected.credential.length, 0);
          inspected?.signatureKey
              .fillRange(0, inspected.signatureKey.length, 0);
        }
      }
      if (packages.isEmpty) {
        throw StateError('An encrypted room requires another active device.');
      }
      _mls.createGroup(groupId);
      pendingCommit = _mls.addMembers(groupId, packages);
      commit = Uint8List.fromList(pendingCommit.commit);
      welcome = Uint8List.fromList(pendingCommit.welcome);
      _mls.mergePendingCommit(groupId);
      final activating = MobilePendingRoomOperation(
        operationId: operation.operationId,
        channelRef: operation.channelRef,
        kind: operation.kind,
        phase: 'activating',
        policyGeneration: policy['generation']! as String,
        groupId: policy['group_id']! as String,
        commit: _base64url(commit),
        welcome: _base64url(welcome),
      );
      _pendingRoomOperations[operation.operationId] = activating;
      // The merged private MLS state must be the durable account-vault state
      // to which the visible server policy is bound.
      await _persist(leaseToken);
      return _activatePendingRoomOperation(activating, leaseToken);
    } finally {
      for (final bytes in packages) {
        bytes.fillRange(0, bytes.length, 0);
      }
      for (final bytes in <Uint8List?>[
        commit,
        welcome,
        pendingCommit?.commit,
        pendingCommit?.welcome,
      ]) {
        bytes?.fillRange(0, bytes.length, 0);
      }
      groupId.fillRange(0, groupId.length, 0);
    }
  }

  Future<KaedeChannel> _activatePendingRoomOperation(
    MobilePendingRoomOperation operation,
    String leaseToken,
  ) async {
    final vaultDigest = _vaultDigest;
    if (operation.phase != 'activating' ||
        operation.policyGeneration == null ||
        operation.groupId == null ||
        operation.commit == null ||
        operation.welcome == null ||
        vaultDigest == null ||
        _vaultRevision == '0') {
      throw StateError('The encrypted-room recovery record is incomplete.');
    }
    final response = await repository.activateE2eeRoom(
      EntityRef.parse(operation.channelRef),
      operationId: operation.operationId,
      deviceId: deviceId,
      generation: operation.policyGeneration!,
      groupId: operation.groupId!,
      commit: operation.commit!,
      welcome: operation.welcome!,
      preparedVaultRevision: _vaultRevision,
      preparedVaultDigest: vaultDigest,
      vaultLeaseToken: leaseToken,
      rekey: operation.kind == 'rekey',
    );
    final channel = _validateCommittedRoomOperation(response, operation);
    _pendingRoomOperations.remove(operation.operationId);
    await _persist(leaseToken);
    return channel;
  }

  Future<Map<String, Object?>> _roomOperationStatus(
    MobilePendingRoomOperation operation,
  ) async {
    final response = await repository.e2eeRoomOperation(
      EntityRef.parse(operation.channelRef),
      operation.operationId,
    );
    return _validateRoomOperationStatus(response, operation);
  }

  Future<void> _reconcileRoomOperationsUnlocked(String leaseToken) async {
    for (final operation in _pendingRoomOperations.values.toList()) {
      Map<String, Object?> status;
      try {
        status = await _roomOperationStatus(operation);
      } on KaedeException catch (error) {
        if (operation.phase == 'proposing' &&
            error.code == 'E2EE_OPERATION_NOT_FOUND') {
          final channel = await _continueRoomOperation(operation, leaseToken);
          _reconciledRoomChannels[operation.channelRef] = channel;
          continue;
        }
        rethrow;
      }
      switch (status['status']) {
        case 'claiming':
          if (operation.phase != 'proposing') {
            throw StateError(
              'The encrypted-room authority lost a prepared operation.',
            );
          }
          final channel = await _continueRoomOperation(operation, leaseToken);
          _reconciledRoomChannels[operation.channelRef] = channel;
          continue;
        case 'prepared':
          if (operation.phase == 'proposing') {
            final prepared = _optionalMap(status['prepared']);
            if (prepared == null) {
              throw StateError(
                'The encrypted-room authority lost its prepared proposal.',
              );
            }
            final channel = await _prepareRoomOperation(
              operation,
              _validateRoomProposal(prepared, operation),
              leaseToken,
            );
            _reconciledRoomChannels[operation.channelRef] = channel;
          } else {
            final channel =
                await _activatePendingRoomOperation(operation, leaseToken);
            _reconciledRoomChannels[operation.channelRef] = channel;
          }
          continue;
        case 'committed':
          final committed = _optionalMap(status['committed']);
          if (operation.phase != 'activating' || committed == null) {
            throw StateError(
              'The authority committed an encrypted room without its portable MLS state.',
            );
          }
          final channel = _validateCommittedRoomOperation(committed, operation);
          _pendingRoomOperations.remove(operation.operationId);
          await _persist(leaseToken);
          _reconciledRoomChannels[operation.channelRef] = channel;
          continue;
        default:
          _pendingRoomOperations.remove(operation.operationId);
          await _persist(leaseToken);
          throw StateError(
            'The encrypted-room update expired or became stale. Review the member list and try again.',
          );
      }
    }
  }

  Future<Map<String, Object?>> encryptMessage(
    KaedeChannel channel,
    String content, {
    String operation = 'create',
    EntityRef? targetMessage,
    List<Map<String, Object?>> attachments = const [],
  }) =>
      _synchronized(
        () async {
          await _syncControlLog(channel);
          return _encryptMessage(
            channel,
            content,
            operation: operation,
            targetMessage: targetMessage,
            attachments: attachments,
          );
        },
      );

  Future<Map<String, Object?>> _encryptMessage(
    KaedeChannel channel,
    String content, {
    required String operation,
    required EntityRef? targetMessage,
    required List<Map<String, Object?>> attachments,
  }) async {
    _requireActive(channel);
    if ((operation == 'edit') != (targetMessage != null) ||
        !{'create', 'edit'}.contains(operation)) {
      throw ArgumentError(
        'Encrypted creates require no target and edits require one target.',
      );
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
    final groupId = _decode(channel.encryptionGroupId!, 32);
    final encoded = Uint8List.fromList(utf8.encode(plaintext));
    final aad = _messageContextBytes(context);
    Uint8List? ciphertext;
    try {
      ciphertext = _mls.encrypt(groupId, encoded, aad);
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
      _putMessageCache(
        '${envelope['ciphertext']}',
        MobileMessageCacheEntry(
          plaintext: plaintext,
          authorRef: accountRef,
          messageRef: null,
        ),
      );
      return envelope;
    } finally {
      groupId.fillRange(0, groupId.length, 0);
      encoded.fillRange(0, encoded.length, 0);
      aad.fillRange(0, aad.length, 0);
      ciphertext?.fillRange(0, ciphertext.length, 0);
    }
  }

  Future<DecryptedE2EEApplication?> decryptMessage(
    KaedeChannel channel,
    KaedeMessage message,
  ) =>
      _synchronized(() async {
        await _syncControlLog(channel);
        return _decryptMessage(channel, message);
      });

  Future<DecryptedE2EEApplication?> _decryptMessage(
    KaedeChannel channel,
    KaedeMessage message,
  ) async {
    _requireEncrypted(channel);
    final envelope = message.e2ee;
    if (envelope == null || envelope['version'] != 2) return null;
    if (message.channelRef != channel.ref ||
        envelope['protocol'] != mlsProtocol ||
        envelope['suite'] != mlsSuite ||
        '${envelope['policy_generation']}' !=
            '${message.encryptionPolicyGeneration}' ||
        '${envelope['epoch']}' != '${message.encryptionEpoch}') {
      throw const FormatException(
          'Encrypted message context does not match this conversation.');
    }
    validateMobileE2EEMessageProjection(message, envelope);
    final ciphertextText = '${envelope['ciphertext']}';
    final processedKey = _processedCacheKey(
      message.ref,
      message.channelRef,
      message.authorRef,
      envelope,
    );
    if (_processed.containsKey(processedKey)) {
      return _processed[processedKey];
    }
    final groupId = _decode('${envelope['group_id']}', 32);
    final ciphertext = _decode(ciphertextText, 64 * 1024);
    try {
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
      NativeMlsProcessed? processed;
      Uint8List? expectedAad;
      Uint8List? credentialNonce;
      try {
        var cached = _messageCache[ciphertextText];
        String? plaintext;
        if (cached != null) {
          if (cached.authorRef != message.authorRef.wire) {
            throw const FormatException(
              'Encrypted message cache does not match its author.',
            );
          }
          if (cached.messageRef == null) {
            if (cached.authorRef != accountRef ||
                message.authorRef.wire != accountRef) {
              throw const FormatException(
                'Only this account can bind a pending encrypted message.',
              );
            }
            cached = cached.bindMessage(message.ref.wire);
          } else if (cached.messageRef != message.ref.wire) {
            throw const FormatException(
              'Encrypted message cache does not match this message.',
            );
          }
          plaintext = cached.plaintext;
        } else {
          processed = _mls.process(groupId, ciphertext);
          if (processed.kind != 'application' ||
              processed.application == null ||
              processed.aad == null ||
              processed.credential == null) {
            return null;
          }
          expectedAad = _messageContextBytes(expectedContext);
          if (!_constantTimeEquals(processed.aad!, expectedAad)) {
            throw const FormatException(
                'Encrypted message authenticated context was modified.');
          }
          final credential = Map<String, Object?>.from(
            jsonDecode(
              utf8.decode(processed.credential!, allowMalformed: false),
            ) as Map,
          );
          final nonce = credential['nonce'];
          if (nonce is! String) {
            throw const FormatException(
                'Encrypted message sender identity does not match its author.');
          }
          credentialNonce = _decode(nonce, 32);
          const credentialFields = <String>{'version', 'account', 'nonce'};
          if (credential.length != credentialFields.length ||
              !credential.keys.toSet().containsAll(credentialFields) ||
              credential['version'] != 1 ||
              credential['account'] != message.authorRef.wire ||
              credentialNonce.length != 32) {
            throw const FormatException(
                'Encrypted message sender identity does not match its author.');
          }
          plaintext =
              utf8.decode(processed.application!, allowMalformed: false);
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
          throw const FormatException(
              'Encrypted message plaintext is invalid.');
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
        _putMessageCache(
          ciphertextText,
          cached ??
              MobileMessageCacheEntry(
                plaintext: plaintext,
                authorRef: message.authorRef.wire,
                messageRef: message.ref.wire,
              ),
        );
        _processed[processedKey] = result;
        return result;
      } finally {
        expectedAad?.fillRange(0, expectedAad.length, 0);
        credentialNonce?.fillRange(0, credentialNonce.length, 0);
        for (final bytes in <Uint8List?>[
          processed?.application,
          processed?.aad,
          processed?.credential,
        ]) {
          bytes?.fillRange(0, bytes.length, 0);
        }
      }
    } finally {
      groupId.fillRange(0, groupId.length, 0);
      ciphertext.fillRange(0, ciphertext.length, 0);
    }
  }

  Future<List<KaedeMessage>> decryptMessages(
    KaedeChannel channel,
    Iterable<KaedeMessage> messages,
  ) =>
      _synchronized(() async {
        await _syncControlLog(channel);
        return _decryptMessages(channel, messages);
      });

  Future<List<KaedeMessage>> _decryptMessages(
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
        final decrypted = await _decryptMessage(channel, message);
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

  void _putMessageCache(
    String ciphertext,
    MobileMessageCacheEntry entry,
  ) {
    // Dart maps retain insertion order. Delete+set makes successful cache use
    // an actual LRU touch instead of a creation-order approximation.
    _messageCache.remove(ciphertext);
    _messageCache[ciphertext] = entry;
    while (_messageCache.length > maximumMobileMessageCacheEntries) {
      _messageCache.remove(_messageCache.keys.first);
    }
  }

  Future<void> syncRoomState(KaedeChannel channel) => _synchronized(() async {
        await _syncControlLog(channel);
        final messages = await repository.messages(channel.ref, limit: 100);
        await _decryptMessages(channel, messages.reversed);
      });

  Future<void> _syncControlLog(KaedeChannel channel) async {
    _requireEncrypted(channel);
    final cursorKey = channel.ref.wire;
    final storedCursor = _controlCursors[cursorKey];
    EntityRef? after;
    if (storedCursor != null) {
      after = EntityRef.parse(storedCursor);
      if (after.wire != storedCursor) {
        throw const FormatException(
          'The encryption control cursor is invalid.',
        );
      }
    }
    final seenCursors = <String>{};
    for (var pageIndex = 0; pageIndex < _maximumControlLogPages; pageIndex++) {
      final page = parseMobileE2EEControlPage(
        await repository.e2eeControlLog(
          channel.ref,
          after: after?.wire,
        ),
        after: after,
        channel: channel.ref,
      );
      for (final control in page.controls) {
        _processControlRecord(control);
        // Advance only after the corresponding MLS control succeeds. The
        // cursor and MLS mutation are sealed into the same vault write.
        _controlCursors.remove(cursorKey);
        _controlCursors[cursorKey] = control.ref.wire;
      }
      if (page.nextAfter == null) return;
      if (!seenCursors.add(page.nextAfter!.wire)) {
        throw const FormatException(
          'The encryption control log cursor is invalid.',
        );
      }
      after = page.nextAfter;
      if (pageIndex == _maximumControlLogPages - 1) {
        throw StateError(
          'The encryption control log is too large to synchronize safely.',
        );
      }
    }
  }

  void _processControlRecord(MobileE2EEControlRecord record) {
    final envelope = record.envelope;
    final operation = envelope['operation'];
    final senderDevice = envelope['sender_device_id'];
    if (envelope['version'] != 2 ||
        envelope['protocol'] != mlsProtocol ||
        envelope['suite'] != mlsSuite ||
        '${envelope['policy_generation']}' != record.policyGeneration ||
        '${envelope['epoch']}' != record.epoch ||
        (operation != 'welcome' && operation != 'commit') ||
        (operation == 'welcome' && !record.apply) ||
        (!record.apply && operation != 'commit') ||
        senderDevice is! String ||
        !RegExp(r'^ked_[A-Za-z0-9_-]{43}$').hasMatch(senderDevice)) {
      throw const FormatException('The encryption control record is invalid.');
    }
    final ciphertextText = envelope['ciphertext'];
    if (ciphertextText is! String) {
      throw const FormatException('The encryption control record is invalid.');
    }
    final processedKey = _processedCacheKey(
      record.ref,
      record.channelRef,
      record.authorRef,
      envelope,
    );
    if (_processed.containsKey(processedKey)) return;
    final groupId = _decode('${envelope['group_id']}', 32);
    final ciphertext = _decode(ciphertextText, 64 * 1024);
    try {
      if (!record.apply) {
        // Initial activation/rekey creates a fresh group. Its Welcome already
        // contains the post-add state, so the paired commit is retained only
        // as an immutable audit record and must never be applied twice.
        _processed[processedKey] = null;
        return;
      }
      if (operation == 'welcome') {
        if (!_mls.hasGroup(groupId)) {
          final joinedGroup = _mls.joinGroup(ciphertext);
          try {
            if (!_constantTimeEquals(joinedGroup, groupId)) {
              throw const FormatException(
                'The encrypted Welcome belongs to another group.',
              );
            }
          } finally {
            joinedGroup.fillRange(0, joinedGroup.length, 0);
          }
        }
      } else {
        if (!_mls.hasGroup(groupId)) {
          throw const FormatException(
            'An encrypted commit arrived before its Welcome.',
          );
        }
        final processed = _mls.process(groupId, ciphertext);
        try {
          if (processed.kind != 'commit') {
            throw const FormatException(
              'The encryption control message is not an MLS commit.',
            );
          }
        } finally {
          for (final bytes in <Uint8List?>[
            processed.application,
            processed.aad,
            processed.credential,
          ]) {
            bytes?.fillRange(0, bytes.length, 0);
          }
        }
      }
      _processed[processedKey] = null;
    } finally {
      groupId.fillRange(0, groupId.length, 0);
      ciphertext.fillRange(0, ciphertext.length, 0);
    }
  }

  Future<Uint8List> mediaKey(KaedeChannel channel, String context) =>
      _synchronized(
        () async {
          await _syncControlLog(channel);
          return _mediaKey(channel, context);
        },
      );

  Future<Uint8List> _mediaKey(
    KaedeChannel channel,
    String context,
  ) async {
    _requireActive(channel);
    if (context.isEmpty || context.length > 256) {
      throw ArgumentError('Invalid encrypted media context.');
    }
    final groupId = _decode(channel.encryptionGroupId!, 32);
    final contextBytes = Uint8List.fromList(utf8.encode(context));
    try {
      return _mls.exportEpochSecret(
        groupId,
        'kaede livekit v1',
        contextBytes,
        32,
      );
    } finally {
      groupId.fillRange(0, groupId.length, 0);
      contextBytes.fillRange(0, contextBytes.length, 0);
    }
  }

  Future<String> safetyNumber(KaedeChannel channel) => _synchronized(
        () async {
          await _syncControlLog(channel);
          return _safetyNumber(channel);
        },
      );

  Future<String> _safetyNumber(KaedeChannel channel) async {
    _requireEncrypted(channel);
    final groupId = _decode(channel.encryptionGroupId!, 32);
    final roster = _mls.memberRoster(groupId);
    Uint8List? digest;
    try {
      digest = Uint8List.fromList((await Sha256().hash(roster)).bytes);
      final digits = digest
          .take(15)
          .map((value) => value.toString().padLeft(3, '0'))
          .join();
      return RegExp(r'.{1,5}')
          .allMatches(digits)
          .map((match) => match.group(0))
          .join(' ');
    } finally {
      groupId.fillRange(0, groupId.length, 0);
      roster.fillRange(0, roster.length, 0);
      digest?.fillRange(0, digest.length, 0);
    }
  }

  Future<String> exportRecovery(String passphrase) => _synchronized(
        () async {
          final state = await store.load(accountRef);
          if (state == null) {
            throw StateError('No encryption state exists on this device.');
          }
          return store.exportRecovery(state, passphrase);
        },
        persist: false,
      );

  Future<void> close() {
    final existing = _closeFuture;
    if (existing != null) return existing;
    _closed = true;
    final operationTail = _operationTail;
    final closing = () async {
      await operationTail;
      _messageCache.clear();
      _controlCursors.clear();
      _pendingRoomOperations.clear();
      _reconciledRoomChannels.clear();
      _processed.clear();
      try {
        _mls.close();
      } finally {
        _vaultKey.destroy();
      }
    }();
    _closeFuture = closing;
    return closing;
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
