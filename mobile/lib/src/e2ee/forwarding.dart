import 'dart:convert';
import 'dart:io';

import 'package:cryptography/cryptography.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/media.dart';
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';

final _forwardNonce = RegExp(r'^[A-Za-z0-9._:-]{1,64}$');
final _canonicalDigest = RegExp(r'^[A-Za-z0-9_-]{43}$');

bool mobilePreparedForwardNeedsCopies(
  String sourceMode,
  Iterable<String> destinationModes,
  int attachmentCount,
) =>
    attachmentCount > 0 ||
    sourceMode == 'e2ee' ||
    destinationModes.any((mode) => mode == 'e2ee');
const _forwardSourceFields = <String>{
  'message_ref',
  'channel_ref',
  'encryption_mode',
  'projection_version',
  'projection_digest',
  'created_at',
  'edited_at',
  'flags',
  'message_type',
  'nsfw',
  'attachment_refs',
  'snapshot',
};
const _forwardDestinationFields = <String>{
  'channel_id',
  'client_nonce',
  'encryption_mode',
  'requires_plaintext_disclosure',
  'authorization',
};
const _forwardProofContentFields = <String>{
  'version',
  'requester_ref',
  'requester_type',
  'source_message_ref',
  'source_channel_ref',
  'destination_channel_ref',
  'destination_encryption_mode',
  'source_encryption_mode',
  'source_projection_version',
  'source_projection_digest',
  'source_created_at',
  'source_edited_at',
  'source_flags',
  'source_message_type',
  'source_nsfw',
  'source_attachment_refs',
  'source_sticker_items',
  'source_custom_emoji_refs',
  'source_snapshot',
  'application_ref',
  'e2ee_device_id',
  'nonce',
  'expires_at',
};

final class MobilePreparedForwardSource {
  const MobilePreparedForwardSource({
    required this.messageRef,
    required this.channelRef,
    required this.encryptionMode,
    required this.projectionDigest,
    required this.createdAt,
    required this.editedAt,
    required this.flags,
    required this.messageType,
    required this.nsfw,
    required this.attachmentRefs,
    required this.snapshot,
  });

  final EntityRef messageRef;
  final EntityRef channelRef;
  final String encryptionMode;
  final String projectionDigest;
  final String createdAt;
  final String? editedAt;
  final int flags;
  final int messageType;
  final bool nsfw;
  final List<EntityRef> attachmentRefs;
  final Map<String, Object?>? snapshot;
}

final class MobilePreparedForwardDestination {
  const MobilePreparedForwardDestination({
    required this.channel,
    required this.nonce,
    required this.encryptionMode,
    required this.requiresPlaintextDisclosure,
    required this.authorization,
  });

  final KaedeChannel channel;
  final String nonce;
  final String encryptionMode;
  final bool requiresPlaintextDisclosure;
  final Map<String, Object?> authorization;
}

final class MobilePreparedForward {
  const MobilePreparedForward({
    required this.source,
    required this.destinations,
  });

  final MobilePreparedForwardSource source;
  final List<MobilePreparedForwardDestination> destinations;
}

bool _samePreparedSource(
  MobilePreparedForwardSource left,
  MobilePreparedForwardSource right,
) =>
    left.messageRef == right.messageRef &&
    left.channelRef == right.channelRef &&
    left.encryptionMode == right.encryptionMode &&
    left.projectionDigest == right.projectionDigest &&
    left.createdAt == right.createdAt &&
    left.editedAt == right.editedAt &&
    left.flags == right.flags &&
    left.messageType == right.messageType &&
    left.nsfw == right.nsfw &&
    _sameJson(
      left.attachmentRefs.map((item) => item.wire).toList(),
      right.attachmentRefs.map((item) => item.wire).toList(),
    ) &&
    _sameJson(left.snapshot, right.snapshot);

Map<String, Object?> _record(Object? value, String label) {
  if (value is! Map || value.keys.any((key) => key is! String)) {
    throw FormatException('$label is invalid.');
  }
  return Map<String, Object?>.from(value);
}

bool _exact(Map<String, Object?> value, Set<String> fields) =>
    value.length == fields.length && value.keys.toSet().containsAll(fields);

EntityRef _qualifiedRef(Object? value, String label) {
  if (value is! String) throw FormatException('$label is invalid.');
  try {
    final ref = EntityRef.parse(value);
    if (ref.wire != value) throw const FormatException();
    return ref;
  } on Object {
    throw FormatException('$label is invalid.');
  }
}

DateTime _timestamp(Object? value, String label) {
  if (value is! String ||
      !RegExp(r'(?:Z|[+-][0-9]{2}:[0-9]{2})$').hasMatch(value)) {
    throw FormatException('$label is invalid.');
  }
  final parsed = DateTime.tryParse(value);
  if (parsed == null) throw FormatException('$label is invalid.');
  return parsed;
}

bool _sameJson(Object? left, Object? right) {
  final leftBytes = mobileCanonicalInteractionJson(left);
  final rightBytes = mobileCanonicalInteractionJson(right);
  try {
    if (leftBytes.length != rightBytes.length) return false;
    var different = 0;
    for (var index = 0; index < leftBytes.length; index++) {
      different |= leftBytes[index] ^ rightBytes[index];
    }
    return different == 0;
  } finally {
    leftBytes.fillRange(0, leftBytes.length, 0);
    rightBytes.fillRange(0, rightBytes.length, 0);
  }
}

List<EntityRef> _sortedRefs(Object? value, int maximum, String label) {
  if (value is! List || value.length > maximum) {
    throw FormatException('$label is invalid.');
  }
  final refs = value.map((item) => _qualifiedRef(item, label)).toList();
  final wires = refs.map((item) => item.wire).toList(growable: false);
  final sorted = wires.toSet().toList()..sort();
  if (wires.length != sorted.length ||
      List.generate(wires.length, (index) => wires[index] == sorted[index])
          .contains(false)) {
    throw FormatException('$label is invalid.');
  }
  return refs;
}

bool _canonicalProofStickerItems(Object? value) {
  if (value is! List || value.length > 9) return false;
  final refs = <String>[];
  for (final item in value) {
    if (item is! Map || item.keys.any((key) => key is! String)) return false;
    final raw = Map<String, Object?>.from(item);
    if (!_exact(raw, const <String>{
          'id',
          'origin_domain',
          'name',
          'format_type',
          'media_hash',
        }) ||
        raw['id'] is! String ||
        raw['origin_domain'] is! String ||
        raw['name'] is! String ||
        (raw['name']! as String).trim() != raw['name'] ||
        (raw['name']! as String).runes.length < 2 ||
        (raw['name']! as String).runes.length > 30 ||
        (raw['name']! as String)
            .runes
            .any((code) => code < 32 || code == 127) ||
        raw['format_type'] is! int ||
        !const <int>{1, 2, 3, 4}.contains(raw['format_type']) ||
        raw['media_hash'] is! String ||
        !RegExp(r'^[0-9a-f]{64}$').hasMatch(raw['media_hash']! as String)) {
      return false;
    }
    try {
      refs.add(_qualifiedRef(
        '${raw['id']}@${raw['origin_domain']}',
        'Forward source sticker',
      ).wire);
    } on FormatException {
      return false;
    }
  }
  final sorted = refs.toSet().toList()..sort();
  return refs.length == sorted.length && _sameJson(refs, sorted);
}

MobilePreparedForwardSource _preparedSource(
  Object? value,
  EntityRef expectedChannel,
  EntityRef expectedMessage,
) {
  final raw = _record(value, 'Forward source authorization');
  if (!_exact(raw, _forwardSourceFields) ||
      raw['encryption_mode'] != 'plaintext' &&
          raw['encryption_mode'] != 'e2ee' ||
      raw['projection_version'] != 2 ||
      raw['projection_digest'] is! String ||
      !_canonicalDigest.hasMatch(raw['projection_digest']! as String) ||
      raw['flags'] is! int ||
      (raw['flags']! as int) < 0 ||
      raw['message_type'] is! int ||
      raw['nsfw'] is! bool) {
    throw const FormatException('Forward source authorization is invalid.');
  }
  final message = _qualifiedRef(raw['message_ref'], 'Forward source message');
  final channel = _qualifiedRef(raw['channel_ref'], 'Forward source channel');
  final created = _timestamp(raw['created_at'], 'Forward creation timestamp');
  final edited = raw['edited_at'] == null
      ? null
      : _timestamp(raw['edited_at'], 'Forward edit timestamp');
  if (message != expectedMessage ||
      channel != expectedChannel ||
      edited != null && edited.isBefore(created)) {
    throw const FormatException('Forward source authorization is invalid.');
  }
  final attachmentRefs = _sortedRefs(
    raw['attachment_refs'],
    10,
    'Forward source attachments',
  );
  final snapshot = raw['snapshot'] == null
      ? null
      : _record(raw['snapshot'], 'Forward source snapshot');
  if (snapshot != null) validateMobileEncryptedForwardSnapshot(snapshot);
  if ((raw['encryption_mode'] == 'plaintext') != (snapshot != null)) {
    throw const FormatException('Forward source authorization is invalid.');
  }
  return MobilePreparedForwardSource(
    messageRef: message,
    channelRef: channel,
    encryptionMode: raw['encryption_mode']! as String,
    projectionDigest: raw['projection_digest']! as String,
    createdAt: raw['created_at']! as String,
    editedAt: raw['edited_at'] as String?,
    flags: raw['flags']! as int,
    messageType: raw['message_type']! as int,
    nsfw: raw['nsfw']! as bool,
    attachmentRefs: attachmentRefs,
    snapshot: snapshot,
  );
}

void _validateProof(
  Object? value,
  MobilePreparedForwardSource source,
  EntityRef requester,
  KaedeChannel destination,
  String nonce,
) {
  final envelope = _record(value, 'Forward source proof');
  for (final field in const <String>{
    'event_id',
    'origin',
    'type',
    'ts',
    'actor',
    'context',
    'content',
    'signatures',
  }) {
    if (!envelope.containsKey(field)) {
      throw const FormatException('Forward source proof is invalid.');
    }
  }
  final signatures = envelope['signatures'];
  if (envelope['event_id'] is! String ||
      !RegExp(r'^kcfe_[A-Za-z0-9_-]{16,59}$')
          .hasMatch(envelope['event_id']! as String) ||
      envelope['origin'] != source.channelRef.domain.value ||
      envelope['type'] != 'message.forward.source.authorized' ||
      envelope['ts'] is! int ||
      (envelope['ts']! as int) < 0 ||
      signatures is! Map ||
      signatures.isEmpty ||
      signatures.values.any((item) => item is! Map || item.isEmpty)) {
    throw const FormatException('Forward source proof is invalid.');
  }
  final context = _record(envelope['context'], 'Forward source proof context');
  if (!_exact(context, const <String>{'source_channel_ref'}) ||
      context['source_channel_ref'] != source.channelRef.wire) {
    throw const FormatException('Forward source proof is invalid.');
  }
  final content = _record(envelope['content'], 'Forward source proof content');
  if (!_exact(content, _forwardProofContentFields) ||
      content['version'] != 1 ||
      content['requester_ref'] != requester.wire ||
      content['requester_type'] != 'human' ||
      content['source_message_ref'] != source.messageRef.wire ||
      content['source_channel_ref'] != source.channelRef.wire ||
      content['destination_channel_ref'] != destination.ref.wire ||
      content['destination_encryption_mode'] != destination.encryptionMode ||
      content['source_encryption_mode'] != source.encryptionMode ||
      content['source_projection_version'] != 2 ||
      content['source_projection_digest'] != source.projectionDigest ||
      content['source_created_at'] != source.createdAt ||
      content['source_edited_at'] != source.editedAt ||
      content['source_flags'] != source.flags ||
      content['source_message_type'] != source.messageType ||
      content['source_nsfw'] != source.nsfw ||
      !_sameJson(
        content['source_attachment_refs'],
        source.attachmentRefs.map((item) => item.wire).toList(),
      ) ||
      !_canonicalProofStickerItems(content['source_sticker_items']) ||
      !mobileCanonicalSortedCustomEmojiRefs(
        content['source_custom_emoji_refs'],
      ) ||
      !_sameJson(content['source_snapshot'], source.snapshot) ||
      content['application_ref'] != null ||
      content['e2ee_device_id'] != null ||
      content['nonce'] != nonce) {
    throw const FormatException('Forward source proof binding is invalid.');
  }
  final expiry = _timestamp(content['expires_at'], 'Forward proof expiry');
  if (!expiry.isAfter(DateTime.now().toUtc())) {
    throw const FormatException('Forward source proof expired.');
  }
}

MobilePreparedForward validateMobilePreparedForwardResponse(
  Object? value, {
  required EntityRef sourceChannel,
  required EntityRef sourceMessage,
  required EntityRef requester,
  required Map<EntityRef, String> requested,
  required Map<EntityRef, KaedeChannel> channels,
}) {
  final raw = _record(value, 'Prepared forward response');
  if (!_exact(raw, const <String>{'source', 'destinations'}) ||
      raw['destinations'] is! List ||
      (raw['destinations']! as List).length != requested.length) {
    throw const FormatException('Prepared forward response is invalid.');
  }
  final source = _preparedSource(raw['source'], sourceChannel, sourceMessage);
  final seen = <EntityRef>{};
  final destinations = (raw['destinations']! as List).map((value) {
    final item = _record(value, 'Prepared forward destination');
    if (!_exact(item, _forwardDestinationFields) ||
        item['client_nonce'] is! String ||
        !_forwardNonce.hasMatch(item['client_nonce']! as String) ||
        item['encryption_mode'] != 'plaintext' &&
            item['encryption_mode'] != 'e2ee' ||
        item['requires_plaintext_disclosure'] is! bool) {
      throw const FormatException('Prepared forward destination is invalid.');
    }
    final ref = _qualifiedRef(
      item['channel_id'],
      'Prepared forward destination',
    );
    final channel = channels[ref];
    final nonce = item['client_nonce']! as String;
    if (!seen.add(ref) ||
        channel == null ||
        requested[ref] != nonce ||
        item['encryption_mode'] != channel.encryptionMode ||
        item['requires_plaintext_disclosure'] !=
            (source.encryptionMode == 'e2ee' &&
                channel.encryptionMode == 'plaintext')) {
      throw const FormatException('Prepared forward destination is invalid.');
    }
    final authorization = _record(
      item['authorization'],
      'Forward source proof',
    );
    _validateProof(authorization, source, requester, channel, nonce);
    return MobilePreparedForwardDestination(
      channel: channel,
      nonce: nonce,
      encryptionMode: item['encryption_mode']! as String,
      requiresPlaintextDisclosure:
          item['requires_plaintext_disclosure']! as bool,
      authorization: authorization,
    );
  }).toList(growable: false);
  return MobilePreparedForward(source: source, destinations: destinations);
}

String _attachmentRef(Object? value) {
  final raw = _record(value, 'Forward attachment binding');
  final id =
      raw['protocol'] == 'kaede-file-v1' ? raw['attachment_id'] : raw['id'];
  final domain = raw['protocol'] == 'kaede-file-v1'
      ? raw['attachment_domain']
      : raw['origin_domain'];
  final ref = _qualifiedRef('$id@$domain', 'Forward attachment binding');
  if (ref.id.value != id || ref.domain.value != domain) {
    throw const FormatException('Forward attachment binding is invalid.');
  }
  return ref.wire;
}

Map<String, Object?> rebindMobileForwardSnapshot(
  Object? value,
  List<Map<String, Object?>> replacements,
) {
  final source = validateMobileEncryptedForwardSnapshot(value);
  final snapshot = Map<String, Object?>.from(
    jsonDecode(jsonEncode(source)) as Map,
  );
  final original = (snapshot['attachments']! as List).cast<Object?>();
  if (original.length != replacements.length) {
    throw const FormatException(
      'Forward destination attachment count is invalid.',
    );
  }
  final originalRefs = original.map(_attachmentRef).toList(growable: false);
  final replacementRefs =
      replacements.map(_attachmentRef).toList(growable: false);
  if (replacementRefs.toSet().length != replacementRefs.length ||
      replacementRefs.any(originalRefs.contains)) {
    throw const FormatException(
      'Forward destination attachments were not freshly rebound.',
    );
  }
  for (var index = 0; index < replacements.length; index++) {
    final left = _record(original[index], 'Forward source attachment');
    final right = replacements[index];
    if (!_sameJson(
          mobileEncryptedForwardAttachmentSemantics(left),
          mobileEncryptedForwardAttachmentSemantics(right),
        ) ||
        left['protocol'] == 'kaede-file-v1' &&
            right['protocol'] == 'kaede-file-v1' &&
            const <String>{'file_id', 'key', 'ciphertext_sha256'}
                .any((field) => left[field] == right[field])) {
      throw const FormatException(
        'Forward destination attachment is not a fresh semantic copy.',
      );
    }
  }
  final sourceIndex = <String, int>{
    for (var index = 0; index < originalRefs.length; index++)
      originalRefs[index]: index,
  };
  if (sourceIndex.length != original.length) {
    throw const FormatException('Forward source attachment is ambiguous.');
  }
  final nested = (snapshot['message_snapshots']! as List).cast<Object?>();
  if (nested.isNotEmpty) {
    final child = _record(nested.single, 'Nested forward snapshot');
    final childAttachments = (child['attachments']! as List).cast<Object?>();
    final used = <int>{};
    child['attachments'] = childAttachments.map((item) {
      final index = sourceIndex[_attachmentRef(item)];
      if (index == null ||
          !used.add(index) ||
          !_sameJson(
            mobileEncryptedForwardAttachmentSemantics(item),
            mobileEncryptedForwardAttachmentSemantics(original[index]),
          )) {
        throw const FormatException(
          'Nested forward attachment binding is invalid.',
        );
      }
      return replacements[index];
    }).toList(growable: false);
    if (used.length != original.length) {
      throw const FormatException(
        'Nested forward attachments do not cover the source files.',
      );
    }
    snapshot['message_snapshots'] = <Object?>[child];
  }
  snapshot['attachments'] = replacements;
  return validateMobileEncryptedForwardSnapshot(snapshot);
}

Map<String, Object?> _sourceSnapshot(
  KaedeMessage message,
  MobilePreparedForwardSource source,
) {
  if (source.encryptionMode == 'plaintext') {
    return validateMobileEncryptedForwardSnapshot(source.snapshot);
  }
  if (!message.e2eeVerified ||
      message.e2ee?['forward_projection_version'] != 2 ||
      message.e2ee?['forward_projection_digest'] != source.projectionDigest) {
    throw StateError('The encrypted source is not safely forwardable.');
  }
  return validateMobileEncryptedForwardSnapshot(<String, Object?>{
    'content': message.content,
    'embeds': message.embeds.map((item) => item.toJson()).toList(),
    'components': message.components.map((item) => item.toJson()).toList(),
    'attachments': message.decryptedAttachments,
    'mention_user_refs': (message.mentionUserRefs.toList()
          ..sort((left, right) => left.wire.compareTo(right.wire)))
        .map((item) => <String, Object?>{
              'id': item.id.value,
              'origin_domain': item.domain.value,
            })
        .toList(),
    'sticker_items': message.stickerItems.map((item) => item.toJson()).toList(),
    'message_snapshots': message.decryptedForwardSnapshot == null
        ? const <Object?>[]
        : <Object?>[message.decryptedForwardSnapshot],
    'message_type': source.messageType,
    'flags': source.flags,
    'created_at': source.createdAt,
    'edited_at': source.editedAt,
  });
}

Future<String> _fileSha256(File file) async {
  final sink = Sha256().newHashSink();
  await for (final chunk in file.openRead()) {
    sink.add(chunk);
  }
  sink.close();
  final digest = (await sink.hash()).bytes;
  return base64UrlEncode(digest).replaceAll('=', '');
}

({int? durationMillis, String? waveform}) _voiceMetadata(Object? value) {
  final stable = mobileEncryptedForwardAttachmentSemantics(value);
  return (
    durationMillis: stable['duration_millis'] as int?,
    waveform: stable['waveform'] as String?,
  );
}

Future<List<File>> _downloadSourceFiles(
  KaedeRepository repository,
  KaedeMessage message,
  Map<String, Object?> snapshot,
) async {
  final directory = await getTemporaryDirectory();
  final projected = <String, KaedeAttachment>{
    for (final attachment in message.attachments)
      attachment.ref.wire: attachment,
  };
  final files = <File>[];
  try {
    for (final (index, value) in (snapshot['attachments']! as List).indexed) {
      final raw = _record(value, 'Forward source attachment');
      final semantics = mobileEncryptedForwardAttachmentSemantics(raw);
      final projection = projected[_attachmentRef(raw)];
      if (projection == null) {
        throw StateError('A forward source attachment is unavailable.');
      }
      final file = File(
        '${directory.path}/kaede-forward-${DateTime.now().microsecondsSinceEpoch}-$index',
      );
      if (raw['protocol'] == 'kaede-file-v1') {
        await downloadEncryptedFile(
          repository: repository,
          manifest: raw,
          destination: file,
          historyMediaUrl: projection.historyMediaUrl,
          privateMediaUrl: projection.privateMediaUrl,
        );
      } else {
        await repository.downloadAttachment(projection, file);
      }
      if (await file.length() != semantics['plaintext_size'] ||
          await _fileSha256(file) != semantics['plaintext_sha256']) {
        throw const FormatException('Forward source attachment was modified.');
      }
      files.add(file);
    }
    return files;
  } on Object {
    for (final file in files) {
      if (await file.exists()) await file.delete();
    }
    rethrow;
  }
}

Map<String, Object?> _plaintextReplacement(
  EntityRef ref,
  Object? source,
) {
  final stable = mobileEncryptedForwardAttachmentSemantics(source);
  final duration = stable['duration_millis'] as int?;
  return <String, Object?>{
    'id': ref.id.value,
    'origin_domain': ref.domain.value,
    'filename': stable['filename'],
    'content_type': stable['content_type'],
    'size': stable['plaintext_size'],
    'plaintext_sha256': stable['plaintext_sha256'],
    'width': null,
    'height': null,
    'duration_secs': duration == null ? null : duration / 1000,
    'waveform': stable['waveform'],
    'blurhash': null,
    'scan_status': 'pending',
    'encryption_mode': 'plaintext',
    'encryption_protocol': null,
    'variants': const <String, Object?>{},
  };
}

Future<({List<EntityRef> attachments, Map<String, Object?> snapshot})>
    _uploadDestinationFiles(
  KaedeRepository repository,
  MobilePreparedForwardDestination destination,
  Map<String, Object?> sourceSnapshot,
  List<File> files,
) async {
  final sourceAttachments =
      (sourceSnapshot['attachments']! as List).cast<Object?>();
  final refs = <EntityRef>[];
  final replacements = <Map<String, Object?>>[];
  // Keep uploads sequential: attachment refs are canonical-sorted in MLS and
  // must retain the source semantic order committed by the projection digest.
  for (var index = 0; index < files.length; index++) {
    final stable = mobileEncryptedForwardAttachmentSemantics(
      sourceAttachments[index],
    );
    final voice = _voiceMetadata(sourceAttachments[index]);
    if (destination.encryptionMode == 'e2ee') {
      final uploaded = await uploadEncryptedFile(
        repository: repository,
        channel: destination.channel.ref,
        source: files[index],
        filename: stable['filename']! as String,
        contentType: stable['content_type']! as String,
        durationMillis: voice.durationMillis,
        waveform: voice.waveform,
      );
      refs.add(uploaded.attachment);
      replacements.add(uploaded.manifest);
    } else {
      final ref = await repository.uploadAttachmentFile(
        channel: destination.channel.ref,
        filename: stable['filename']! as String,
        contentType: stable['content_type']! as String,
        file: files[index],
        durationSecs:
            voice.durationMillis == null ? null : voice.durationMillis! / 1000,
        waveform: voice.waveform,
      );
      refs.add(ref);
      replacements.add(_plaintextReplacement(ref, sourceAttachments[index]));
    }
  }
  return (
    attachments: refs,
    snapshot: rebindMobileForwardSnapshot(sourceSnapshot, replacements),
  );
}

Future<MessageForwardResult> executeMobilePreparedForward({
  required KaedeRepository repository,
  required KaedeMessage sourceMessage,
  required KaedeChannel sourceChannel,
  required List<KaedeChannel> destinationChannels,
  required EntityRef requester,
  MobileE2EEClient? e2eeClient,
  String? note,
}) async {
  if (destinationChannels.isEmpty || destinationChannels.length > 5) {
    throw StateError('Choose between one and five forwarding destinations.');
  }
  final channels = <EntityRef, KaedeChannel>{
    for (final channel in destinationChannels) channel.ref: channel,
  };
  if (channels.length != destinationChannels.length) {
    throw StateError('Forwarding destinations must be unique.');
  }
  final requests = <EntityRef, String>{
    for (final channel in destinationChannels)
      channel.ref: 'forward-${const Uuid().v4()}',
  };
  final response = await repository.prepareMessageForward(
    sourceChannel: sourceChannel.ref,
    sourceMessage: sourceMessage.ref,
    destinations: requests.entries
        .map((item) => (channel: item.key, nonce: item.value))
        .toList(growable: false),
  );
  final prepared = validateMobilePreparedForwardResponse(
    response,
    sourceChannel: sourceChannel.ref,
    sourceMessage: sourceMessage.ref,
    requester: requester,
    requested: requests,
    channels: channels,
  );
  if (prepared.source.encryptionMode != sourceChannel.encryptionMode) {
    throw StateError('Forward source encryption changed during preparation.');
  }
  final snapshot = _sourceSnapshot(sourceMessage, prepared.source);
  if (await mobileEncryptedForwardSnapshotProjectionDigest(snapshot) !=
      prepared.source.projectionDigest) {
    throw const FormatException(
      'The local source does not match its authority commitment.',
    );
  }
  final needsCopies = mobilePreparedForwardNeedsCopies(
    prepared.source.encryptionMode,
    prepared.destinations.map((item) => item.encryptionMode),
    (snapshot['attachments']! as List).length,
  );
  final files = needsCopies
      ? await _downloadSourceFiles(repository, sourceMessage, snapshot)
      : <File>[];
  try {
    final messages = <({EntityRef channel, Map<String, Object?> message})>[];
    for (final destination in prepared.destinations) {
      final copies = needsCopies
          ? await _uploadDestinationFiles(
              repository,
              destination,
              snapshot,
              files,
            )
          : null;
      if (copies != null &&
          await mobileEncryptedForwardSnapshotProjectionDigest(
                copies.snapshot,
              ) !=
              prepared.source.projectionDigest) {
        throw const FormatException(
          'Forward destination files do not match the source commitment.',
        );
      }
      final body = <String, Object?>{
        'attachment_ids':
            copies?.attachments.map((item) => item.id.value).toList() ??
                const <String>[],
        'forwarded_message_id': prepared.source.messageRef.wire,
        'client_nonce': destination.nonce,
      };
      final trimmedNote = note?.trim() ?? '';
      if (destination.encryptionMode == 'plaintext') {
        if (trimmedNote.isNotEmpty) body['content'] = trimmedNote;
        if (prepared.source.encryptionMode == 'e2ee') {
          body['forward_snapshot'] = copies!.snapshot;
        }
      } else {
        if (e2eeClient == null || copies == null) {
          throw StateError(
            'Encryption is unavailable for a forwarding destination.',
          );
        }
        body['e2ee'] = await e2eeClient.encryptMessage(
          destination.channel,
          trimmedNote,
          attachments:
              copies.snapshot['attachments']! as List<Map<String, Object?>>,
          rich: MobileEncryptedRichMessageOptions(
            allowedMentions: const <String, Object?>{
              'parse': <String>[],
              'users': <String>[],
              'roles': <String>[],
              'replied_user': false,
            },
            forward: MobileEncryptedMessageForward(
              snapshot: copies.snapshot,
              sourceProjectionDigest: prepared.source.projectionDigest,
              sourceMessageRef: prepared.source.messageRef,
              sourceChannelRef: prepared.source.channelRef,
              sourceCreatedAt: prepared.source.createdAt,
              sourceEditedAt: prepared.source.editedAt,
              sourceFlags: prepared.source.flags,
              sourceMessageType: prepared.source.messageType,
            ),
          ),
        );
      }
      messages.add((channel: destination.channel.ref, message: body));
    }
    // Large encrypted files can consume most of the 90-second proof lifetime.
    // Refresh with the same idempotency nonces immediately before submission,
    // then require every source and destination binding to remain unchanged.
    final refreshed = validateMobilePreparedForwardResponse(
      await repository.prepareMessageForward(
        sourceChannel: sourceChannel.ref,
        sourceMessage: sourceMessage.ref,
        destinations: requests.entries
            .map((item) => (channel: item.key, nonce: item.value))
            .toList(growable: false),
      ),
      sourceChannel: sourceChannel.ref,
      sourceMessage: sourceMessage.ref,
      requester: requester,
      requested: requests,
      channels: channels,
    );
    if (!_samePreparedSource(prepared.source, refreshed.source)) {
      throw StateError('The forward source changed while files were prepared.');
    }
    final refreshedByChannel = <EntityRef, MobilePreparedForwardDestination>{
      for (final item in refreshed.destinations) item.channel.ref: item,
    };
    for (final item in messages) {
      final proof = refreshedByChannel[item.channel];
      if (proof == null) {
        throw StateError('A forward destination changed during preparation.');
      }
      item.message['forward_source_proof'] = proof.authorization;
    }
    return repository.submitPreparedMessageForward(
      sourceChannel: sourceChannel.ref,
      sourceMessage: sourceMessage.ref,
      destinations: messages,
    );
  } finally {
    for (final file in files) {
      if (await file.exists()) await file.delete();
    }
  }
}
