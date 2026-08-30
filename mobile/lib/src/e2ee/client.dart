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
import 'package:kaede_mobile/src/domain/rich_content.dart';
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

final class MobilePreparedEncryptedInteraction {
  const MobilePreparedEncryptedInteraction({
    required this.envelope,
    required this.context,
    required this.attachmentIds,
  });

  final Map<String, Object?> envelope;
  final Map<String, Object?> context;
  final List<String> attachmentIds;
}

final class MobileDecryptedInteractionResponse {
  const MobileDecryptedInteractionResponse({
    required this.context,
    required this.data,
  });

  final Map<String, Object?> context;
  final Map<String, Object?> data;
}

/// Authenticated rich content for an ordinary encrypted Message v2.
///
/// Callers pass only locally derived/plaintext presentation data. The client
/// derives every server-routable contract and digest itself before MLS.
final class MobileEncryptedRichMessageOptions {
  const MobileEncryptedRichMessageOptions({
    this.embeds = const <Map<String, Object?>>[],
    this.components = const <Map<String, Object?>>[],
    this.poll,
    this.stickerItems = const <KaedeStickerItem>[],
    this.tts = false,
    this.voiceMessage = false,
    this.flags = 0,
    this.allowedMentions,
    this.forward,
    this.messageRevision,
  });

  final List<Map<String, Object?>> embeds;
  final List<Map<String, Object?>> components;
  final Map<String, Object?>? poll;
  final List<KaedeStickerItem> stickerItems;
  final bool tts;
  final bool voiceMessage;
  final int flags;
  final Map<String, Object?>? allowedMentions;
  final MobileEncryptedMessageForward? forward;
  final String? messageRevision;
}

final class MobileEncryptedMessageForward {
  const MobileEncryptedMessageForward({
    required this.snapshot,
    required this.sourceProjectionDigest,
    required this.sourceMessageRef,
    required this.sourceChannelRef,
    required this.sourceCreatedAt,
    required this.sourceEditedAt,
    required this.sourceFlags,
    required this.sourceMessageType,
  });

  final Map<String, Object?> snapshot;
  final String sourceProjectionDigest;
  final EntityRef sourceMessageRef;
  final EntityRef sourceChannelRef;
  final String sourceCreatedAt;
  final String? sourceEditedAt;
  final int sourceFlags;
  final int sourceMessageType;
}

MobileEncryptedRichMessageOptions? mobileEncryptedRichEditOptions(
  KaedeMessage message,
) {
  final envelope = message.e2ee;
  if (envelope == null || !envelope.containsKey('rich_payload_digest')) {
    return null;
  }
  if (message.poll != null) {
    throw StateError('Encrypted polls cannot be edited after publication.');
  }
  final revision = _canonicalMobileUnsignedI63(
    envelope['message_revision'],
    positive: true,
  );
  final maximum = BigInt.parse('9223372036854775807');
  if (revision == null || BigInt.parse(revision) >= maximum) {
    throw const FormatException('Encrypted message revision is invalid.');
  }
  return MobileEncryptedRichMessageOptions(
    embeds: message.embeds.map((item) => item.toJson()).toList(),
    components: message.components.map((item) => item.toJson()).toList(),
    stickerItems: message.stickerItems,
    tts: message.tts,
    voiceMessage: message.flags & (1 << 13) != 0,
    flags: message.flags,
    allowedMentions: message.decryptedAllowedMentions,
    messageRevision: '${BigInt.parse(revision) + BigInt.one}',
  );
}

Map<String, Object?> mobileInteractionResponseAuthenticatedContext(
  KaedeChannel channel, {
  required String authorityDomain,
  required String interactionRef,
  required String responseRef,
  required String invokerRef,
  required String channelRef,
  required String applicationRef,
  required int sequence,
  required String revision,
  required int callbackType,
  required String operation,
  required List<String> attachmentRefs,
  required String? interactionContractDigest,
  required String senderDeviceId,
}) {
  late final Domain authority;
  late final EntityRef interaction;
  late final EntityRef response;
  late final EntityRef invoker;
  late final EntityRef channelIdentity;
  late final EntityRef application;
  try {
    authority = Domain(authorityDomain);
    interaction = EntityRef.parse(interactionRef);
    response = EntityRef.parse(responseRef);
    invoker = EntityRef.parse(invokerRef);
    channelIdentity = EntityRef.parse(channelRef);
    application = EntityRef.parse(applicationRef);
  } on FormatException {
    throw const FormatException(
      'Encrypted bot response authority projection is invalid.',
    );
  }
  final sortedRefs = [...attachmentRefs]..sort();
  if (authority.value != authorityDomain ||
      interaction.domain != authority ||
      response.domain != authority ||
      channelIdentity.domain != authority ||
      channelIdentity != channel.ref ||
      application.wire != applicationRef ||
      invoker.wire != invokerRef ||
      sequence < 0 ||
      sequence > 9223372036854775807 ||
      !const <int>{4, 7, 8, 9}.contains(callbackType) ||
      !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(revision) ||
      BigInt.parse(revision) > BigInt.parse('9223372036854775807') ||
      !const <String>{'CREATE', 'UPDATE'}.contains(operation) ||
      (operation == 'CREATE' && revision != '1') ||
      (operation == 'UPDATE' && BigInt.parse(revision) <= BigInt.one) ||
      !RegExp(r'^kbe_[A-Za-z0-9_-]{43}$').hasMatch(senderDeviceId) ||
      (interactionContractDigest != null &&
          !isCanonicalBase64url32(interactionContractDigest)) ||
      attachmentRefs.length > 10 ||
      attachmentRefs.toSet().length != attachmentRefs.length ||
      !_listEquals(attachmentRefs, sortedRefs) ||
      attachmentRefs.any((ref) {
        try {
          final parsed = EntityRef.parse(ref);
          return parsed.wire != ref || parsed.domain != authority;
        } on FormatException {
          return true;
        }
      })) {
    throw const FormatException(
      'Encrypted bot response authority projection is invalid.',
    );
  }
  return <String, Object?>{
    'application_ref': application.wire,
    'attachment_refs': attachmentRefs,
    'authority_domain': authority.value,
    'callback_type': callbackType,
    'channel_ref': channelIdentity.wire,
    'epoch': '${channel.encryptionEpoch}',
    'group_id': channel.encryptionGroupId,
    'interaction_ref': interaction.wire,
    'interaction_contract_digest': interactionContractDigest,
    'invoker_ref': invoker.wire,
    'operation': operation == 'CREATE' ? 'create' : 'edit',
    'policy_generation': '${channel.encryptionPolicyGeneration}',
    'response_ref': response.wire,
    'revision': revision,
    'sender_device_id': senderDeviceId,
    'sequence': '$sequence',
  };
}

bool _listEquals<T>(List<T> left, List<T> right) =>
    left.length == right.length &&
    left.indexed.every((item) => item.$2 == right[item.$1]);

Object? _canonicalInteractionValue(Object? value, Set<Object> seen) {
  if (value == null || value is String || value is bool || value is int) {
    return value;
  }
  if (value is double) {
    if (!value.isFinite) {
      throw const FormatException(
        'Encrypted interaction JSON must contain finite numbers.',
      );
    }
    return value;
  }
  if (value is List) {
    return value
        .map((item) => _canonicalInteractionValue(item, seen))
        .toList(growable: false);
  }
  if (value is! Map) {
    throw const FormatException(
      'Encrypted interaction JSON contains an unsupported value.',
    );
  }
  if (!seen.add(value)) {
    throw const FormatException(
      'Encrypted interaction JSON cannot be recursive.',
    );
  }
  try {
    if (value.keys.any((key) => key is! String)) {
      throw const FormatException(
        'Encrypted interaction JSON object keys must be strings.',
      );
    }
    final keys = value.keys.cast<String>().toList()..sort();
    return <String, Object?>{
      for (final key in keys) key: _canonicalInteractionValue(value[key], seen),
    };
  } finally {
    seen.remove(value);
  }
}

Uint8List mobileCanonicalInteractionJson(Object? value) => Uint8List.fromList(
      utf8.encode(jsonEncode(_canonicalInteractionValue(value, <Object>{}))),
    );

const _mobileRichMessageContextFields = <String>{
  'application_ref',
  'attachment_manifest_digest',
  'author_ref',
  'channel_ref',
  'epoch',
  'forward_projection_digest',
  'forward_projection_version',
  'forward_snapshot_digest',
  'forward_source_projection_digest',
  'forwarded_channel_ref',
  'forwarded_created_at',
  'forwarded_edited_at',
  'forwarded_flags',
  'forwarded_message_ref',
  'forwarded_message_type',
  'group_id',
  'interaction_contract_digest',
  'interaction_installation_ref',
  'interaction_installation_revision',
  'interaction_integration_type',
  'message_attachment_refs',
  'message_custom_emoji_refs',
  'message_mention_everyone',
  'message_mention_refs',
  'message_mention_role_refs',
  'message_mention_user_refs',
  'message_replied_user_ref',
  'message_sticker_refs',
  'message_flags',
  'message_revision',
  'operation',
  'policy_generation',
  'referenced_message_ref',
  'rich_payload_digest',
  'sender_device_id',
  'target_message',
  'tts',
  'view_persistent',
  'view_version',
  'voice_message',
};

String? _canonicalMobileUnsignedI63(Object? value, {required bool positive}) {
  if (value is! String ||
      !(positive
              ? RegExp(r'^[1-9][0-9]{0,18}$')
              : RegExp(r'^(?:0|[1-9][0-9]{0,18})$'))
          .hasMatch(value)) {
    return null;
  }
  return BigInt.parse(value) <= BigInt.parse('9223372036854775807')
      ? value
      : null;
}

bool _canonicalMobileRef(Object? value) {
  if (value is! String) return false;
  try {
    return EntityRef.parse(value).wire == value;
  } on FormatException {
    return false;
  }
}

bool _canonicalMobileSortedRefs(Object? value, int maximum) {
  if (value is! List ||
      value.length > maximum ||
      value.any((item) => item is! String)) {
    return false;
  }
  final refs = value.cast<String>();
  return refs.every(_canonicalMobileRef) &&
      refs.toSet().length == refs.length &&
      _listEquals(refs, refs.toList()..sort());
}

final _mobileCustomEmojiRoutingToken = RegExp(
  r'^<(a?):([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>$',
);

bool _canonicalMobileCustomEmojiToken(Object? value) {
  if (value is! String) return false;
  final match = _mobileCustomEmojiRoutingToken.firstMatch(value);
  return match != null && _canonicalMobileRef('${match[3]}@${match[4]}');
}

bool mobileCanonicalSortedCustomEmojiRefs(Object? value) {
  if (value is! List ||
      value.length > 256 ||
      value.any((item) => !_canonicalMobileCustomEmojiToken(item))) {
    return false;
  }
  final refs = value.cast<String>();
  return refs.toSet().length == refs.length &&
      _listEquals(refs, refs.toList()..sort());
}

final _qualifiedMobileUserMention = RegExp(
  r'<@([1-9][0-9]{0,18})@([a-z0-9.-]{1,253})>',
  caseSensitive: false,
);
final _unqualifiedMobileUserMention = RegExp(r'<@[1-9][0-9]{0,18}>');
final _qualifiedMobileRoleMention = RegExp(
  r'<@&([1-9][0-9]{0,18})@([a-z0-9.-]{1,253})>',
  caseSensitive: false,
);
final _broadMobileEveryoneMention = RegExp(
  r'(?<![A-Za-z0-9_])@(?:everyone|here)\b',
  caseSensitive: false,
);

/// Validate the exact notification policy carried only in rich ciphertext.
Map<String, Object?> validateMobileEncryptedAllowedMentions(Object? value) {
  final raw = _mobileRoutingMap(value, 'Encrypted message allowed mentions');
  if (!_hasExactMobileRoutingFields(
        raw,
        const <String>{'parse', 'users', 'roles', 'replied_user'},
      ) ||
      raw['parse'] is! List ||
      raw['users'] is! List ||
      raw['roles'] is! List ||
      raw['replied_user'] is! bool) {
    throw const FormatException(
      'Encrypted message allowed mentions are invalid.',
    );
  }
  final parseRaw = raw['parse']! as List;
  if (parseRaw.any(
        (item) => item != 'everyone' && item != 'roles' && item != 'users',
      ) ||
      parseRaw.any((item) => item is! String)) {
    throw const FormatException(
      'Encrypted message allowed mentions are invalid.',
    );
  }
  final parse = parseRaw.cast<String>();
  if (parse.toSet().length != parse.length ||
      !_listEquals(parse, parse.toList()..sort()) ||
      !_canonicalMobileSortedRefs(raw['users'], 100) ||
      !_canonicalMobileSortedRefs(raw['roles'], 100) ||
      parse.contains('users') && (raw['users']! as List).isNotEmpty ||
      parse.contains('roles') && (raw['roles']! as List).isNotEmpty) {
    throw const FormatException(
      'Encrypted message allowed mentions are invalid.',
    );
  }
  return Map<String, Object?>.unmodifiable(<String, Object?>{
    'parse': List<String>.unmodifiable(parse),
    'users': List<String>.unmodifiable((raw['users']! as List).cast<String>()),
    'roles': List<String>.unmodifiable((raw['roles']! as List).cast<String>()),
    'replied_user': raw['replied_user'],
  });
}

/// Derive only Discord notification-bearing mention intent from rich data.
({List<String> userRefs, List<String> roleRefs, bool everyone})
    mobileRichMessageMentionIntent(Map<String, Object?> data) {
  final policy = validateMobileEncryptedAllowedMentions(
    data['allowed_mentions'],
  );
  final texts = <String>[];
  if (data['content'] case final String content) texts.add(content);
  final seen = Set<Object>.identity();
  void walkComponents(Object? value) {
    if (value is List) {
      if (!seen.add(value)) {
        throw const FormatException(
          'Encrypted message components cannot be recursive.',
        );
      }
      try {
        for (final nested in value) {
          walkComponents(nested);
        }
      } finally {
        seen.remove(value);
      }
      return;
    }
    if (value is! Map) return;
    if (!seen.add(value)) {
      throw const FormatException(
        'Encrypted message components cannot be recursive.',
      );
    }
    try {
      if (value['type'] == 10 && value['content'] is String) {
        texts.add(value['content']! as String);
      }
      for (final nested in value.values) {
        walkComponents(nested);
      }
    } finally {
      seen.remove(value);
    }
  }

  walkComponents(data['components']);
  final visibleUsers = <String>{};
  final visibleRoles = <String>{};
  var visibleEveryone = false;
  for (final text in texts) {
    if (_unqualifiedMobileUserMention.hasMatch(text)) {
      throw const FormatException(
        'Encrypted user mention tokens must be origin-qualified.',
      );
    }
    for (final match in _qualifiedMobileUserMention.allMatches(text)) {
      final ref = EntityRef.parse('${match[1]}@${match[2]!.toLowerCase()}');
      visibleUsers.add(ref.wire);
    }
    for (final match in _qualifiedMobileRoleMention.allMatches(text)) {
      final ref = EntityRef.parse('${match[1]}@${match[2]!.toLowerCase()}');
      visibleRoles.add(ref.wire);
    }
    visibleEveryone =
        visibleEveryone || _broadMobileEveryoneMention.hasMatch(text);
  }
  final parse = (policy['parse']! as List).cast<String>().toSet();
  final explicitUsers = (policy['users']! as List).cast<String>().toSet();
  final explicitRoles = (policy['roles']! as List).cast<String>().toSet();
  return (
    userRefs: visibleUsers
        .where((ref) => parse.contains('users') || explicitUsers.contains(ref))
        .toList()
      ..sort(),
    roleRefs: visibleRoles
        .where((ref) => parse.contains('roles') || explicitRoles.contains(ref))
        .toList()
      ..sort(),
    everyone: parse.contains('everyone') && visibleEveryone,
  );
}

bool _canonicalMobileTimestamp(Object? value) =>
    value is String &&
    RegExp(r'(?:Z|[+-][0-9]{2}:[0-9]{2})$').hasMatch(value) &&
    DateTime.tryParse(value) != null;

/// Validate the exact canonical context authenticated by rich Message v2.
Map<String, Object?> validateMobileRichMessageAuthenticatedContext(
  Object? value,
) {
  final raw = _mobileRoutingMap(value, 'Encrypted rich message context');
  final attachmentRefs = raw['message_attachment_refs'];
  final attachmentList =
      attachmentRefs is List ? attachmentRefs : const <Object?>[];
  final applicationRef = raw['application_ref'];
  final integrationType = raw['interaction_integration_type'];
  final installationRef = raw['interaction_installation_ref'];
  final installationRevision = raw['interaction_installation_revision'];
  final lineage = <Object?>[
    applicationRef,
    integrationType,
    installationRef,
    installationRevision,
  ];
  final hasLineage = lineage.any((item) => item != null);
  final operation = raw['operation'];
  final revision =
      _canonicalMobileUnsignedI63(raw['message_revision'], positive: true);
  final viewVersion =
      _canonicalMobileUnsignedI63(raw['view_version'], positive: false);
  final forwardRequired = <Object?>[
    raw['forward_snapshot_digest'],
    raw['forward_source_projection_digest'],
    raw['forwarded_channel_ref'],
    raw['forwarded_created_at'],
    raw['forwarded_flags'],
    raw['forwarded_message_ref'],
    raw['forwarded_message_type'],
  ];
  final hasForward = forwardRequired.any((item) => item != null);
  final rawForwardedCreated = raw['forwarded_created_at'];
  final rawForwardedEdited = raw['forwarded_edited_at'];
  final forwardMetadataValid = hasForward
      ? forwardRequired.every((item) => item != null) &&
          _canonicalMobileRef(raw['forwarded_message_ref']) &&
          _canonicalMobileRef(raw['forwarded_channel_ref']) &&
          raw['forward_snapshot_digest'] is String &&
          isCanonicalBase64url32(raw['forward_snapshot_digest']! as String) &&
          raw['forward_source_projection_digest'] is String &&
          isCanonicalBase64url32(
            raw['forward_source_projection_digest']! as String,
          ) &&
          _canonicalMobileTimestamp(rawForwardedCreated) &&
          (rawForwardedEdited == null ||
              _canonicalMobileTimestamp(rawForwardedEdited) &&
                  DateTime.parse(rawForwardedEdited as String).toUtc().isAfter(
                        DateTime.parse(rawForwardedCreated! as String)
                            .toUtc()
                            .subtract(const Duration(microseconds: 1)),
                      )) &&
          raw['forwarded_flags'] is int &&
          ((raw['forwarded_flags']! as int) &
                  ~((1 << 2) | (1 << 13) | (1 << 15))) ==
              0 &&
          const <int>{0, 19, 20, 23}.contains(raw['forwarded_message_type'])
      : rawForwardedEdited == null;
  if (!_hasExactMobileRoutingFields(raw, _mobileRichMessageContextFields) ||
      !_canonicalMobileRef(raw['channel_ref']) ||
      !_canonicalMobileRef(raw['author_ref']) ||
      raw['group_id'] is! String ||
      _canonicalMobileUnsignedI63(raw['policy_generation'], positive: true) ==
          null ||
      _canonicalMobileUnsignedI63(raw['epoch'], positive: false) == null ||
      raw['sender_device_id'] is! String ||
      !RegExp(r'^(?:ked|kbe|kwe)_[A-Za-z0-9_-]{43}$')
          .hasMatch(raw['sender_device_id']! as String) ||
      !const <String>{'create', 'edit'}.contains(operation) ||
      revision == null ||
      operation == 'create' &&
          (revision != '1' || raw['target_message'] != null) ||
      operation == 'edit' &&
          (BigInt.parse(revision) <= BigInt.one ||
              !_canonicalMobileRef(raw['target_message'])) ||
      !_canonicalMobileSortedRefs(attachmentRefs, 10) ||
      !_canonicalMobileSortedRefs(raw['message_mention_refs'], 5000) ||
      !_canonicalMobileSortedRefs(raw['message_mention_user_refs'], 100) ||
      !_canonicalMobileSortedRefs(raw['message_mention_role_refs'], 100) ||
      raw['message_mention_everyone'] is! bool ||
      raw['message_replied_user_ref'] != null &&
          !_canonicalMobileRef(raw['message_replied_user_ref']) ||
      !_canonicalMobileSortedRefs(raw['message_sticker_refs'], 9) ||
      !mobileCanonicalSortedCustomEmojiRefs(
        raw['message_custom_emoji_refs'],
      ) ||
      raw['referenced_message_ref'] != null &&
          !_canonicalMobileRef(raw['referenced_message_ref']) ||
      raw['rich_payload_digest'] is! String ||
      !isCanonicalBase64url32(raw['rich_payload_digest']! as String) ||
      raw['attachment_manifest_digest'] != null &&
          (raw['attachment_manifest_digest'] is! String ||
              !isCanonicalBase64url32(
                raw['attachment_manifest_digest']! as String,
              )) ||
      raw['interaction_contract_digest'] != null &&
          (raw['interaction_contract_digest'] is! String ||
              !isCanonicalBase64url32(
                raw['interaction_contract_digest']! as String,
              )) ||
      raw['forward_projection_digest'] != null &&
          (raw['forward_projection_digest'] is! String ||
              !isCanonicalBase64url32(
                raw['forward_projection_digest']! as String,
              )) ||
      (raw['forward_projection_digest'] == null
          ? raw['forward_projection_version'] != null
          : raw['forward_projection_version'] != 2) ||
      !forwardMetadataValid ||
      attachmentList.isNotEmpty !=
          (raw['attachment_manifest_digest'] != null) ||
      raw['message_flags'] is! int ||
      (raw['message_flags']! as int) < 0 ||
      (raw['message_flags']! as int) > 2147483647 ||
      raw['tts'] is! bool ||
      raw['voice_message'] is! bool ||
      raw['tts'] == true && raw['voice_message'] == true ||
      raw['voice_message'] == true && attachmentList.length != 1 ||
      viewVersion == null ||
      raw['view_persistent'] is! bool ||
      (hasLineage
          ? lineage.any((item) => item == null) ||
              !_canonicalMobileRef(applicationRef) ||
              !_canonicalMobileRef(installationRef) ||
              !const <String>{'guild_install', 'user_install', 'dm_capability'}
                  .contains(integrationType) ||
              _canonicalMobileUnsignedI63(
                    installationRevision,
                    positive: true,
                  ) ==
                  null
          : lineage.any((item) => item != null))) {
    throw const FormatException('Encrypted rich message context is invalid.');
  }
  return Map<String, Object?>.unmodifiable(
    Map<String, Object?>.from(
      _canonicalInteractionValue(raw, <Object>{})! as Map,
    ),
  );
}

Uint8List mobileRichMessageAuthenticatedData(Map<String, Object?> context) =>
    mobileCanonicalInteractionJson(<String, Object?>{
      'context': context,
      'purpose': 'kaede.message.rich.v1',
    });

Future<String> mobileRichMessagePayloadDigest(
  Map<String, Object?> data,
) async {
  final encoded = mobileCanonicalInteractionJson(data);
  try {
    return _base64url((await Sha256().hash(encoded)).bytes);
  } finally {
    encoded.fillRange(0, encoded.length, 0);
  }
}

Map<String, Object?> _mobileRoutingMap(Object? value, String label) {
  if (value is! Map || value.keys.any((key) => key is! String)) {
    throw FormatException('$label is invalid.');
  }
  return Map<String, Object?>.from(value);
}

bool _hasExactMobileRoutingFields(
  Map<String, Object?> value,
  Set<String> fields,
) =>
    value.length == fields.length && value.keys.toSet().containsAll(fields);

String _mobileRoutingText(Object? value, String label) {
  if (value is! String || value.runes.isEmpty || value.runes.length > 100) {
    throw FormatException('$label is invalid.');
  }
  return value;
}

int _mobileRoutingInt(
  Object? value,
  String label, {
  required int minimum,
  required int maximum,
}) {
  if (value is! int || value < minimum || value > maximum) {
    throw FormatException('$label is invalid.');
  }
  return value;
}

List<String> _validateMobileRoutingOptionDigests(
  Object? value, {
  required int maximum,
}) {
  if (value is! List || value.isEmpty || value.length > maximum) {
    throw const FormatException(
      'Interaction routing option digests are invalid.',
    );
  }
  final digests = <String>[];
  for (final item in value) {
    if (item is! String || !isCanonicalBase64url32(item)) {
      throw const FormatException(
        'Interaction routing option digest is invalid.',
      );
    }
    digests.add(item);
  }
  final sorted = [...digests]..sort();
  if (digests.toSet().length != digests.length ||
      !_listEquals(digests, sorted)) {
    throw const FormatException(
      'Interaction routing option digests must be sorted and unique.',
    );
  }
  return digests;
}

Future<List<String>> _mobileRoutingOptionDigests(
  Object? value, {
  required int maximum,
}) async {
  if (value is! List || value.isEmpty || value.length > maximum) {
    throw const FormatException('Interaction routing options are invalid.');
  }
  final values = value
      .map(
        (item) => _mobileRoutingText(
          _mobileRoutingMap(item, 'Interaction routing option')['value'],
          'Interaction routing option value',
        ),
      )
      .toList(growable: false);
  if (values.toSet().length != values.length) {
    throw const FormatException(
      'Interaction routing option values must be unique.',
    );
  }
  final digests = await Future.wait(
    values.map((value) async {
      final bytes = Uint8List.fromList(utf8.encode(value));
      try {
        return _base64url((await Sha256().hash(bytes)).bytes);
      } finally {
        bytes.fillRange(0, bytes.length, 0);
      }
    }),
  );
  digests.sort();
  return digests;
}

void _validateMobileRoutingControl(Object? value, {required bool modal}) {
  final raw = _mobileRoutingMap(value, 'Interaction routing control');
  final type = _mobileRoutingInt(
    raw['type'],
    'Interaction routing control type',
    minimum: 0,
    maximum: 2147483647,
  );
  final allowed = <int>{
    3,
    5,
    6,
    7,
    8,
    ...(modal ? <int>{4, 19, 21, 22, 23} : <int>{2})
  };
  if (!allowed.contains(type)) {
    throw const FormatException(
      'Interaction routing control type is invalid.',
    );
  }
  _mobileRoutingText(raw['custom_id'], 'Interaction routing custom ID');
  if (type == 2) {
    if (!_hasExactMobileRoutingFields(
          raw,
          const <String>{'type', 'custom_id', 'disabled'},
        ) ||
        raw['disabled'] is! bool) {
      throw const FormatException('Interaction routing button is invalid.');
    }
    return;
  }
  if (const <int>{3, 5, 6, 7, 8}.contains(type)) {
    final fields = <String>{
      'type',
      'custom_id',
      'disabled',
      'min_values',
      'max_values',
      if (modal) 'required',
      if (type == 3) 'option_value_digests',
      if (type == 8) 'channel_types',
    };
    final minimum = _mobileRoutingInt(
      raw['min_values'],
      'Interaction routing minimum values',
      minimum: 0,
      maximum: 25,
    );
    final maximum = _mobileRoutingInt(
      raw['max_values'],
      'Interaction routing maximum values',
      minimum: minimum,
      maximum: 25,
    );
    if (!_hasExactMobileRoutingFields(raw, fields) ||
        raw['disabled'] is! bool ||
        (modal && raw['required'] is! bool) ||
        (modal && raw['disabled'] == true) ||
        (modal && raw['required'] == true && minimum == 0)) {
      throw const FormatException('Interaction routing select is invalid.');
    }
    if (type == 3 &&
        maximum >
            _validateMobileRoutingOptionDigests(
              raw['option_value_digests'],
              maximum: 25,
            ).length) {
      throw const FormatException(
        'Interaction routing select range is invalid.',
      );
    }
    if (type == 8) {
      final channelTypes = raw['channel_types'];
      if (channelTypes is! List ||
          channelTypes.length > 19 ||
          channelTypes.any(
            (item) => item is! int || item < 0 || item > 2147483647,
          ) ||
          channelTypes.toSet().length != channelTypes.length) {
        throw const FormatException(
          'Interaction routing channel filter is invalid.',
        );
      }
    }
    return;
  }
  if (type == 4) {
    final minimum = _mobileRoutingInt(
      raw['min_length'],
      'Interaction routing minimum length',
      minimum: 0,
      maximum: 4000,
    );
    _mobileRoutingInt(
      raw['max_length'],
      'Interaction routing maximum length',
      minimum: minimum,
      maximum: 4000,
    );
    if (!_hasExactMobileRoutingFields(
          raw,
          const <String>{
            'type',
            'custom_id',
            'required',
            'min_length',
            'max_length',
          },
        ) ||
        raw['required'] is! bool) {
      throw const FormatException(
        'Interaction routing text input is invalid.',
      );
    }
    return;
  }
  if (type == 19) {
    final minimum = _mobileRoutingInt(
      raw['min_values'],
      'Interaction routing minimum files',
      minimum: 0,
      maximum: 10,
    );
    _mobileRoutingInt(
      raw['max_values'],
      'Interaction routing maximum files',
      minimum: minimum,
      maximum: 10,
    );
    final fileTypes = raw['file_types'];
    if (!_hasExactMobileRoutingFields(
          raw,
          const <String>{
            'type',
            'custom_id',
            'required',
            'min_values',
            'max_values',
            'file_types',
          },
        ) ||
        raw['required'] is! bool ||
        (raw['required'] == true && minimum == 0) ||
        fileTypes is! List ||
        fileTypes.length > 10 ||
        fileTypes.any(
          (item) => item is! String || item.isEmpty || item.runes.length > 100,
        ) ||
        fileTypes.toSet().length != fileTypes.length) {
      throw const FormatException(
        'Interaction routing file input is invalid.',
      );
    }
    return;
  }
  if (type == 21 || type == 22) {
    final fields = <String>{
      'type',
      'custom_id',
      'required',
      'option_value_digests',
      if (type == 22) ...<String>{'min_values', 'max_values'},
    };
    final options = _validateMobileRoutingOptionDigests(
      raw['option_value_digests'],
      maximum: 10,
    );
    if (!_hasExactMobileRoutingFields(raw, fields) ||
        raw['required'] is! bool) {
      throw const FormatException(
        'Interaction routing choice input is invalid.',
      );
    }
    if (type == 22) {
      final minimum = _mobileRoutingInt(
        raw['min_values'],
        'Interaction routing minimum choices',
        minimum: 0,
        maximum: options.length,
      );
      _mobileRoutingInt(
        raw['max_values'],
        'Interaction routing maximum choices',
        minimum: minimum,
        maximum: options.length,
      );
      if (raw['required'] == true && minimum == 0) {
        throw const FormatException(
          'Interaction routing required choices are invalid.',
        );
      }
    }
    return;
  }
  if (!_hasExactMobileRoutingFields(
    raw,
    const <String>{'type', 'custom_id'},
  )) {
    throw const FormatException('Interaction routing checkbox is invalid.');
  }
}

/// Validate the only public metadata permitted beside encrypted rich content.
Map<String, Object?> validateMobileInteractionRoutingContract(
  Object? value,
  int? callbackType,
) {
  final raw = _mobileRoutingMap(value, 'Interaction routing contract');
  if (raw['version'] != 1) {
    throw const FormatException('Interaction routing contract is invalid.');
  }
  if (raw['kind'] == 'message') {
    final components = raw['components'];
    final hasPoll = raw.containsKey('poll');
    if (!const <int?>{null, 4, 7}.contains(callbackType) ||
        !_hasExactMobileRoutingFields(
          raw,
          <String>{
            'version',
            'kind',
            'view_timeout_seconds',
            'components',
            if (hasPoll) 'poll',
          },
        ) ||
        components is! List ||
        components.isEmpty && !hasPoll ||
        components.length > 40) {
      throw const FormatException(
        'Interaction message routing contract is invalid.',
      );
    }
    _mobileRoutingInt(
      raw['view_timeout_seconds'],
      'Interaction view timeout',
      minimum: 1,
      maximum: 86400,
    );
    final customIds = <String>[];
    for (final component in components) {
      _validateMobileRoutingControl(component, modal: false);
      customIds.add('${(component as Map)['custom_id']}');
    }
    if (customIds.toSet().length != customIds.length) {
      throw const FormatException(
        'Interaction routing custom IDs must be unique.',
      );
    }
    if (hasPoll) _validateMobileRoutingPoll(raw['poll']);
  } else if (raw['kind'] == 'modal') {
    final components = raw['components'];
    if (callbackType != 9 ||
        !_hasExactMobileRoutingFields(
          raw,
          const <String>{'version', 'kind', 'custom_id', 'components'},
        ) ||
        components is! List ||
        components.isEmpty ||
        components.length > 5) {
      throw const FormatException(
        'Interaction modal routing contract is invalid.',
      );
    }
    _mobileRoutingText(raw['custom_id'], 'Modal custom ID');
    final customIds = <String>[];
    for (final value in components) {
      final row = _mobileRoutingMap(value, 'Interaction modal routing row');
      late final Object? field;
      if (row['type'] == 1) {
        final fields = row['components'];
        if (!_hasExactMobileRoutingFields(
              row,
              const <String>{'type', 'components'},
            ) ||
            fields is! List ||
            fields.length != 1) {
          throw const FormatException(
            'Interaction modal routing row is invalid.',
          );
        }
        field = fields.single;
      } else if (row['type'] == 18) {
        if (!_hasExactMobileRoutingFields(
          row,
          const <String>{'type', 'component'},
        )) {
          throw const FormatException(
            'Interaction modal routing row is invalid.',
          );
        }
        field = row['component'];
      } else {
        throw const FormatException(
          'Interaction modal routing row is invalid.',
        );
      }
      _validateMobileRoutingControl(field, modal: true);
      customIds.add('${(field as Map)['custom_id']}');
    }
    if (customIds.toSet().length != customIds.length) {
      throw const FormatException(
        'Interaction routing custom IDs must be unique.',
      );
    }
  } else {
    throw const FormatException(
      'Interaction routing contract kind is invalid.',
    );
  }
  return Map<String, Object?>.unmodifiable(
    Map<String, Object?>.from(
      _canonicalInteractionValue(raw, <Object>{})! as Map,
    ),
  );
}

Map<String, Object?> _validateMobileRoutingPoll(Object? value) {
  final raw = _mobileRoutingMap(value, 'Encrypted poll routing contract');
  final answerIds = raw['answer_ids'];
  final duration = _mobileRoutingInt(
    raw['duration_seconds'],
    'Encrypted poll duration',
    minimum: 3600,
    maximum: 2764800,
  );
  if (!_hasExactMobileRoutingFields(
        raw,
        const <String>{
          'version',
          'answer_ids',
          'allow_multiselect',
          'duration_seconds',
          'layout_type',
        },
      ) ||
      raw['version'] != 1 ||
      answerIds is! List ||
      answerIds.length < 2 ||
      answerIds.length > 10 ||
      answerIds.asMap().entries.any((item) => item.value != item.key + 1) ||
      raw['allow_multiselect'] is! bool ||
      duration % 3600 != 0 ||
      raw['layout_type'] != 1) {
    throw const FormatException('Encrypted poll routing contract is invalid.');
  }
  return Map<String, Object?>.unmodifiable(
    Map<String, Object?>.from(
      _canonicalInteractionValue(raw, <Object>{})! as Map,
    ),
  );
}

Map<String, Object?> _mobileRoutingPoll(Object? value) {
  final raw = _mobileRoutingMap(value, 'Encrypted poll');
  final answers = raw['answers'];
  final durationHours = _mobileRoutingInt(
    raw['duration'],
    'Encrypted poll duration',
    minimum: 1,
    maximum: 768,
  );
  if (answers is! List ||
      answers.length < 2 ||
      answers.length > 10 ||
      raw['allow_multiselect'] is! bool ||
      raw['layout_type'] != 1) {
    throw const FormatException('Encrypted poll is invalid.');
  }
  return _validateMobileRoutingPoll(<String, Object?>{
    'version': 1,
    'answer_ids': <int>[
      for (var index = 0; index < answers.length; index++) index + 1,
    ],
    'allow_multiselect': raw['allow_multiselect'],
    'duration_seconds': durationHours * 3600,
    'layout_type': 1,
  });
}

Future<Map<String, Object?>?> _mobileRoutingControl(
  Object? value, {
  required bool modal,
}) async {
  final raw = _mobileRoutingMap(value, 'Interaction routing control');
  final type = _mobileRoutingInt(
    raw['type'],
    'Interaction routing control type',
    minimum: 0,
    maximum: 2147483647,
  );
  if (type == 2 && raw['custom_id'] == null) return null;
  final allowed = <int>{
    3,
    5,
    6,
    7,
    8,
    ...(modal ? <int>{4, 19, 21, 22, 23} : <int>{2})
  };
  if (!allowed.contains(type)) return null;
  final customId =
      _mobileRoutingText(raw['custom_id'], 'Interaction routing custom ID');
  if (type == 2) {
    if (raw.containsKey('disabled') && raw['disabled'] is! bool) {
      throw const FormatException(
        'Interaction routing button state is invalid.',
      );
    }
    return <String, Object?>{
      'type': 2,
      'custom_id': customId,
      'disabled': raw['disabled'] ?? false,
    };
  }
  if (const <int>{3, 5, 6, 7, 8}.contains(type)) {
    final minimum = _mobileRoutingInt(
      raw.containsKey('min_values') ? raw['min_values'] : 1,
      'Interaction routing minimum values',
      minimum: 0,
      maximum: 25,
    );
    final maximum = _mobileRoutingInt(
      raw.containsKey('max_values') ? raw['max_values'] : 1,
      'Interaction routing maximum values',
      minimum: minimum,
      maximum: 25,
    );
    if (raw.containsKey('disabled') && raw['disabled'] is! bool) {
      throw const FormatException(
        'Interaction routing select state is invalid.',
      );
    }
    final disabled = raw['disabled'] ?? false;
    final required = raw['required'] != false;
    if (modal && (disabled == true || required && minimum == 0)) {
      throw const FormatException(
        'Interaction routing modal select state is invalid.',
      );
    }
    final result = <String, Object?>{
      'type': type,
      'custom_id': customId,
      'disabled': disabled,
      'min_values': minimum,
      'max_values': maximum,
      if (modal) 'required': required,
    };
    if (type == 3) {
      final digests = await _mobileRoutingOptionDigests(
        raw['options'],
        maximum: 25,
      );
      if (maximum > digests.length) {
        throw const FormatException(
          'Interaction routing select range is invalid.',
        );
      }
      result['option_value_digests'] = digests;
    }
    if (type == 8) {
      final channelTypes = raw.containsKey('channel_types')
          ? raw['channel_types']
          : const <int>[];
      if (channelTypes is! List ||
          channelTypes.length > 19 ||
          channelTypes.any(
            (item) => item is! int || item < 0 || item > 2147483647,
          ) ||
          channelTypes.toSet().length != channelTypes.length) {
        throw const FormatException(
          'Interaction routing channel types are invalid.',
        );
      }
      result['channel_types'] = List<Object?>.from(channelTypes);
    }
    return result;
  }
  if (type == 4) {
    final minimum = _mobileRoutingInt(
      raw['min_length'] ?? 0,
      'Interaction routing minimum length',
      minimum: 0,
      maximum: 4000,
    );
    final maximum = _mobileRoutingInt(
      raw['max_length'] ?? 4000,
      'Interaction routing maximum length',
      minimum: 1,
      maximum: 4000,
    );
    if (minimum > maximum) {
      throw const FormatException(
        'Interaction routing text length range is invalid.',
      );
    }
    return <String, Object?>{
      'type': 4,
      'custom_id': customId,
      'required': raw['required'] != false,
      'min_length': minimum,
      'max_length': maximum,
    };
  }
  if (type == 19) {
    final fileTypes =
        raw.containsKey('file_types') ? raw['file_types'] : const <String>[];
    if (fileTypes is! List ||
        fileTypes.length > 10 ||
        fileTypes.any(
          (item) => item is! String || item.isEmpty || item.runes.length > 100,
        ) ||
        fileTypes.toSet().length != fileTypes.length) {
      throw const FormatException(
        'Interaction routing file types are invalid.',
      );
    }
    final minimum = _mobileRoutingInt(
      raw.containsKey('min_values') ? raw['min_values'] : 1,
      'Interaction routing minimum files',
      minimum: 0,
      maximum: 10,
    );
    final maximum = _mobileRoutingInt(
      raw.containsKey('max_values') ? raw['max_values'] : 1,
      'Interaction routing maximum files',
      minimum: 1,
      maximum: 10,
    );
    final required = raw['required'] != false;
    if (minimum > maximum || required && minimum == 0) {
      throw const FormatException(
        'Interaction routing file range is invalid.',
      );
    }
    return <String, Object?>{
      'type': 19,
      'custom_id': customId,
      'required': required,
      'min_values': minimum,
      'max_values': maximum,
      'file_types': List<Object?>.from(fileTypes),
    };
  }
  if (type == 21 || type == 22) {
    final digests = await _mobileRoutingOptionDigests(
      raw['options'],
      maximum: 10,
    );
    final required = raw['required'] != false;
    final result = <String, Object?>{
      'type': type,
      'custom_id': customId,
      'required': required,
      'option_value_digests': digests,
    };
    if (type == 22) {
      final minimum = _mobileRoutingInt(
        raw.containsKey('min_values') ? raw['min_values'] : 1,
        'Interaction routing minimum choices',
        minimum: 0,
        maximum: digests.length,
      );
      final maximum = _mobileRoutingInt(
        raw.containsKey('max_values') ? raw['max_values'] : digests.length,
        'Interaction routing maximum choices',
        minimum: minimum,
        maximum: digests.length,
      );
      if (required && minimum == 0) {
        throw const FormatException(
          'Interaction routing required choices are invalid.',
        );
      }
      result['min_values'] = minimum;
      result['max_values'] = maximum;
    }
    return result;
  }
  return <String, Object?>{'type': 23, 'custom_id': customId};
}

List<Map<String, Object?>> _mobileRoutingControlNodes(
  Object? value,
  Set<Object> seen, [
  int depth = 0,
]) {
  if (depth > 8) {
    throw const FormatException(
      'Interaction routing components are too deeply nested.',
    );
  }
  final raw = _mobileRoutingMap(value, 'Interaction component');
  if (!seen.add(value as Object)) {
    throw const FormatException(
      'Interaction routing components cannot be recursive.',
    );
  }
  try {
    final result = <Map<String, Object?>>[raw];
    if (raw.containsKey('components')) {
      final children = raw['components'];
      if (children is! List) {
        throw const FormatException(
          'Interaction component children are invalid.',
        );
      }
      for (final child in children) {
        result.addAll(_mobileRoutingControlNodes(child, seen, depth + 1));
      }
    }
    for (final key in const <String>['component', 'accessory']) {
      if (raw[key] != null) {
        result.addAll(_mobileRoutingControlNodes(raw[key], seen, depth + 1));
      }
    }
    return result;
  } finally {
    seen.remove(value);
  }
}

/// Derive the privacy-preserving routing contract from decrypted content.
Future<Map<String, Object?>?> mobileInteractionRoutingContract(
  Map<String, Object?> data,
  int? callbackType,
) async {
  if (callbackType == 8) return null;
  if (callbackType == 9) {
    final customId = _mobileRoutingText(data['custom_id'], 'Modal custom ID');
    final components = data['components'];
    if (components is! List) {
      throw const FormatException('Modal components are invalid.');
    }
    final rows = <Map<String, Object?>>[];
    final customIds = <String>[];
    for (final value in components) {
      final row = _mobileRoutingMap(value, 'Modal row');
      if (row['type'] == 10) continue;
      late final Map<String, Object?>? field;
      if (row['type'] == 1) {
        final fields = row['components'];
        if (fields is! List || fields.length != 1) {
          throw const FormatException('Modal row is invalid.');
        }
        field = await _mobileRoutingControl(fields.single, modal: true);
        if (field == null) {
          throw const FormatException('Modal input is invalid.');
        }
        rows.add(<String, Object?>{
          'type': 1,
          'components': <Object?>[field],
        });
      } else if (row['type'] == 18) {
        field = await _mobileRoutingControl(row['component'], modal: true);
        if (field == null) {
          throw const FormatException('Modal input is invalid.');
        }
        rows.add(<String, Object?>{'type': 18, 'component': field});
      } else {
        throw const FormatException('Modal row is invalid.');
      }
      customIds.add('${field['custom_id']}');
    }
    if (rows.isEmpty ||
        rows.length > 5 ||
        customIds.toSet().length != customIds.length) {
      throw const FormatException('Modal routing contract is invalid.');
    }
    return validateMobileInteractionRoutingContract(
      <String, Object?>{
        'version': 1,
        'kind': 'modal',
        'custom_id': customId,
        'components': rows,
      },
      callbackType,
    );
  }
  if (callbackType != null && callbackType != 4 && callbackType != 7) {
    throw const FormatException(
      'Interaction routing callback type is invalid.',
    );
  }
  final components = data['components'] ?? const <Object?>[];
  if (components is! List) {
    throw const FormatException('Message components are invalid.');
  }
  final controls = <Map<String, Object?>>[];
  for (final layout in components) {
    for (final value in _mobileRoutingControlNodes(layout, <Object>{})) {
      final control = await _mobileRoutingControl(value, modal: false);
      if (control != null) controls.add(control);
    }
  }
  final poll = data['poll'] == null ? null : _mobileRoutingPoll(data['poll']);
  if (controls.isEmpty && poll == null) return null;
  final customIds = controls.map((item) => '${item['custom_id']}').toList();
  if (customIds.toSet().length != customIds.length) {
    throw const FormatException(
      'Interaction routing custom IDs must be unique.',
    );
  }
  return validateMobileInteractionRoutingContract(
    <String, Object?>{
      'version': 1,
      'kind': 'message',
      'view_timeout_seconds': _mobileRoutingInt(
        data.containsKey('view_timeout_seconds')
            ? data['view_timeout_seconds']
            : 900,
        'Interaction view timeout',
        minimum: 1,
        maximum: 86400,
      ),
      'components': controls,
      if (poll != null) 'poll': poll,
    },
    callbackType,
  );
}

Future<String> mobileInteractionRoutingContractDigest(
  Map<String, Object?> contract,
) async {
  final bytes = mobileCanonicalInteractionJson(contract);
  try {
    return _base64url((await Sha256().hash(bytes)).bytes);
  } finally {
    bytes.fillRange(0, bytes.length, 0);
  }
}

Future<void> _validateMobileInteractionRoutingContractForData(
  Map<String, Object?> data,
  int callbackType,
  Map<String, Object?>? expectedContract,
  String? expectedDigest,
) async {
  final derived = await mobileInteractionRoutingContract(data, callbackType);
  if (derived == null || expectedContract == null || expectedDigest == null) {
    if (derived != null || expectedContract != null || expectedDigest != null) {
      throw const FormatException(
        'Encrypted bot response routing contract does not match its content.',
      );
    }
    return;
  }
  final expected = mobileCanonicalInteractionJson(expectedContract);
  final actual = mobileCanonicalInteractionJson(derived);
  try {
    if (!_constantTimeEquals(expected, actual) ||
        await mobileInteractionRoutingContractDigest(derived) !=
            expectedDigest) {
      throw const FormatException(
        'Encrypted bot response routing contract does not match its content.',
      );
    }
  } finally {
    expected.fillRange(0, expected.length, 0);
    actual.fillRange(0, actual.length, 0);
  }
}

String? _optionalInteractionInteger(Object? value, String label) {
  if (value == null) return null;
  if (value is int && value < 1) {
    throw FormatException('Encrypted interaction $label is invalid.');
  }
  if (value is! int && value is! String) {
    throw FormatException('Encrypted interaction $label is invalid.');
  }
  final rendered = '$value';
  if (!RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(rendered) ||
      BigInt.parse(rendered) > BigInt.parse('9223372036854775807')) {
    throw FormatException('Encrypted interaction $label is invalid.');
  }
  return rendered;
}

List<String> _canonicalInteractionAttachmentIds(Iterable<String> values) {
  final result = <String>[];
  for (final value in values) {
    if (!RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(value) ||
        BigInt.parse(value) > BigInt.parse('9223372036854775807')) {
      throw const FormatException(
        'Encrypted interaction attachment ID is invalid.',
      );
    }
    if (result.contains(value)) {
      throw const FormatException(
        'Encrypted interaction attachment IDs must be unique.',
      );
    }
    result.add(value);
  }
  if (result.length > 10) {
    throw const FormatException(
      'Encrypted interactions accept at most 10 files.',
    );
  }
  result
      .sort((left, right) => BigInt.parse(left).compareTo(BigInt.parse(right)));
  return result;
}

Map<String, Object?> mobileInteractionAuthenticatedContext(
  KaedeChannel channel, {
  required EntityRef invoker,
  required String senderDeviceId,
  required EntityRef application,
  required String integrationType,
  required String interactionContext,
  required String interactionType,
  String? commandId,
  String? commandName,
  String? commandType,
  Object? componentType,
  String? customId,
  EntityRef? message,
  Object? responseId,
  EntityRef? target,
  Object? viewVersion,
  Object? autocompleteGeneration,
  String? focusedOption,
  Iterable<String> attachmentIds = const <String>[],
}) {
  if (!const {'guild_install', 'user_install', 'dm_capability'}
          .contains(integrationType) ||
      !const {'guild', 'bot_dm', 'private_channel'}
          .contains(interactionContext) ||
      !const {'command', 'autocomplete', 'component', 'modal_submit'}
          .contains(interactionType)) {
    throw const FormatException(
      'Encrypted interaction authority projection is invalid.',
    );
  }
  final files = _canonicalInteractionAttachmentIds(attachmentIds);
  final commandInteraction =
      const {'command', 'autocomplete'}.contains(interactionType);
  if ((commandInteraction &&
          (commandId == null ||
              !RegExp(r'^[1-9]\d{0,18}$').hasMatch(commandId))) ||
      (!commandInteraction && commandId != null)) {
    throw const FormatException(
      'Encrypted interaction command identity is invalid.',
    );
  }
  return <String, Object?>{
    'application_ref': application.wire,
    'attachment_ids': files,
    'autocomplete_generation': _optionalInteractionInteger(
      autocompleteGeneration,
      'autocomplete generation',
    ),
    'channel_ref': channel.ref.wire,
    'command_id': commandId,
    'command_name': commandName,
    'command_type': commandType,
    'component_type': componentType,
    'context': interactionContext,
    'custom_id': customId,
    'epoch': '${channel.encryptionEpoch}',
    'focused_option': focusedOption,
    'group_id': channel.encryptionGroupId,
    'integration_type': integrationType,
    'interaction_type': interactionType,
    'invoker_ref': invoker.wire,
    'message_ref': message?.wire,
    'policy_generation': '${channel.encryptionPolicyGeneration}',
    'response_id': _optionalInteractionInteger(responseId, 'response ID'),
    'sender_device_id': senderDeviceId,
    'target_ref': target?.wire,
    'view_version': _optionalInteractionInteger(viewVersion, 'view version'),
  };
}

Map<String, Map<String, Object?>> _interactionAttachmentManifests(
  List<String> attachmentIds,
  Map<String, Map<String, Object?>> values,
) {
  if (values.length != attachmentIds.length ||
      values.keys.any((key) => !attachmentIds.contains(key))) {
    throw const FormatException(
      'Encrypted interaction file manifests must match the uploaded files exactly.',
    );
  }
  return <String, Map<String, Object?>>{
    for (final attachmentId in attachmentIds)
      attachmentId: _interactionAttachmentManifest(
        attachmentId,
        values[attachmentId],
      ),
  };
}

Map<String, Object?> _interactionAttachmentManifest(
  String attachmentId,
  Map<String, Object?>? value, {
  bool allowVoiceMetadata = false,
}) {
  const baseFields = <String>{
    'attachment_domain',
    'attachment_id',
    'chunk_size',
    'ciphertext_sha256',
    'ciphertext_size',
    'content_type',
    'file_id',
    'filename',
    'key',
    'plaintext_sha256',
    'plaintext_size',
    'protocol',
    'version',
  };
  final hasDuration = value?.containsKey('duration_millis') == true;
  final hasWaveform = value?.containsKey('waveform') == true;
  final voiceMetadata = hasDuration && hasWaveform;
  final fields = <String>{
    ...baseFields,
    if (voiceMetadata) ...const <String>{'duration_millis', 'waveform'},
  };
  if (value == null ||
      hasDuration != hasWaveform ||
      voiceMetadata && !allowVoiceMetadata ||
      value.length != fields.length ||
      !value.keys.toSet().containsAll(fields) ||
      value['version'] != 1 ||
      value['protocol'] != 'kaede-file-v1' ||
      value['attachment_id'] != attachmentId ||
      value['attachment_domain'] is! String) {
    throw const FormatException(
      'Encrypted interaction file manifest authority is invalid.',
    );
  }
  final filename = '${value['filename'] ?? ''}'.trim();
  final contentType = '${value['content_type'] ?? ''}'.toLowerCase();
  final plaintextSize = value['plaintext_size'];
  final ciphertextSize = value['ciphertext_size'];
  final chunkSize = value['chunk_size'];
  final fileId = value['file_id'];
  final key = value['key'];
  final digest = value['ciphertext_sha256'];
  final plaintextDigest = value['plaintext_sha256'];
  if (filename.isEmpty ||
      value['filename'] is! String ||
      filename != value['filename'] ||
      filename.length > 255 ||
      filename.runes.any((code) => code <= 0x1f || code == 0x7f) ||
      contentType.length > 100 ||
      value['content_type'] is! String ||
      contentType != value['content_type'] ||
      !RegExp(r'^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$')
          .hasMatch(contentType) ||
      plaintextSize is! int ||
      plaintextSize < 1 ||
      plaintextSize > 64 * 1024 * 1024 ||
      ciphertextSize is! int ||
      chunkSize is! int ||
      chunkSize < 64 * 1024 ||
      chunkSize > 1024 * 1024 ||
      ciphertextSize !=
          plaintextSize +
              41 +
              ((plaintextSize + chunkSize - 1) ~/ chunkSize) * 20 ||
      fileId is! String ||
      !RegExp(r'^[A-Za-z0-9_-]{21}[AQgw]$').hasMatch(fileId) ||
      key is! String ||
      !isCanonicalBase64url32(key) ||
      digest is! String ||
      !isCanonicalBase64url32(digest) ||
      plaintextDigest is! String ||
      !isCanonicalBase64url32(plaintextDigest)) {
    throw const FormatException(
      'Encrypted interaction file manifest is invalid.',
    );
  }
  Uint8List? waveformBytes;
  if (voiceMetadata) {
    final durationMillis = value['duration_millis'];
    final waveform = value['waveform'];
    try {
      if (durationMillis is! int ||
          durationMillis < 1 ||
          durationMillis > 1200000 ||
          waveform is! String ||
          waveform.length < 4 ||
          waveform.length > 344 ||
          !RegExp(
            r'^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$',
          ).hasMatch(waveform)) {
        throw const FormatException(
          'Encrypted voice message metadata is invalid.',
        );
      }
      waveformBytes = base64.decode(waveform);
      if (waveformBytes.isEmpty ||
          waveformBytes.length > 256 ||
          base64.encode(waveformBytes) != waveform) {
        throw const FormatException(
          'Encrypted voice message metadata is invalid.',
        );
      }
    } on FormatException {
      throw const FormatException(
        'Encrypted voice message metadata is invalid.',
      );
    } finally {
      waveformBytes?.fillRange(0, waveformBytes.length, 0);
    }
  }
  final domain = Domain('${value['attachment_domain']}');
  return <String, Object?>{
    'version': 1,
    'protocol': 'kaede-file-v1',
    'file_id': fileId,
    'key': key,
    'filename': filename,
    'content_type': contentType,
    'plaintext_size': plaintextSize,
    'plaintext_sha256': plaintextDigest,
    'ciphertext_size': ciphertextSize,
    'ciphertext_sha256': digest,
    'chunk_size': chunkSize,
    'attachment_id': attachmentId,
    'attachment_domain': domain.value,
    if (voiceMetadata) ...<String, Object?>{
      'duration_millis': value['duration_millis'],
      'waveform': value['waveform'],
    },
  };
}

/// Strict manifest validator shared by rich-message send and receive paths.
List<Map<String, Object?>> validateMobileEncryptedRichMessageAttachments(
  Object? value, {
  required bool voiceMessage,
}) {
  if (value is! List ||
      value.length > 10 ||
      value.any(
        (item) => item is! Map || item.keys.any((key) => key is! String),
      )) {
    throw const FormatException(
      'Encrypted rich message attachment manifests are invalid.',
    );
  }
  final attachments = <Map<String, Object?>>[];
  final refs = <String>{};
  for (final item in value) {
    final raw = Map<String, Object?>.from(item as Map);
    final id = raw['attachment_id'];
    final manifest = _interactionAttachmentManifest(
      id is String ? id : '',
      raw,
      allowVoiceMetadata: true,
    );
    final ref = '${manifest['attachment_id']}@${manifest['attachment_domain']}';
    if (!refs.add(ref)) {
      throw const FormatException(
        'Encrypted rich message attachment identity is duplicated.',
      );
    }
    attachments.add(manifest);
  }
  final voiceManifest = attachments.firstOrNull;
  if (voiceMessage
      ? attachments.length != 1 ||
          voiceManifest == null ||
          voiceManifest['duration_millis'] is! int ||
          voiceManifest['waveform'] is! String ||
          !('${voiceManifest['content_type']}').startsWith('audio/')
      : attachments.any(
          (manifest) =>
              manifest.containsKey('duration_millis') ||
              manifest.containsKey('waveform'),
        )) {
    throw const FormatException(
      'Encrypted voice message metadata does not match its authenticated body.',
    );
  }
  return List<Map<String, Object?>>.unmodifiable(attachments);
}

({List<String> refs, List<Map<String, Object?>> transport})
    _mobileResponseAttachmentTransport(
  List<Object?> values, {
  required Domain authority,
  required String interactionRef,
  required String responseRef,
}) {
  if (values.length > 10) {
    throw const FormatException(
      'Encrypted bot response has too many attachments.',
    );
  }
  final refs = <String>{};
  final transport = <Map<String, Object?>>[];
  for (final value in values) {
    if (value is! Map) {
      throw const FormatException(
        'Encrypted bot response attachment transport is invalid.',
      );
    }
    final item = Map<String, Object?>.from(value);
    late final EntityRef ref;
    try {
      ref = EntityRef(
        Snowflake('${item['id'] ?? ''}'),
        Domain('${item['origin_domain'] ?? ''}'),
      );
    } on FormatException {
      throw const FormatException(
        'Encrypted bot response attachment identity is invalid.',
      );
    }
    if (ref.domain != authority ||
        !refs.add(ref.wire) ||
        item['private_media_url'] !=
            '/api/v1/interactions/$interactionRef/responses/$responseRef/attachments/${ref.wire}') {
      throw const FormatException(
        'Encrypted bot response attachment capability is invalid.',
      );
    }
    transport.add(item);
  }
  final sorted = refs.toList()..sort();
  return (refs: sorted, transport: transport);
}

void _validateMobileInteractionResponsePlaintext(
  Map<String, Object?> data,
  int callbackType,
) {
  final encoded = mobileCanonicalInteractionJson(data);
  try {
    if (encoded.length > 64 * 1024) {
      throw const FormatException('Encrypted bot response body is too large.');
    }
  } finally {
    encoded.fillRange(0, encoded.length, 0);
  }
  if (callbackType == 8) {
    final choices = data['choices'];
    if (data.length != 1 || choices is! List || choices.length > 25) {
      throw const FormatException(
          'Encrypted autocomplete response is invalid.');
    }
    for (final raw in choices) {
      if (raw is! Map ||
          raw.length != 2 ||
          !raw.containsKey('name') ||
          !raw.containsKey('value')) {
        throw const FormatException(
            'Encrypted autocomplete response is invalid.');
      }
      final name = raw['name'];
      final value = raw['value'];
      if (name is! String ||
          name.isEmpty ||
          name.length > 100 ||
          !((value is String && value.isNotEmpty && value.length <= 100) ||
              (value is num && value.isFinite && value.abs() <= 1e308))) {
        throw const FormatException(
            'Encrypted autocomplete response is invalid.');
      }
    }
    return;
  }
  if (callbackType == 9) {
    final title = data['title'];
    final customId = data['custom_id'];
    final components = data['components'];
    if (data.length != 3 ||
        title is! String ||
        title.isEmpty ||
        title.length > 45 ||
        customId is! String ||
        customId.isEmpty ||
        customId.length > 100 ||
        components is! List ||
        components.isEmpty ||
        components.length > 5 ||
        components.any((component) =>
            component is! Map ||
            !const <int>{1, 10, 18}.contains(component['type']))) {
      throw const FormatException('Encrypted modal response is invalid.');
    }
    _validateMobileRichTree(components, 0);
    return;
  }
  if (!const <int>{4, 7}.contains(callbackType)) {
    throw const FormatException(
      'Encrypted bot response callback type is unsupported.',
    );
  }
  const allowed = <String>{
    'content',
    'embeds',
    'components',
    'flags',
    'poll',
    'attachments',
    'view_timeout_seconds',
    'view_persistent',
  };
  if (data.keys.any((key) => !allowed.contains(key)) ||
      data['content'] != null &&
          (data['content'] is! String ||
              (data['content']! as String).isEmpty ||
              (data['content']! as String).length > 4000) ||
      data['embeds'] != null &&
          (data['embeds'] is! List || (data['embeds']! as List).length > 10) ||
      data['components'] != null &&
          (data['components'] is! List ||
              (data['components']! as List).length > 40) ||
      data['flags'] != null &&
          (data['flags'] is! int ||
              (data['flags']! as int) < 0 ||
              (data['flags']! as int) > 2147483647) ||
      data['attachments'] != null && data['attachments'] is! Map ||
      data.containsKey('view_timeout_seconds') &&
          (data['view_timeout_seconds'] is! int ||
              (data['view_timeout_seconds']! as int) < 1 ||
              (data['view_timeout_seconds']! as int) > 86400) ||
      data.containsKey('view_persistent') && data['view_persistent'] != false) {
    throw const FormatException('Encrypted bot message shape is invalid.');
  }
  _validateMobileRichTree(data['embeds'] ?? const <Object?>[], 0);
  _validateMobileRichTree(data['components'] ?? const <Object?>[], 0);
  if (data['poll'] != null) _validateMobileRichTree(data['poll'], 0);
}

void _validateMobileRichTree(Object? value, int depth) {
  if (depth > 8) {
    throw const FormatException(
        'Encrypted bot rich content is too deeply nested.');
  }
  if (value == null || value is bool || value is num) return;
  if (value is String) {
    if (value.length > 4000) {
      throw const FormatException('Encrypted bot rich content is too large.');
    }
    return;
  }
  if (value is List) {
    if (value.length > 100) {
      throw const FormatException(
          'Encrypted bot rich content has too many items.');
    }
    for (final item in value) {
      _validateMobileRichTree(item, depth + 1);
    }
    return;
  }
  if (value is! Map || value.length > 64) {
    throw const FormatException('Encrypted bot rich content is invalid.');
  }
  for (final item in value.values) {
    _validateMobileRichTree(item, depth + 1);
  }
}

Map<String, Object?> _authenticatedMobileInteractionResponseData(
  Object? value,
  Map<String, Object?> context,
  List<Map<String, Object?>> transport,
) {
  if (value is! Map) {
    throw const FormatException('Encrypted bot response body is invalid.');
  }
  final canonical = Map<String, Object?>.from(
    _canonicalInteractionValue(value, <Object>{})! as Map,
  );
  _validateMobileInteractionResponsePlaintext(
    canonical,
    context['callback_type']! as int,
  );
  final refs = (context['attachment_refs']! as List).cast<String>();
  final rawManifests = canonical['attachments'];
  if (rawManifests != null && rawManifests is! Map) {
    throw const FormatException(
      'Encrypted bot response file manifests are invalid.',
    );
  }
  final manifests = rawManifests == null
      ? const <String, Object?>{}
      : Map<String, Object?>.from(rawManifests as Map);
  if (manifests.length != refs.length ||
      manifests.keys.any((ref) => !refs.contains(ref))) {
    throw const FormatException(
      'Encrypted bot response files do not match their authenticated refs.',
    );
  }
  if (refs.isEmpty) {
    return rawManifests == null
        ? canonical
        : <String, Object?>{...canonical, 'attachments': const <Object?>[]};
  }
  final byRef = <String, Map<String, Object?>>{
    for (final item in transport)
      '${item['id']}@${item['origin_domain']}': item,
  };
  final attachments = <Map<String, Object?>>[];
  for (final refValue in refs) {
    final ref = EntityRef.parse(refValue);
    final manifestValue = manifests[refValue];
    if (manifestValue is! Map) {
      throw const FormatException(
        'Encrypted bot response file manifest is invalid.',
      );
    }
    final manifest = _interactionAttachmentManifest(
      ref.id.value,
      Map<String, Object?>.from(manifestValue),
    );
    final projection = byRef[refValue];
    if (manifest['attachment_domain'] != ref.domain.value ||
        projection == null) {
      throw const FormatException(
        'Encrypted bot response file transport is invalid.',
      );
    }
    attachments.add(<String, Object?>{
      'id': ref.id.value,
      'origin_domain': ref.domain.value,
      'filename': manifest['filename'],
      'content_type': manifest['content_type'],
      'size': manifest['plaintext_size'],
      'width': null,
      'height': null,
      'blurhash': null,
      'scan_status': 'encrypted',
      'encryption_mode': 'e2ee',
      'encryption_protocol': 'kaede-file-v1',
      'variants': const <String, Object?>{},
      'private_media_url': projection['private_media_url'],
      'encrypted_manifest': manifest,
    });
  }
  return <String, Object?>{...canonical, 'attachments': attachments};
}

void _validateMobileBotResponseCredential(
  Uint8List value,
  Map<String, Object?> context,
) {
  final credential = Map<String, Object?>.from(
    jsonDecode(utf8.decode(value, allowMalformed: false)) as Map,
  );
  const fields = <String>{
    'account',
    'application_ref',
    'credential_type',
    'device_id',
    'worker_id',
  };
  final workerId = credential['worker_id'];
  if (credential.length != fields.length ||
      !credential.keys.toSet().containsAll(fields) ||
      credential['credential_type'] != 'kaede-bot-device-v2' ||
      credential['application_ref'] != context['application_ref'] ||
      credential['device_id'] != context['sender_device_id'] ||
      workerId is! String ||
      !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(workerId) ||
      BigInt.parse(workerId) > BigInt.parse('9223372036854775807') ||
      credential['account'] !=
          'bot:${context['application_ref']}:worker:$workerId') {
    throw const FormatException(
      'Encrypted bot response sender identity is invalid.',
    );
  }
}

const _mobileRichEnvelopeFields = <String>{
  'version',
  'protocol',
  'suite',
  'group_id',
  'policy_generation',
  'epoch',
  'forward_projection_digest',
  'forward_projection_version',
  'forward_snapshot_digest',
  'forward_source_projection_digest',
  'forwarded_channel_ref',
  'forwarded_created_at',
  'forwarded_edited_at',
  'forwarded_flags',
  'forwarded_message_ref',
  'forwarded_message_type',
  'sender_device_id',
  'operation',
  'ciphertext',
  'author_ref',
  'message_revision',
  'message_attachment_refs',
  'message_custom_emoji_refs',
  'message_mention_everyone',
  'message_mention_refs',
  'message_mention_role_refs',
  'message_mention_user_refs',
  'message_replied_user_ref',
  'message_sticker_refs',
  'referenced_message_ref',
  'rich_payload_digest',
  'application_ref',
  'interaction_integration_type',
  'interaction_installation_ref',
  'interaction_installation_revision',
  'view_version',
  'view_persistent',
  'tts',
  'voice_message',
  'message_flags',
};

Future<
    ({
      Map<String, Object?> context,
      Map<String, Object?>? contract,
    })> _mobileRichMessageProjection(
  KaedeChannel channel,
  KaedeMessage message,
  Map<String, Object?> envelope,
) async {
  final hasContract = envelope.containsKey('interaction_contract') &&
      envelope.containsKey('interaction_contract_digest');
  final fields = <String>{
    ..._mobileRichEnvelopeFields,
    if (envelope.containsKey('attachment_manifest_digest'))
      'attachment_manifest_digest',
    if (hasContract) ...<String>{
      'interaction_contract',
      'interaction_contract_digest',
    },
    if (envelope['operation'] == 'edit') 'target_message',
  };
  if (envelope.containsKey('interaction_contract') !=
          envelope.containsKey('interaction_contract_digest') ||
      envelope.length != fields.length ||
      !envelope.keys.toSet().containsAll(fields) ||
      envelope['version'] != 2 ||
      envelope['protocol'] != mlsProtocol ||
      envelope['suite'] != mlsSuite ||
      envelope['ciphertext'] is! String) {
    throw const FormatException('Encrypted rich message envelope is invalid.');
  }
  final attachmentRefs =
      message.attachments.map((item) => item.ref.wire).toList()..sort();
  final mentionRefs = message.mentionUserRefs.map((item) => item.wire).toList()
    ..sort();
  if (attachmentRefs.length > 10 ||
      attachmentRefs.toSet().length != attachmentRefs.length) {
    throw const FormatException(
      'Encrypted rich message attachment projection is invalid.',
    );
  }
  final context = validateMobileRichMessageAuthenticatedContext(
    <String, Object?>{
      'application_ref': envelope['application_ref'],
      'attachment_manifest_digest': envelope['attachment_manifest_digest'],
      'author_ref': envelope['author_ref'],
      'channel_ref': message.channelRef.wire,
      'epoch': envelope['epoch'],
      'forward_projection_digest': envelope['forward_projection_digest'],
      'forward_projection_version': envelope['forward_projection_version'],
      'forward_snapshot_digest': envelope['forward_snapshot_digest'],
      'forward_source_projection_digest':
          envelope['forward_source_projection_digest'],
      'forwarded_channel_ref': envelope['forwarded_channel_ref'],
      'forwarded_created_at': envelope['forwarded_created_at'],
      'forwarded_edited_at': envelope['forwarded_edited_at'],
      'forwarded_flags': envelope['forwarded_flags'],
      'forwarded_message_ref': envelope['forwarded_message_ref'],
      'forwarded_message_type': envelope['forwarded_message_type'],
      'group_id': envelope['group_id'],
      'interaction_contract_digest':
          hasContract ? envelope['interaction_contract_digest'] : null,
      'interaction_installation_ref': envelope['interaction_installation_ref'],
      'interaction_installation_revision':
          envelope['interaction_installation_revision'],
      'interaction_integration_type': envelope['interaction_integration_type'],
      'message_attachment_refs': envelope['message_attachment_refs'],
      'message_custom_emoji_refs': envelope['message_custom_emoji_refs'],
      'message_mention_everyone': envelope['message_mention_everyone'],
      'message_mention_refs': envelope['message_mention_refs'],
      'message_mention_role_refs': envelope['message_mention_role_refs'],
      'message_mention_user_refs': envelope['message_mention_user_refs'],
      'message_replied_user_ref': envelope['message_replied_user_ref'],
      'message_sticker_refs': envelope['message_sticker_refs'],
      'message_flags': envelope['message_flags'],
      'message_revision': envelope['message_revision'],
      'operation': envelope['operation'],
      'policy_generation': envelope['policy_generation'],
      'referenced_message_ref': envelope['referenced_message_ref'],
      'rich_payload_digest': envelope['rich_payload_digest'],
      'sender_device_id': envelope['sender_device_id'],
      'target_message': envelope['target_message'],
      'tts': envelope['tts'],
      'view_persistent': envelope['view_persistent'],
      'view_version': envelope['view_version'],
      'voice_message': envelope['voice_message'],
    },
  );
  final voiceMessage = message.flags & (1 << 13) != 0;
  if (context['channel_ref'] != channel.ref.wire ||
      context['author_ref'] != message.authorRef.wire ||
      context['application_ref'] != message.applicationRef?.wire ||
      context['message_flags'] != message.flags ||
      context['tts'] != message.tts ||
      context['voice_message'] != voiceMessage ||
      context['forwarded_message_ref'] != message.forwardedMessageRef?.wire ||
      message.forwardedMessage != null ||
      message.forwardSnapshot != null ||
      !_listEquals(
        (context['message_attachment_refs']! as List).cast<String>(),
        attachmentRefs,
      ) ||
      !_listEquals(
        (context['message_mention_refs']! as List).cast<String>(),
        mentionRefs,
      ) ||
      context['referenced_message_ref'] != message.reference?.wire ||
      context['interaction_integration_type'] !=
          message.interactionIntegrationType ||
      context['interaction_installation_ref'] !=
          message.interactionInstallationRef?.wire ||
      context['interaction_installation_revision'] !=
          message.interactionInstallationRevision?.toString() ||
      context['view_version'] != '${message.viewVersion}' ||
      context['view_persistent'] != message.viewPersistent) {
    throw const FormatException(
      'Encrypted rich message context does not match its projection.',
    );
  }
  final contract = hasContract
      ? validateMobileInteractionRoutingContract(
          envelope['interaction_contract'],
          null,
        )
      : null;
  if ((contract == null
          ? null
          : await mobileInteractionRoutingContractDigest(contract)) !=
      context['interaction_contract_digest']) {
    throw const FormatException(
      'Encrypted rich message routing contract digest is invalid.',
    );
  }
  if ((contract?['poll'] != null) !=
          (context['forward_projection_digest'] == null) ||
      (context['forward_projection_digest'] == null
          ? context['forward_projection_version'] != null
          : context['forward_projection_version'] != 2)) {
    throw const FormatException(
      'Encrypted rich message forward projection metadata is invalid.',
    );
  }
  final hasControls = contract != null &&
      contract['components'] is List &&
      (contract['components']! as List).isNotEmpty;
  if (hasControls != (message.viewVersion > 0) ||
      hasControls && message.applicationRef == null ||
      !hasControls &&
          (message.viewPersistent || message.viewExpiresAt != null) ||
      hasControls && message.viewPersistent && message.viewExpiresAt != null ||
      hasControls && !message.viewPersistent && message.viewExpiresAt == null) {
    throw const FormatException(
      'Encrypted rich message view projection is invalid.',
    );
  }
  return (context: context, contract: contract);
}

List<KaedeStickerItem> _mobileRichStickerItems(Object? value) {
  if (value is! List || value.length > 3) {
    throw const FormatException('Encrypted rich message stickers are invalid.');
  }
  final refs = <EntityRef>{};
  return value.map((item) {
    final raw = _mobileRoutingMap(item, 'Encrypted rich message sticker');
    late final EntityRef ref;
    try {
      ref = EntityRef.parse('${raw['id']}@${raw['origin_domain']}');
    } on FormatException {
      throw const FormatException('Encrypted rich message sticker is invalid.');
    }
    final name = raw['name'];
    final format = raw['format_type'];
    if (!_hasExactMobileRoutingFields(
          raw,
          const <String>{'id', 'origin_domain', 'name', 'format_type'},
        ) ||
        !refs.add(ref) ||
        name is! String ||
        name.trim() != name ||
        name.runes.length < 2 ||
        name.runes.length > 30 ||
        format is! int ||
        !const <int>{1, 2, 3, 4}.contains(format)) {
      throw const FormatException('Encrypted rich message sticker is invalid.');
    }
    return KaedeStickerItem(
      ref: ref,
      name: name,
      formatType: format,
      mediaHash: '',
    );
  }).toList(growable: false);
}

/// Collect every sticker whose private presentation is carried by this rich
/// body, including its immutable forwarded and nested snapshots.
List<String> mobileRichMessageStickerRefs(Object? value) {
  final data = _mobileRoutingMap(value, 'Encrypted rich message body');
  final refs = <String>{};

  void addItems(Object? items) {
    refs.addAll(_mobileRichStickerItems(items).map((item) => item.ref.wire));
  }

  void addSnapshot(Object? value) {
    final snapshot = _mobileRoutingMap(value, 'Encrypted forward snapshot');
    addItems(snapshot['sticker_items']);
    final nested = snapshot['message_snapshots'];
    if (nested is! List) {
      throw const FormatException(
        'Encrypted forward snapshot stickers are invalid.',
      );
    }
    nested.forEach(addSnapshot);
  }

  addItems(data['sticker_items']);
  if (data['forward_snapshot'] != null) {
    addSnapshot(
      validateMobileEncryptedForwardSnapshot(data['forward_snapshot']),
    );
  }
  if (refs.length > 9) {
    throw const FormatException(
      'Encrypted rich message has too many routed stickers.',
    );
  }
  return refs.toList()..sort();
}

/// Extract the exact authority-visible custom-emoji tokens from rich plaintext.
List<String> mobileRichMessageCustomEmojiRefs(Object? value) {
  final refs = <String>{};
  final tokenPattern = RegExp(
    r'<(a?):([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>',
  );

  void walk(Object? item) {
    if (item is String) {
      for (final match in tokenPattern.allMatches(item)) {
        final ref = '${match[3]}@${match[4]}';
        if (!_canonicalMobileRef(ref)) {
          throw const FormatException(
            'Encrypted rich message custom emoji is invalid.',
          );
        }
        refs.add('<${match[1]}:${match[2]}:$ref>');
      }
      return;
    }
    if (item is List) {
      item.forEach(walk);
      return;
    }
    if (item is! Map) return;
    final raw = Map<Object?, Object?>.from(item);
    final id = raw['id'];
    final name = raw['name'];
    final animated = raw['animated'] ?? false;
    if (id is String &&
        id.contains('@') &&
        name is String &&
        RegExp(r'^[A-Za-z0-9_]{2,32}$').hasMatch(name) &&
        animated is bool) {
      if (!_canonicalMobileRef(id)) {
        throw const FormatException(
          'Encrypted rich message custom emoji is invalid.',
        );
      }
      refs.add('<${animated ? 'a' : ''}:$name:$id>');
    }
    raw.values.forEach(walk);
  }

  walk(value);
  final result = refs.toList()..sort();
  if (result.length > 256) {
    throw const FormatException(
      'Encrypted rich message has too many custom emoji references.',
    );
  }
  return result;
}

Map<String, Object?> _mobileStableForwardManifest(
  Map<String, Object?> manifest,
) {
  final plaintextDigest = manifest['plaintext_sha256'];
  if (plaintextDigest is! String || !isCanonicalBase64url32(plaintextDigest)) {
    throw const FormatException(
      'Legacy encrypted attachments cannot be forwarded safely.',
    );
  }
  return <String, Object?>{
    'filename': manifest['filename'],
    'content_type': manifest['content_type'],
    'plaintext_size': manifest['plaintext_size'],
    'plaintext_sha256': plaintextDigest,
    if (manifest['duration_millis'] != null) ...<String, Object?>{
      'duration_millis': manifest['duration_millis'],
      'waveform': manifest['waveform'],
    },
  };
}

const _mobileForwardableMessageTypes = <int>{0, 19, 20, 23};
const _mobileForwardSnapshotFlagMask = (1 << 2) | (1 << 13) | (1 << 15);
const _mobileForwardSnapshotFields = <String>{
  'content',
  'embeds',
  'components',
  'attachments',
  'mention_user_refs',
  'sticker_items',
  'message_snapshots',
  'message_type',
  'flags',
  'created_at',
  'edited_at',
};

Map<String, Object?> _mobileStableForwardSnapshotAttachment(Object? value) {
  final raw = _mobileRoutingMap(
    value,
    'Encrypted forward snapshot attachment',
  );
  if (raw['protocol'] == 'kaede-file-v1') {
    final manifests = validateMobileEncryptedRichMessageAttachments(
      <Object?>[raw],
      voiceMessage:
          raw.containsKey('duration_millis') || raw.containsKey('waveform'),
    );
    return _mobileStableForwardManifest(manifests.single);
  }
  const allowed = <String>{
    'id',
    'origin_domain',
    'filename',
    'content_type',
    'size',
    'plaintext_sha256',
    'width',
    'height',
    'duration_secs',
    'waveform',
    'blurhash',
    'scan_status',
    'encryption_mode',
    'encryption_protocol',
    'variants',
  };
  late final EntityRef ref;
  try {
    ref = EntityRef.parse('${raw['id']}@${raw['origin_domain']}');
  } on FormatException {
    throw const FormatException(
      'Encrypted forward snapshot attachment is invalid.',
    );
  }
  final duration = raw['duration_secs'];
  if (raw.keys.any((field) => !allowed.contains(field)) ||
      raw['id'] != ref.id.value ||
      raw['origin_domain'] != ref.domain.value ||
      raw['filename'] is! String ||
      (raw['filename']! as String).isEmpty ||
      (raw['filename']! as String).runes.length > 255 ||
      raw['content_type'] is! String ||
      (raw['content_type']! as String).isEmpty ||
      (raw['content_type']! as String).length > 100 ||
      raw['size'] is! int ||
      (raw['size']! as int) < 0 ||
      (raw['size']! as int) > 100 * 1024 * 1024 ||
      raw['plaintext_sha256'] is! String ||
      !isCanonicalBase64url32(raw['plaintext_sha256']! as String) ||
      raw['encryption_mode'] != 'plaintext' ||
      ((duration == null) != (raw['waveform'] == null)) ||
      duration != null &&
          (duration is! num ||
              !duration.isFinite ||
              duration <= 0 ||
              duration > 1200 ||
              raw['waveform'] is! String)) {
    throw const FormatException(
      'Encrypted forward snapshot attachment is invalid.',
    );
  }
  return <String, Object?>{
    'filename': raw['filename'],
    'content_type': raw['content_type'],
    'plaintext_size': raw['size'],
    'plaintext_sha256': raw['plaintext_sha256'],
    if (duration != null) ...<String, Object?>{
      'duration_millis': ((duration as num) * 1000).round(),
      'waveform': raw['waveform'],
    },
  };
}

Map<String, Object?> mobileEncryptedForwardAttachmentSemantics(
  Object? value,
) =>
    _mobileStableForwardSnapshotAttachment(value);

({Map<String, Object?> snapshot, Map<String, Object?> projection})
    _mobileForwardSnapshotProjection(Object? value, [int depth = 0]) {
  final raw = _mobileRoutingMap(value, 'Encrypted forward snapshot');
  final expected = <String>{..._mobileForwardSnapshotFields};
  if (!raw.containsKey('edited_at')) expected.remove('edited_at');
  if (raw.length != expected.length ||
      !raw.keys.toSet().containsAll(expected)) {
    throw const FormatException(
      'Encrypted forward snapshot fields are invalid.',
    );
  }
  final content = raw['content'];
  final embeds = raw['embeds'];
  final components = raw['components'];
  final attachments = raw['attachments'];
  final mentions = raw['mention_user_refs'];
  final stickers = raw['sticker_items'];
  final nested = raw['message_snapshots'];
  final messageType = raw['message_type'];
  final flags = raw['flags'];
  if (content != null &&
          (content is! String ||
              content.isEmpty ||
              content.runes.length > 4000) ||
      embeds is! List ||
      embeds.length > 10 ||
      embeds.any((item) => item is! Map) ||
      components is! List ||
      components.length > 40 ||
      components.any((item) => item is! Map) ||
      attachments is! List ||
      attachments.length > 10 ||
      mentions is! List ||
      mentions.length > 5000 ||
      stickers is! List ||
      stickers.length > 3 ||
      stickers.any((item) => item is! Map) ||
      nested is! List ||
      nested.length > 1 ||
      depth > 0 && nested.isNotEmpty ||
      messageType is! int ||
      !_mobileForwardableMessageTypes.contains(messageType) ||
      flags is! int ||
      flags < 0 ||
      flags & ~_mobileForwardSnapshotFlagMask != 0) {
    throw const FormatException('Encrypted forward snapshot is invalid.');
  }
  _validateMobileRichTree(embeds, 0);
  _validateMobileRichTree(components, 0);
  final createdAt = raw['created_at'];
  final editedAt = raw['edited_at'];
  final timestamp = RegExp(r'(?:Z|[+-][0-9]{2}:[0-9]{2})$');
  final created = createdAt is String ? DateTime.tryParse(createdAt) : null;
  final edited = editedAt is String ? DateTime.tryParse(editedAt) : null;
  if (created == null ||
      !timestamp.hasMatch(createdAt! as String) ||
      editedAt != null &&
          (edited == null ||
              !timestamp.hasMatch(editedAt as String) ||
              edited.isBefore(created))) {
    throw const FormatException(
      'Encrypted forward snapshot timestamps are invalid.',
    );
  }
  final normalizedMentions = <Map<String, Object?>>[];
  final mentionRefs = <String>[];
  for (final item in mentions) {
    final mention = _mobileRoutingMap(
      item,
      'Encrypted forward snapshot mention',
    );
    if (!_hasExactMobileRoutingFields(
      mention,
      const <String>{'id', 'origin_domain'},
    )) {
      throw const FormatException(
        'Encrypted forward snapshot mention is invalid.',
      );
    }
    late final EntityRef ref;
    try {
      ref = EntityRef.parse('${mention['id']}@${mention['origin_domain']}');
    } on FormatException {
      throw const FormatException(
        'Encrypted forward snapshot mention is invalid.',
      );
    }
    if (mention['id'] != ref.id.value ||
        mention['origin_domain'] != ref.domain.value) {
      throw const FormatException(
        'Encrypted forward snapshot mention is invalid.',
      );
    }
    mentionRefs.add(ref.wire);
    normalizedMentions.add(<String, Object?>{
      'id': ref.id.value,
      'origin_domain': ref.domain.value,
    });
  }
  final sortedMentions = mentionRefs.toSet().toList()..sort();
  if (!_listEquals(mentionRefs, sortedMentions)) {
    throw const FormatException(
      'Encrypted forward snapshot mentions are invalid.',
    );
  }
  final normalizedNested = nested
      .map((item) => _mobileForwardSnapshotProjection(item, depth + 1))
      .toList(growable: false);
  if (content == null &&
      embeds.isEmpty &&
      components.isEmpty &&
      attachments.isEmpty &&
      stickers.isEmpty &&
      nested.isEmpty) {
    throw const FormatException('Encrypted forward snapshot has no body.');
  }
  final snapshot = <String, Object?>{
    'content': content,
    'embeds':
        embeds.map((item) => Map<String, Object?>.from(item as Map)).toList(),
    'components': components
        .map((item) => Map<String, Object?>.from(item as Map))
        .toList(),
    'attachments': attachments
        .map((item) => Map<String, Object?>.from(item as Map))
        .toList(),
    'mention_user_refs': normalizedMentions,
    'sticker_items':
        stickers.map((item) => Map<String, Object?>.from(item as Map)).toList(),
    'message_snapshots': normalizedNested.map((item) => item.snapshot).toList(),
    'message_type': messageType,
    'flags': flags,
    'created_at': createdAt,
    'edited_at': editedAt,
  };
  return (
    snapshot: snapshot,
    projection: <String, Object?>{
      'version': 2,
      'content': content,
      'embeds': snapshot['embeds'],
      'components': snapshot['components'],
      'attachments': attachments
          .map(_mobileStableForwardSnapshotAttachment)
          .toList(growable: false),
      'mention_user_refs': normalizedMentions,
      'sticker_items': snapshot['sticker_items'],
      'message_snapshots':
          normalizedNested.map((item) => item.projection).toList(),
      'flags': flags,
    },
  );
}

Map<String, Object?> validateMobileEncryptedForwardSnapshot(Object? value) =>
    _mobileForwardSnapshotProjection(value).snapshot;

Future<String> mobileEncryptedForwardSnapshotProjectionDigest(
  Object? value,
) async {
  final encoded = mobileCanonicalInteractionJson(
    _mobileForwardSnapshotProjection(value).projection,
  );
  try {
    return _base64url((await Sha256().hash(encoded)).bytes);
  } finally {
    encoded.fillRange(0, encoded.length, 0);
  }
}

Future<String> mobileEncryptedForwardSnapshotDigest(Object? value) async {
  final encoded = mobileCanonicalInteractionJson(value);
  try {
    return _base64url((await Sha256().hash(encoded)).bytes);
  } finally {
    encoded.fillRange(0, encoded.length, 0);
  }
}

KaedeMessageSnapshot mobileEncryptedForwardSnapshotPresentation(
  Object? value,
) {
  final snapshot = validateMobileEncryptedForwardSnapshot(value);
  final attachments = (snapshot['attachments']! as List).map((item) {
    final raw = _mobileRoutingMap(
      item,
      'Encrypted forward snapshot attachment',
    );
    if (raw['protocol'] != 'kaede-file-v1') {
      return raw;
    }
    final manifest = validateMobileEncryptedRichMessageAttachments(
      <Object?>[raw],
      voiceMessage:
          raw.containsKey('duration_millis') || raw.containsKey('waveform'),
    ).single;
    return <String, Object?>{
      'id': manifest['attachment_id'],
      'origin_domain': manifest['attachment_domain'],
      'filename': manifest['filename'],
      'content_type': manifest['content_type'],
      'size': manifest['plaintext_size'],
      'width': null,
      'height': null,
      'blurhash': null,
      'scan_status': 'encrypted',
      'duration_secs': manifest['duration_millis'] == null
          ? null
          : (manifest['duration_millis']! as int) / 1000,
      'waveform': manifest['waveform'],
      'plaintext_sha256': manifest['plaintext_sha256'],
      'encrypted_manifest': manifest,
    };
  }).toList(growable: false);
  final nested = (snapshot['message_snapshots']! as List)
      .map(mobileEncryptedForwardSnapshotPresentation)
      .map((item) => <String, Object?>{'message': item.toJson()})
      .toList(growable: false);
  return KaedeMessageSnapshot.fromJson(
    <String, Object?>{
      ...snapshot,
      'attachments': attachments,
      'message_snapshots': nested,
    },
    trustClientState: true,
  );
}

/// Digest of the author-free body eligible for a secure immutable forward.
Future<String?> mobileRichMessageForwardProjectionDigest(
  Map<String, Object?> data,
  List<String> mentionRefs,
) async {
  if (data['poll'] != null) return null;
  if (!_canonicalMobileSortedRefs(mentionRefs, 5000)) {
    throw const FormatException(
      'Encrypted rich message mention references are invalid.',
    );
  }
  final attachments = validateMobileEncryptedRichMessageAttachments(
    data['attachments'],
    voiceMessage: data['voice_message'] == true,
  );
  final mentions = mentionRefs.map((item) {
    final ref = EntityRef.parse(item);
    return <String, Object?>{
      'id': ref.id.value,
      'origin_domain': ref.domain.value,
    };
  }).toList(growable: false);
  final projection = <String, Object?>{
    'version': 2,
    'content': data['content'],
    'embeds': data['embeds'],
    'components': data['components'],
    'attachments':
        attachments.map(_mobileStableForwardManifest).toList(growable: false),
    'mention_user_refs': mentions,
    'sticker_items': data['sticker_items'],
    'message_snapshots': data['forward_snapshot'] == null
        ? const <Object?>[]
        : <Object?>[
            _mobileForwardSnapshotProjection(data['forward_snapshot'])
                .projection,
          ],
    'flags': (data['flags']! as int) & ((1 << 2) | (1 << 13) | (1 << 15)),
  };
  final encoded = mobileCanonicalInteractionJson(projection);
  try {
    return _base64url((await Sha256().hash(encoded)).bytes);
  } finally {
    encoded.fillRange(0, encoded.length, 0);
  }
}

Map<String, Object?> _mobileRichPollMedia(Object? value,
    {required bool answer}) {
  final raw = _mobileRoutingMap(value, 'Encrypted poll media');
  final text = raw['text'];
  if (raw.keys
          .any((field) => !const <String>{'text', 'emoji'}.contains(field)) ||
      !raw.containsKey('text') && !raw.containsKey('emoji') ||
      text != null &&
          (text is! String ||
              text.trim().isEmpty ||
              text.runes.length > (answer ? 55 : 300))) {
    throw const FormatException('Encrypted poll media is invalid.');
  }
  if (raw.containsKey('emoji')) {
    final emoji = _mobileRoutingMap(raw['emoji'], 'Encrypted poll emoji');
    if (emoji.keys.any(
          (field) => !const <String>{'id', 'name', 'animated'}.contains(field),
        ) ||
        emoji['id'] == null && emoji['name'] == null ||
        emoji['id'] != null && !_canonicalMobileRef(emoji['id']) ||
        emoji['name'] != null &&
            (emoji['name'] is! String ||
                (emoji['name']! as String).trim().isEmpty ||
                (emoji['name']! as String).runes.length > 64) ||
        emoji['animated'] != null && emoji['animated'] is! bool ||
        emoji['animated'] == true && emoji['id'] == null) {
      throw const FormatException('Encrypted poll emoji is invalid.');
    }
  }
  return raw;
}

RichPoll _mobileMergedRichPoll(
  Object? dataValue,
  Object? projectionValue,
  Object? contractValue,
  DateTime createdAt,
) {
  final data = _mobileRoutingMap(dataValue, 'Encrypted poll');
  final projection = _mobileRoutingMap(
    projectionValue,
    'Encrypted poll projection',
  );
  final contract = _validateMobileRoutingPoll(contractValue);
  final answers = data['answers'];
  final answerIds = (contract['answer_ids']! as List).cast<int>();
  if (!_hasExactMobileRoutingFields(
        data,
        const <String>{
          'question',
          'answers',
          'duration',
          'allow_multiselect',
          'layout_type',
        },
      ) ||
      answers is! List ||
      answers.length != answerIds.length ||
      data['allow_multiselect'] != contract['allow_multiselect'] ||
      data['layout_type'] != 1 ||
      data['duration'] is! int ||
      (data['duration']! as int) * 3600 != contract['duration_seconds']) {
    throw const FormatException(
      'Encrypted poll does not match its routing contract.',
    );
  }
  final question = _mobileRichPollMedia(data['question'], answer: false);
  if (question['text'] is! String || question.containsKey('emoji')) {
    throw const FormatException('Encrypted poll question is invalid.');
  }
  final presentationAnswers = <Map<String, Object?>>[];
  for (final item in answers) {
    final raw = _mobileRoutingMap(item, 'Encrypted poll answer');
    if (!_hasExactMobileRoutingFields(raw, const <String>{'poll_media'})) {
      throw const FormatException('Encrypted poll answer is invalid.');
    }
    presentationAnswers.add(<String, Object?>{
      'answer_id': presentationAnswers.length + 1,
      'poll_media': _mobileRichPollMedia(raw['poll_media'], answer: true),
    });
  }
  final result = _mobileRoutingMap(
    projection['results'],
    'Encrypted poll results',
  );
  final counts = result['answer_counts'];
  final expiry = projection['expiry'] is String
      ? DateTime.tryParse(projection['expiry']! as String)?.toUtc()
      : null;
  final expectedExpiry = createdAt.toUtc().add(
        Duration(seconds: contract['duration_seconds']! as int),
      );
  if (!_hasExactMobileRoutingFields(
        projection,
        const <String>{
          'encrypted',
          'answer_ids',
          'expiry',
          'allow_multiselect',
          'layout_type',
          'finalized_at',
          'results',
        },
      ) ||
      projection['encrypted'] != true ||
      !_listEquals(
        (projection['answer_ids'] as List?)?.cast<int>() ?? const <int>[],
        answerIds,
      ) ||
      projection['allow_multiselect'] != contract['allow_multiselect'] ||
      projection['layout_type'] != 1 ||
      expiry == null ||
      (expiry.difference(expectedExpiry).inMilliseconds).abs() > 2000 ||
      projection['finalized_at'] != null &&
          (projection['finalized_at'] is! String ||
              DateTime.tryParse(projection['finalized_at']! as String) ==
                  null) ||
      !_hasExactMobileRoutingFields(
        result,
        const <String>{'is_finalized', 'answer_counts'},
      ) ||
      result['is_finalized'] is! bool ||
      counts is! List ||
      counts.length != answerIds.length) {
    throw const FormatException('Encrypted poll projection is invalid.');
  }
  final presentationCounts = <Map<String, Object?>>[];
  for (var index = 0; index < counts.length; index++) {
    final count = _mobileRoutingMap(counts[index], 'Encrypted poll count');
    if (!_hasExactMobileRoutingFields(
          count,
          const <String>{'id', 'count', 'me_voted'},
        ) ||
        count['id'] != answerIds[index] ||
        count['count'] is! int ||
        (count['count']! as int) < 0 ||
        count['me_voted'] is! bool) {
      throw const FormatException('Encrypted poll count is invalid.');
    }
    presentationCounts.add(count);
  }
  if (projection['finalized_at'] != null && result['is_finalized'] != true) {
    throw const FormatException('Encrypted poll finalization is invalid.');
  }
  return RichPoll.fromJson(<String, Object?>{
    'question': question,
    'answers': presentationAnswers,
    'expiry': projection['expiry'],
    'allow_multiselect': contract['allow_multiselect'],
    'layout_type': 1,
    'results': <String, Object?>{
      'is_finalized': result['is_finalized'],
      'answer_counts': presentationCounts,
    },
  });
}

Future<DecryptedE2EEApplication> _authenticatedMobileRichMessageApplication(
  Object? value,
  Map<String, Object?> context,
  Map<String, Object?>? contract,
  KaedeMessage message,
) async {
  final data = _mobileRoutingMap(value, 'Encrypted rich message body');
  final content = data['content'];
  final embeds = data['embeds'];
  final components = data['components'];
  final flags = data['flags'];
  if (!_hasExactMobileRoutingFields(
        data,
        const <String>{
          'content',
          'embeds',
          'components',
          'poll',
          'sticker_items',
          'tts',
          'voice_message',
          'flags',
          'allowed_mentions',
          'forward_snapshot',
          'attachments',
        },
      ) ||
      content != null &&
          (content is! String ||
              content.trim().isEmpty ||
              content.runes.length > 4000) ||
      embeds is! List ||
      embeds.length > 10 ||
      embeds.any((item) => item is! Map) ||
      components is! List ||
      components.length > 40 ||
      components.any((item) => item is! Map) ||
      data['tts'] is! bool ||
      data['voice_message'] is! bool ||
      flags is! int ||
      flags < 0 ||
      flags > 2147483647 ||
      data['tts'] != context['tts'] ||
      data['voice_message'] != context['voice_message'] ||
      flags != context['message_flags']) {
    throw const FormatException('Encrypted rich message body is invalid.');
  }
  final forwardSnapshot = data['forward_snapshot'] == null
      ? null
      : validateMobileEncryptedForwardSnapshot(data['forward_snapshot']);
  if (forwardSnapshot == null) {
    if (context['forward_snapshot_digest'] != null ||
        context['forward_source_projection_digest'] != null ||
        context['forwarded_message_ref'] != null ||
        context['forwarded_channel_ref'] != null ||
        context['forwarded_created_at'] != null ||
        context['forwarded_edited_at'] != null ||
        context['forwarded_flags'] != null ||
        context['forwarded_message_type'] != null) {
      throw const FormatException(
        'Encrypted rich message forward metadata is incomplete.',
      );
    }
  } else {
    try {
      EntityRef.parse(context['forwarded_message_ref']! as String);
      EntityRef.parse(context['forwarded_channel_ref']! as String);
    } on Object {
      throw const FormatException(
        'Encrypted rich message forward source is invalid.',
      );
    }
    if (context['forward_snapshot_digest'] !=
            await mobileEncryptedForwardSnapshotDigest(
              data['forward_snapshot'],
            ) ||
        context['forward_source_projection_digest'] !=
            await mobileEncryptedForwardSnapshotProjectionDigest(
              forwardSnapshot,
            ) ||
        context['forwarded_created_at'] != forwardSnapshot['created_at'] ||
        context['forwarded_edited_at'] != forwardSnapshot['edited_at'] ||
        context['forwarded_flags'] != forwardSnapshot['flags'] ||
        context['forwarded_message_type'] != forwardSnapshot['message_type']) {
      throw const FormatException(
        'Encrypted rich message forward source was modified.',
      );
    }
  }
  _validateMobileRichTree(embeds, 0);
  _validateMobileRichTree(components, 0);
  final rawAttachments = data['attachments'];
  final attachments = validateMobileEncryptedRichMessageAttachments(
    rawAttachments,
    voiceMessage: context['voice_message'] == true,
  );
  final attachmentRefs = attachments
      .map(
        (manifest) =>
            '${manifest['attachment_id']}@${manifest['attachment_domain']}',
      )
      .toList(growable: false);
  if (!_listEquals(
    attachmentRefs,
    (context['message_attachment_refs']! as List).cast<String>(),
  )) {
    throw const FormatException(
      'Encrypted rich message files do not match their authenticated refs.',
    );
  }
  if (context['attachment_manifest_digest'] case final String expected) {
    final encoded = mobileCanonicalInteractionJson(rawAttachments);
    try {
      if (_base64url((await Sha256().hash(encoded)).bytes) != expected) {
        throw const FormatException(
          'Encrypted rich message file manifest was modified.',
        );
      }
    } finally {
      encoded.fillRange(0, encoded.length, 0);
    }
  }
  if (await mobileRichMessagePayloadDigest(data) !=
      context['rich_payload_digest']) {
    throw const FormatException(
      'Encrypted rich message body digest was modified.',
    );
  }
  if (await mobileRichMessageForwardProjectionDigest(
        data,
        (context['message_mention_refs']! as List).cast<String>(),
      ) !=
      context['forward_projection_digest']) {
    throw const FormatException(
      'Encrypted rich message forward projection was modified.',
    );
  }
  final derivedContract = await mobileInteractionRoutingContract(data, null);
  final expectedContract =
      contract == null ? null : mobileCanonicalInteractionJson(contract);
  final actualContract = derivedContract == null
      ? null
      : mobileCanonicalInteractionJson(derivedContract);
  try {
    if ((expectedContract == null) != (actualContract == null) ||
        expectedContract != null &&
            actualContract != null &&
            !_constantTimeEquals(expectedContract, actualContract) ||
        (derivedContract == null
                ? null
                : await mobileInteractionRoutingContractDigest(
                    derivedContract,
                  )) !=
            context['interaction_contract_digest']) {
      throw const FormatException(
        'Encrypted rich message routing contract does not match its body.',
      );
    }
  } finally {
    expectedContract?.fillRange(0, expectedContract.length, 0);
    actualContract?.fillRange(0, actualContract.length, 0);
  }
  final stickerItems = _mobileRichStickerItems(data['sticker_items']);
  final allowedMentions = validateMobileEncryptedAllowedMentions(
    data['allowed_mentions'],
  );
  final mentionIntent = mobileRichMessageMentionIntent(data);
  if (!_listEquals(
        mobileRichMessageStickerRefs(data),
        (context['message_sticker_refs']! as List).cast<String>(),
      ) ||
      !_listEquals(
        mobileRichMessageCustomEmojiRefs(data),
        (context['message_custom_emoji_refs']! as List).cast<String>(),
      ) ||
      !_listEquals(
        mentionIntent.userRefs,
        (context['message_mention_user_refs']! as List).cast<String>(),
      ) ||
      !_listEquals(
        mentionIntent.roleRefs,
        (context['message_mention_role_refs']! as List).cast<String>(),
      ) ||
      mentionIntent.everyone != context['message_mention_everyone'] ||
      allowedMentions['replied_user'] !=
          (context['message_replied_user_ref'] != null) ||
      context['message_replied_user_ref'] != null &&
          context['referenced_message_ref'] == null) {
    throw const FormatException(
      'Encrypted rich message routing metadata was modified.',
    );
  }
  if (context['message_replied_user_ref'] != null &&
      message.referencedMessage?.authorRef.wire !=
          context['message_replied_user_ref']) {
    throw const FormatException(
      'Encrypted rich message replied-user reference was modified.',
    );
  }
  final requiredRecipients = <String>{
    ...mentionIntent.userRefs,
    if (context['message_replied_user_ref'] case final String ref) ref,
  };
  final resolvedRecipients =
      (context['message_mention_refs']! as List).cast<String>().toSet();
  if (!resolvedRecipients.containsAll(requiredRecipients) ||
      mentionIntent.roleRefs.isEmpty &&
          !mentionIntent.everyone &&
          (resolvedRecipients.length != requiredRecipients.length ||
              !requiredRecipients.containsAll(resolvedRecipients))) {
    throw const FormatException(
      'Encrypted rich message resolved mention routing was modified.',
    );
  }
  final pollContract = contract?['poll'];
  final RichPoll? poll;
  if (data['poll'] == null &&
      pollContract == null &&
      message.encryptedPollProjection == null) {
    poll = null;
  } else if (data['poll'] != null &&
      pollContract != null &&
      message.encryptedPollProjection != null) {
    poll = _mobileMergedRichPoll(
      data['poll'],
      message.encryptedPollProjection,
      pollContract,
      message.createdAt,
    );
  } else {
    throw const FormatException(
      'Encrypted poll projection does not match its authenticated body.',
    );
  }
  final componentsV2 = components.any(
    (item) => (item as Map)['type'] != 1,
  );
  if ((((context['message_flags']! as int) & (1 << 15)) != 0) != componentsV2 ||
      componentsV2 &&
          (content != null ||
              embeds.isNotEmpty ||
              poll != null ||
              stickerItems.isNotEmpty) ||
      context['voice_message'] == true &&
          (content != null ||
              embeds.isNotEmpty ||
              components.isNotEmpty ||
              poll != null ||
              stickerItems.isNotEmpty ||
              attachments.length != 1) ||
      content == null &&
          embeds.isEmpty &&
          components.isEmpty &&
          poll == null &&
          stickerItems.isEmpty &&
          attachments.isEmpty) {
    throw const FormatException(
      'Encrypted rich message content combination is invalid.',
    );
  }
  return DecryptedE2EEApplication(
    content: content as String?,
    attachments: attachments,
    rich: true,
    embeds: embeds
        .map((item) =>
            RichEmbed.fromJson(Map<String, Object?>.from(item as Map)))
        .toList(growable: false),
    components: components
        .map(
          (item) => RichMessageLayout.fromJson(
              Map<String, Object?>.from(item as Map)),
        )
        .toList(growable: false),
    poll: poll,
    stickerItems: stickerItems,
    tts: context['tts']! as bool,
    voiceMessage: context['voice_message']! as bool,
    flags: context['message_flags']! as int,
    allowedMentions: allowedMentions,
    forwardSnapshot: forwardSnapshot,
    forwardSnapshotPresentation: forwardSnapshot == null
        ? null
        : mobileEncryptedForwardSnapshotPresentation(forwardSnapshot),
  );
}

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
  Map<String, Object?> envelope, [
  EntityRef? applicationRef,
  EntityRef? webhookRef,
]) {
  final fields = <String>[
    messageRef.wire,
    channelRef.wire,
    authorRef.wire,
    applicationRef?.wire ?? '',
    webhookRef?.wire ?? '',
    utf8.decode(mobileCanonicalInteractionJson(envelope)),
  ];
  if (fields.any((field) => field.contains('\u0000'))) {
    throw const FormatException('Encrypted message context is invalid.');
  }
  return fields.join('\u0000');
}

void validateMobileEncryptedMessageSenderCredential(
  Uint8List credentialBytes,
  KaedeMessage message,
  String senderDeviceId,
) {
  final credential = Map<String, Object?>.from(
    jsonDecode(utf8.decode(credentialBytes, allowMalformed: false)) as Map,
  );
  final application = message.applicationRef;
  final webhook = message.webhookRef;
  const humanFields = <String>{'version', 'account', 'nonce'};
  if (credential.length == humanFields.length &&
      credential.keys.toSet().containsAll(humanFields) &&
      credential['version'] == 1 &&
      credential['account'] == message.authorRef.wire &&
      application == null &&
      webhook == null &&
      credential['nonce'] is String &&
      RegExp(r'^ked_[A-Za-z0-9_-]{43}$').hasMatch(senderDeviceId)) {
    final nonce = _decode(credential['nonce']! as String, 32);
    try {
      if (nonce.length == 32) return;
    } finally {
      nonce.fillRange(0, nonce.length, 0);
    }
  }
  const botFields = <String>{
    'account',
    'application_ref',
    'credential_type',
    'device_id',
    'worker_id',
  };
  final workerId = credential['worker_id'];
  final botValid = webhook == null &&
      application != null &&
      credential.length == botFields.length &&
      credential.keys.toSet().containsAll(botFields) &&
      credential['credential_type'] == 'kaede-bot-device-v2' &&
      credential['application_ref'] == application.wire &&
      credential['device_id'] == senderDeviceId &&
      RegExp(r'^kbe_[A-Za-z0-9_-]{43}$').hasMatch(senderDeviceId) &&
      workerId is String &&
      RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(workerId) &&
      BigInt.parse(workerId) <= BigInt.parse('9223372036854775807') &&
      credential['account'] == 'bot:${application.wire}:worker:$workerId';
  const webhookFields = <String>{
    'account',
    'credential_type',
    'device_id',
    'webhook_ref',
  };
  final webhookValid = application == null &&
      webhook != null &&
      credential.length == webhookFields.length &&
      credential.keys.toSet().containsAll(webhookFields) &&
      credential['credential_type'] == 'kaede-webhook-device-v1' &&
      credential['device_id'] == senderDeviceId &&
      credential['webhook_ref'] == webhook.wire &&
      credential['account'] == 'webhook:${webhook.wire}' &&
      RegExp(r'^kwe_[A-Za-z0-9_-]{43}$').hasMatch(senderDeviceId);
  if (!botValid && !webhookValid) {
    throw const FormatException(
      'Encrypted message sender identity does not match its author or actor.',
    );
  }
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
    this.rich = false,
    this.embeds = const <RichEmbed>[],
    this.components = const <RichMessageLayout>[],
    this.poll,
    this.stickerItems = const <KaedeStickerItem>[],
    this.tts = false,
    this.voiceMessage = false,
    this.flags = 0,
    this.allowedMentions,
    this.forwardSnapshot,
    this.forwardSnapshotPresentation,
  });

  final String? content;
  final List<Map<String, Object?>> attachments;
  final bool rich;
  final List<RichEmbed> embeds;
  final List<RichMessageLayout> components;
  final RichPoll? poll;
  final List<KaedeStickerItem> stickerItems;
  final bool tts;
  final bool voiceMessage;
  final int flags;
  final Map<String, Object?>? allowedMentions;
  final Map<String, Object?>? forwardSnapshot;
  final KaedeMessageSnapshot? forwardSnapshotPresentation;

  KaedeMessage applyTo(KaedeMessage message) => message.copyWith(
        e2eeVerified: true,
        content: content,
        clearContent: rich && content == null,
        decryptedAttachments: attachments,
        stickerItems: rich ? stickerItems : null,
        embeds: rich ? embeds : null,
        components: rich ? components : null,
        poll: rich ? poll : null,
        clearPoll: rich && poll == null,
        clearEncryptedPollProjection: rich,
        tts: rich ? tts : null,
        flags: rich ? flags : null,
        decryptedAllowedMentions: rich ? allowedMentions : null,
        forwardSnapshot: rich ? forwardSnapshotPresentation : null,
        clearForwardSnapshot: rich && forwardSnapshotPresentation == null,
        decryptedForwardSnapshot: rich ? forwardSnapshot : null,
        clearDecryptedForwardSnapshot: rich && forwardSnapshot == null,
      );
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

Future<String> mobileBotE2eeDeviceId(
  EntityRef application,
  String workerId,
  List<int> identityKey,
) async {
  if (!RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(workerId) ||
      BigInt.parse(workerId) > BigInt.parse('9223372036854775807') ||
      identityKey.length != 32) {
    throw const FormatException('The bot encryption identity is invalid.');
  }
  final digest = await Sha256().hash(<int>[
    ...utf8.encode(
      'kaede-bot-e2ee-device-v1\u0000${application.wire}\u0000$workerId\u0000',
    ),
    ...identityKey,
  ]);
  return 'kbe_${_base64url(digest.bytes)}';
}

Future<String> mobileWebhookE2eeDeviceId(
  EntityRef webhook,
  List<int> identityKey,
) async {
  if (identityKey.length != 32) {
    throw const FormatException('The webhook encryption identity is invalid.');
  }
  final digest = await Sha256().hash(<int>[
    ...utf8.encode(
      'kaede-webhook-e2ee-device-v1\u0000${webhook.wire}\u0000',
    ),
    ...identityKey,
  ]);
  return 'kwe_${_base64url(digest.bytes)}';
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
        recordRef.domain != channel.domain ||
        policyGeneration is! String ||
        !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(policyGeneration) ||
        epoch is! String ||
        !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(epoch) ||
        apply is! bool ||
        roomOperationId is! String ||
        !RegExp(r'^keo_[A-Za-z0-9_-]{43}$').hasMatch(roomOperationId) ||
        roomOperationDomain is! String ||
        Domain(roomOperationDomain).value != roomOperationDomain ||
        roomOperationDomain != channel.domain.value ||
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
            !RegExp(r'^(?:ked|kbe|kwe)_[A-Za-z0-9_-]{43}$')
                .hasMatch(claimedDevice) ||
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
        if (claimedAccount.wire != '$userId@$userDomain') {
          throw const FormatException(
            'A claimed key package has a non-canonical participant.',
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
          if (expectedIdentity.length != 32 ||
              !_constantTimeEquals(
                inspected.signatureKey,
                expectedIdentity,
              ) ||
              !_constantTimeEquals(
                inspected.credential,
                expectedCredential,
              )) {
            throw const FormatException(
              'A claimed key package does not authenticate its participant.',
            );
          }
          if (claimedDevice.startsWith('ked_')) {
            final nonce = credentialPayload['nonce'];
            const credentialFields = <String>{'version', 'account', 'nonce'};
            if (nonce is! String ||
                credentialPayload.length != credentialFields.length ||
                !credentialPayload.keys.toSet().containsAll(credentialFields) ||
                credentialPayload['version'] != 1 ||
                credentialPayload['account'] != claimedAccount.wire) {
              throw const FormatException(
                'A claimed key package does not authenticate its participant.',
              );
            }
            credentialNonce = _decode(nonce, 32);
            if (credentialNonce.length != 32 ||
                await mobileE2eeDeviceId(
                      claimedAccount.wire,
                      expectedIdentity,
                    ) !=
                    claimedDevice) {
              throw const FormatException(
                'A claimed key package does not authenticate its participant.',
              );
            }
          } else if (claimedDevice.startsWith('kbe_')) {
            const credentialFields = <String>{
              'account',
              'application_ref',
              'credential_type',
              'device_id',
              'worker_id',
            };
            final workerId = credentialPayload['worker_id'];
            EntityRef? application;
            try {
              application = EntityRef.parse(
                '${credentialPayload['application_ref']}',
              );
            } on FormatException {
              // Rejected with the rest of the exact credential below.
            }
            if (credentialPayload.length != credentialFields.length ||
                !credentialPayload.keys.toSet().containsAll(credentialFields) ||
                application == null ||
                application.domain != claimedAccount.domain ||
                credentialPayload['credential_type'] != 'kaede-bot-device-v2' ||
                credentialPayload['device_id'] != claimedDevice ||
                workerId is! String ||
                !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(workerId) ||
                BigInt.parse(workerId) > BigInt.parse('9223372036854775807') ||
                credentialPayload['account'] !=
                    'bot:${application.wire}:worker:$workerId' ||
                await mobileBotE2eeDeviceId(
                      application,
                      workerId,
                      expectedIdentity,
                    ) !=
                    claimedDevice) {
              throw const FormatException(
                'A claimed bot key package has an invalid device credential.',
              );
            }
          } else {
            const credentialFields = <String>{
              'account',
              'credential_type',
              'device_id',
              'webhook_ref',
            };
            EntityRef? webhook;
            try {
              webhook = EntityRef.parse('${credentialPayload['webhook_ref']}');
            } on FormatException {
              // Rejected with the rest of the exact credential below.
            }
            if (credentialPayload.length != credentialFields.length ||
                !credentialPayload.keys.toSet().containsAll(credentialFields) ||
                webhook == null ||
                webhook != claimedAccount ||
                webhook.domain != claimedAccount.domain ||
                credentialPayload['credential_type'] !=
                    'kaede-webhook-device-v1' ||
                credentialPayload['device_id'] != claimedDevice ||
                credentialPayload['account'] != 'webhook:${webhook.wire}' ||
                await mobileWebhookE2eeDeviceId(
                      webhook,
                      expectedIdentity,
                    ) !=
                    claimedDevice) {
              throw const FormatException(
                'A claimed webhook key package has an invalid device credential.',
              );
            }
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
    List<EntityRef> mentionUserRefs = const [],
    EntityRef? referencedMessage,
    EntityRef? repliedUserRef,
    MobileEncryptedRichMessageOptions? rich,
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
            mentionUserRefs: mentionUserRefs,
            referencedMessage: referencedMessage,
            repliedUserRef: repliedUserRef,
            rich: rich,
          );
        },
      );

  Future<Map<String, Object?>> _encryptMessage(
    KaedeChannel channel,
    String content, {
    required String operation,
    required EntityRef? targetMessage,
    required List<Map<String, Object?>> attachments,
    required List<EntityRef> mentionUserRefs,
    required EntityRef? referencedMessage,
    required EntityRef? repliedUserRef,
    required MobileEncryptedRichMessageOptions? rich,
  }) async {
    _requireActive(channel);
    if ((operation == 'edit') != (targetMessage != null) ||
        !{'create', 'edit'}.contains(operation)) {
      throw ArgumentError(
        'Encrypted creates require no target and edits require one target.',
      );
    }
    if (rich != null) {
      return _encryptRichMessage(
        channel,
        content,
        operation: operation,
        targetMessage: targetMessage,
        attachments: attachments,
        mentionUserRefs: mentionUserRefs,
        referencedMessage: referencedMessage,
        repliedUserRef: repliedUserRef,
        rich: rich,
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
          applicationRef: null,
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

  Future<Map<String, Object?>> _encryptRichMessage(
    KaedeChannel channel,
    String content, {
    required String operation,
    required EntityRef? targetMessage,
    required List<Map<String, Object?>> attachments,
    required List<EntityRef> mentionUserRefs,
    required EntityRef? referencedMessage,
    required EntityRef? repliedUserRef,
    required MobileEncryptedRichMessageOptions rich,
  }) async {
    final canonicalAttachments = validateMobileEncryptedRichMessageAttachments(
      attachments,
      voiceMessage: rich.voiceMessage,
    ).toList(growable: false)
      ..sort((left, right) {
        final leftRef = '${left['attachment_id']}@${left['attachment_domain']}';
        final rightRef =
            '${right['attachment_id']}@${right['attachment_domain']}';
        return leftRef.compareTo(rightRef);
      });
    final stickerData = rich.stickerItems
        .map(
          (item) => <String, Object?>{
            'id': item.ref.id.value,
            'origin_domain': item.ref.domain.value,
            'name': item.name,
            'format_type': item.formatType,
          },
        )
        .toList(growable: false);
    final mentionRefs = mentionUserRefs.map((item) => item.wire).toList()
      ..sort();
    if (!_canonicalMobileSortedRefs(mentionRefs, 5000)) {
      throw const FormatException(
        'Encrypted rich message mention references are invalid.',
      );
    }
    final allowedMentions = validateMobileEncryptedAllowedMentions(
      rich.allowedMentions ??
          <String, Object?>{
            'parse': const <String>['everyone', 'roles', 'users'],
            'users': const <String>[],
            'roles': const <String>[],
            'replied_user': repliedUserRef != null,
          },
    );
    if ((allowedMentions['replied_user'] == true) != (repliedUserRef != null) ||
        repliedUserRef != null && referencedMessage == null) {
      throw const FormatException(
        'Encrypted rich message reply notification routing is incomplete.',
      );
    }
    final forward = rich.forward;
    final forwardSnapshot = forward == null
        ? null
        : validateMobileEncryptedForwardSnapshot(forward.snapshot);
    if (forward != null) {
      if (!isCanonicalBase64url32(forward.sourceProjectionDigest) ||
          forward.sourceCreatedAt != forwardSnapshot!['created_at'] ||
          forward.sourceEditedAt != forwardSnapshot['edited_at'] ||
          forward.sourceFlags != forwardSnapshot['flags'] ||
          forward.sourceMessageType != forwardSnapshot['message_type'] ||
          await mobileEncryptedForwardSnapshotProjectionDigest(
                forwardSnapshot,
              ) !=
              forward.sourceProjectionDigest) {
        throw const FormatException(
          'Encrypted forward source metadata is invalid.',
        );
      }
      final snapshotBytes = mobileCanonicalInteractionJson(
        forwardSnapshot['attachments'],
      );
      final attachmentBytes = mobileCanonicalInteractionJson(
        canonicalAttachments,
      );
      try {
        if (!_constantTimeEquals(snapshotBytes, attachmentBytes)) {
          throw const FormatException(
            'Encrypted forward attachments do not match the destination uploads.',
          );
        }
      } finally {
        snapshotBytes.fillRange(0, snapshotBytes.length, 0);
        attachmentBytes.fillRange(0, attachmentBytes.length, 0);
      }
    }
    final data = <String, Object?>{
      'content': content.trim().isEmpty ? null : content,
      'embeds': rich.embeds,
      'components': rich.components,
      'poll': rich.poll,
      'sticker_items': stickerData,
      'tts': rich.tts,
      'voice_message': rich.voiceMessage,
      'flags': rich.flags,
      'allowed_mentions': allowedMentions,
      'forward_snapshot': forwardSnapshot,
      'attachments': canonicalAttachments,
    };
    if (rich.flags < 0 ||
        rich.flags > 2147483647 ||
        rich.embeds.length > 10 ||
        rich.components.length > 40) {
      throw const FormatException('Encrypted rich message body is invalid.');
    }
    _validateMobileRichTree(rich.embeds, 0);
    _validateMobileRichTree(rich.components, 0);
    if (rich.poll != null) {
      final poll = rich.poll!;
      if (!_hasExactMobileRoutingFields(
            poll,
            const <String>{
              'question',
              'answers',
              'duration',
              'allow_multiselect',
              'layout_type',
            },
          ) ||
          poll['answers'] is! List ||
          (poll['answers']! as List).length < 2 ||
          (poll['answers']! as List).length > 10) {
        throw const FormatException('Encrypted poll is invalid.');
      }
      final question = _mobileRichPollMedia(poll['question'], answer: false);
      if (question['text'] is! String || question.containsKey('emoji')) {
        throw const FormatException('Encrypted poll question is invalid.');
      }
      for (final answer in poll['answers']! as List) {
        final raw = _mobileRoutingMap(answer, 'Encrypted poll answer');
        if (!_hasExactMobileRoutingFields(
          raw,
          const <String>{'poll_media'},
        )) {
          throw const FormatException('Encrypted poll answer is invalid.');
        }
        _mobileRichPollMedia(raw['poll_media'], answer: true);
      }
    }
    final componentsV2 = rich.components.any((item) => item['type'] != 1);
    if ((((rich.flags & (1 << 15)) != 0) != componentsV2) ||
        componentsV2 &&
            (data['content'] != null ||
                rich.embeds.isNotEmpty ||
                rich.poll != null ||
                stickerData.isNotEmpty) ||
        rich.voiceMessage &&
            (data['content'] != null ||
                rich.tts ||
                rich.embeds.isNotEmpty ||
                rich.components.isNotEmpty ||
                rich.poll != null ||
                stickerData.isNotEmpty ||
                canonicalAttachments.length != 1) ||
        forwardSnapshot != null && rich.poll != null ||
        data['content'] == null &&
            rich.embeds.isEmpty &&
            rich.components.isEmpty &&
            rich.poll == null &&
            stickerData.isEmpty &&
            canonicalAttachments.isEmpty &&
            forwardSnapshot == null) {
      throw const FormatException(
        'Encrypted rich message content combination is invalid.',
      );
    }
    final contract = await mobileInteractionRoutingContract(data, null);
    final hasControls = contract != null &&
        contract['components'] is List &&
        (contract['components']! as List).isNotEmpty;
    if (hasControls) {
      throw const FormatException(
        'Human encrypted messages cannot own application interaction controls.',
      );
    }
    final mentionIntent = mobileRichMessageMentionIntent(data);
    final requiredRecipients = <String>{
      ...mentionIntent.userRefs,
      if (repliedUserRef != null) repliedUserRef.wire,
    };
    final resolvedRecipients = mentionRefs.toSet();
    if (!resolvedRecipients.containsAll(requiredRecipients) ||
        mentionIntent.roleRefs.isEmpty &&
            !mentionIntent.everyone &&
            (resolvedRecipients.length != requiredRecipients.length ||
                !requiredRecipients.containsAll(resolvedRecipients))) {
      throw const FormatException(
        'Encrypted rich message resolved mention routing is invalid.',
      );
    }
    final contractDigest = contract == null
        ? null
        : await mobileInteractionRoutingContractDigest(contract);
    final attachmentRefs = canonicalAttachments
        .map(
          (item) => '${item['attachment_id']}@${item['attachment_domain']}',
        )
        .toList(growable: false);
    String? attachmentDigest;
    if (canonicalAttachments.isNotEmpty) {
      final encodedAttachments =
          mobileCanonicalInteractionJson(canonicalAttachments);
      try {
        attachmentDigest =
            _base64url((await Sha256().hash(encodedAttachments)).bytes);
      } finally {
        encodedAttachments.fillRange(0, encodedAttachments.length, 0);
      }
    }
    final revision = operation == 'create'
        ? '1'
        : _canonicalMobileUnsignedI63(
            rich.messageRevision,
            positive: true,
          );
    if (revision == null ||
        operation == 'edit' && BigInt.parse(revision) <= BigInt.one) {
      throw const FormatException(
        'Encrypted rich edits require the next positive message revision.',
      );
    }
    final richDigest = await mobileRichMessagePayloadDigest(data);
    final forwardProjectionDigest =
        await mobileRichMessageForwardProjectionDigest(data, mentionRefs);
    final forwardSnapshotDigest = forwardSnapshot == null
        ? null
        : await mobileEncryptedForwardSnapshotDigest(forwardSnapshot);
    final context = validateMobileRichMessageAuthenticatedContext(
      <String, Object?>{
        'application_ref': null,
        'attachment_manifest_digest': attachmentDigest,
        'author_ref': accountRef,
        'channel_ref': channel.ref.wire,
        'epoch': '${channel.encryptionEpoch}',
        'forward_projection_digest': forwardProjectionDigest,
        'forward_projection_version':
            forwardProjectionDigest == null ? null : 2,
        'forward_snapshot_digest': forwardSnapshotDigest,
        'forward_source_projection_digest': forward?.sourceProjectionDigest,
        'forwarded_channel_ref': forward?.sourceChannelRef.wire,
        'forwarded_created_at': forward?.sourceCreatedAt,
        'forwarded_edited_at': forward?.sourceEditedAt,
        'forwarded_flags': forward?.sourceFlags,
        'forwarded_message_ref': forward?.sourceMessageRef.wire,
        'forwarded_message_type': forward?.sourceMessageType,
        'group_id': channel.encryptionGroupId,
        'interaction_contract_digest': contractDigest,
        'interaction_installation_ref': null,
        'interaction_installation_revision': null,
        'interaction_integration_type': null,
        'message_attachment_refs': attachmentRefs,
        'message_custom_emoji_refs': mobileRichMessageCustomEmojiRefs(data),
        'message_mention_everyone': mentionIntent.everyone,
        'message_mention_refs': mentionRefs,
        'message_mention_role_refs': mentionIntent.roleRefs,
        'message_mention_user_refs': mentionIntent.userRefs,
        'message_replied_user_ref': repliedUserRef?.wire,
        'message_sticker_refs': mobileRichMessageStickerRefs(data),
        'message_flags': rich.flags,
        'message_revision': revision,
        'operation': operation,
        'policy_generation': '${channel.encryptionPolicyGeneration}',
        'referenced_message_ref': referencedMessage?.wire,
        'rich_payload_digest': richDigest,
        'sender_device_id': deviceId,
        'target_message': targetMessage?.wire,
        'tts': rich.tts,
        'view_persistent': false,
        'view_version': '0',
        'voice_message': rich.voiceMessage,
      },
    );
    final plaintext = mobileCanonicalInteractionJson(<String, Object?>{
      'version': 2,
      'kind': 'message',
      'context': context,
      'data': data,
    });
    final aad = mobileRichMessageAuthenticatedData(context);
    final groupId = _decode(channel.encryptionGroupId!, 128);
    Uint8List? ciphertext;
    try {
      ciphertext = _mls.encrypt(groupId, plaintext, aad);
      final envelope = <String, Object?>{
        'version': 2,
        'protocol': mlsProtocol,
        'suite': mlsSuite,
        'group_id': channel.encryptionGroupId,
        'policy_generation': '${channel.encryptionPolicyGeneration}',
        'epoch': '${channel.encryptionEpoch}',
        'forward_projection_digest': context['forward_projection_digest'],
        'forward_projection_version': context['forward_projection_version'],
        'forward_snapshot_digest': context['forward_snapshot_digest'],
        'forward_source_projection_digest':
            context['forward_source_projection_digest'],
        'forwarded_channel_ref': context['forwarded_channel_ref'],
        'forwarded_created_at': context['forwarded_created_at'],
        'forwarded_edited_at': context['forwarded_edited_at'],
        'forwarded_flags': context['forwarded_flags'],
        'forwarded_message_ref': context['forwarded_message_ref'],
        'forwarded_message_type': context['forwarded_message_type'],
        'sender_device_id': deviceId,
        'operation': operation,
        'ciphertext': _base64url(ciphertext),
        'author_ref': context['author_ref'],
        'message_revision': context['message_revision'],
        'message_attachment_refs': context['message_attachment_refs'],
        'message_custom_emoji_refs': context['message_custom_emoji_refs'],
        'message_mention_everyone': context['message_mention_everyone'],
        'message_mention_refs': context['message_mention_refs'],
        'message_mention_role_refs': context['message_mention_role_refs'],
        'message_mention_user_refs': context['message_mention_user_refs'],
        'message_replied_user_ref': context['message_replied_user_ref'],
        'message_sticker_refs': context['message_sticker_refs'],
        'referenced_message_ref': context['referenced_message_ref'],
        'rich_payload_digest': context['rich_payload_digest'],
        'application_ref': null,
        'interaction_integration_type': null,
        'interaction_installation_ref': null,
        'interaction_installation_revision': null,
        'view_version': '0',
        'view_persistent': false,
        'tts': rich.tts,
        'voice_message': rich.voiceMessage,
        'message_flags': rich.flags,
        if (targetMessage != null) 'target_message': targetMessage.wire,
        if (attachmentDigest != null)
          'attachment_manifest_digest': attachmentDigest,
        if (contract != null) ...<String, Object?>{
          'interaction_contract': contract,
          'interaction_contract_digest': contractDigest,
        },
      };
      _putMessageCache(
        '${envelope['ciphertext']}',
        MobileMessageCacheEntry(
          plaintext: utf8.decode(plaintext),
          authorRef: accountRef,
          messageRef: null,
          applicationRef: null,
        ),
      );
      return envelope;
    } finally {
      groupId.fillRange(0, groupId.length, 0);
      plaintext.fillRange(0, plaintext.length, 0);
      aad.fillRange(0, aad.length, 0);
      ciphertext?.fillRange(0, ciphertext.length, 0);
    }
  }

  Future<MobilePreparedEncryptedInteraction> encryptInteraction(
    KaedeChannel channel, {
    required EntityRef application,
    required String integrationType,
    required String interactionContext,
    required String interactionType,
    String? commandId,
    String? commandName,
    String? commandType,
    Object? componentType,
    String? customId,
    EntityRef? message,
    Object? responseId,
    EntityRef? target,
    Object? viewVersion,
    Object? autocompleteGeneration,
    String? focusedOption,
    Iterable<String> attachmentIds = const <String>[],
    Map<String, Map<String, Object?>> attachmentManifests =
        const <String, Map<String, Object?>>{},
    Map<String, Object?> options = const <String, Object?>{},
    List<String> values = const <String>[],
    List<Map<String, Object?>> components = const <Map<String, Object?>>[],
  }) =>
      _synchronized(() async {
        await _syncControlLog(channel);
        _requireActive(channel);
        if (const {'command', 'autocomplete'}.contains(interactionType) &&
            (values.isNotEmpty || components.isNotEmpty)) {
          throw const FormatException(
            'Encrypted command interactions may contain only command options.',
          );
        }
        if (interactionType == 'component' &&
            (options.isNotEmpty || components.isNotEmpty)) {
          throw const FormatException(
            'Encrypted component interactions may contain only selected values.',
          );
        }
        if (interactionType == 'modal_submit' &&
            (options.isNotEmpty || values.isNotEmpty)) {
          throw const FormatException(
            'Encrypted modal interactions may contain only submitted components.',
          );
        }
        final context = mobileInteractionAuthenticatedContext(
          channel,
          invoker: EntityRef.parse(accountRef),
          senderDeviceId: deviceId,
          application: application,
          integrationType: integrationType,
          interactionContext: interactionContext,
          interactionType: interactionType,
          commandId: commandId,
          commandName: commandName,
          commandType: commandType,
          componentType: componentType,
          customId: customId,
          message: message,
          responseId: responseId,
          target: target,
          viewVersion: viewVersion,
          autocompleteGeneration: autocompleteGeneration,
          focusedOption: focusedOption,
          attachmentIds: attachmentIds,
        );
        final files = _interactionAttachmentManifests(
          (context['attachment_ids']! as List).cast<String>(),
          attachmentManifests,
        );
        final plaintext = mobileCanonicalInteractionJson(<String, Object?>{
          'context': context,
          'data': <String, Object?>{
            'attachments': files,
            'components': components,
            'options': options,
            'values': values,
          },
          'kind': 'interaction',
          'version': 1,
        });
        final aad = mobileCanonicalInteractionJson(<String, Object?>{
          'context': context,
          'purpose': 'kaede.interaction.v1',
        });
        final groupId = _decode(channel.encryptionGroupId!, 128);
        String? attachmentManifestDigest;
        if (files.isNotEmpty) {
          final encodedFiles = mobileCanonicalInteractionJson(files);
          try {
            attachmentManifestDigest =
                _base64url((await Sha256().hash(encodedFiles)).bytes);
          } finally {
            encodedFiles.fillRange(0, encodedFiles.length, 0);
          }
        }
        Uint8List? ciphertext;
        try {
          ciphertext = _mls.encrypt(groupId, plaintext, aad);
          return MobilePreparedEncryptedInteraction(
            context: context,
            attachmentIds:
                List<String>.unmodifiable(context['attachment_ids']! as List),
            envelope: <String, Object?>{
              'version': 2,
              'protocol': mlsProtocol,
              'suite': mlsSuite,
              'group_id': channel.encryptionGroupId,
              'policy_generation': '${channel.encryptionPolicyGeneration}',
              'epoch': '${channel.encryptionEpoch}',
              'sender_device_id': deviceId,
              'operation': 'create',
              'ciphertext': _base64url(ciphertext),
              if (attachmentManifestDigest != null)
                'attachment_manifest_digest': attachmentManifestDigest,
            },
          );
        } finally {
          groupId.fillRange(0, groupId.length, 0);
          plaintext.fillRange(0, plaintext.length, 0);
          aad.fillRange(0, aad.length, 0);
          ciphertext?.fillRange(0, ciphertext.length, 0);
        }
      });

  Future<MobileDecryptedInteractionResponse> decryptInteractionResponse(
    KaedeChannel channel, {
    required String authorityDomain,
    required String interactionRef,
    required String responseRef,
    required String invokerRef,
    required String channelRef,
    required String applicationRef,
    required int sequence,
    required String revision,
    required int callbackType,
    required String operation,
    required Map<String, Object?> envelope,
    required List<Object?> attachments,
  }) =>
      _synchronized(() async {
        await _syncControlLog(channel);
        _requireEncrypted(channel);
        if (invokerRef != accountRef) {
          throw const FormatException(
            'Encrypted bot response targets another account.',
          );
        }
        final mlsOperation = operation == 'CREATE' ? 'create' : 'edit';
        final hasContract = envelope.containsKey('interaction_contract') &&
            envelope.containsKey('interaction_contract_digest');
        final hasPartialContract =
            envelope.containsKey('interaction_contract') !=
                envelope.containsKey('interaction_contract_digest');
        final fields = <String>{
          'version',
          'protocol',
          'suite',
          'group_id',
          'policy_generation',
          'epoch',
          'sender_device_id',
          'operation',
          'ciphertext',
          'interaction_ref',
          'response_ref',
          'sequence',
          'revision',
          'callback_type',
          'attachment_refs',
          if (hasContract) ...<String>{
            'interaction_contract',
            'interaction_contract_digest',
          },
          if (mlsOperation == 'edit') 'target_message',
        };
        final senderDeviceId = envelope['sender_device_id'];
        final rawAttachmentRefs = envelope['attachment_refs'];
        if (hasPartialContract ||
            envelope.length != fields.length ||
            !envelope.keys.toSet().containsAll(fields) ||
            envelope['version'] != 2 ||
            envelope['protocol'] != mlsProtocol ||
            envelope['suite'] != mlsSuite ||
            envelope['operation'] != mlsOperation ||
            senderDeviceId is! String ||
            envelope['ciphertext'] is! String ||
            envelope['interaction_ref'] is! String ||
            envelope['response_ref'] is! String ||
            envelope['sequence'] is! String ||
            envelope['revision'] is! String ||
            envelope['callback_type'] is! int ||
            !const <int>{4, 7, 8, 9}.contains(envelope['callback_type']) ||
            rawAttachmentRefs is! List ||
            rawAttachmentRefs.any((ref) => ref is! String) ||
            (mlsOperation == 'create'
                ? envelope.containsKey('target_message')
                : envelope['target_message'] != responseRef)) {
          throw const FormatException(
            'Encrypted bot response envelope is invalid.',
          );
        }
        if (callbackType == 9 && !hasContract ||
            callbackType == 8 && hasContract) {
          throw const FormatException(
            'Encrypted bot response routing contract is invalid.',
          );
        }
        final interactionContractDigest =
            hasContract ? envelope['interaction_contract_digest'] : null;
        if (interactionContractDigest != null &&
            !isCanonicalBase64url32(interactionContractDigest)) {
          throw const FormatException(
            'Encrypted bot response routing contract digest is invalid.',
          );
        }
        final interactionContract = hasContract
            ? validateMobileInteractionRoutingContract(
                envelope['interaction_contract'],
                callbackType,
              )
            : null;
        if (interactionContract != null &&
            await mobileInteractionRoutingContractDigest(
                  interactionContract,
                ) !=
                interactionContractDigest) {
          throw const FormatException(
            'Encrypted bot response routing contract digest is invalid.',
          );
        }
        final authority = Domain(authorityDomain);
        final transport = _mobileResponseAttachmentTransport(
          attachments,
          authority: authority,
          interactionRef: interactionRef,
          responseRef: responseRef,
        );
        final context = mobileInteractionResponseAuthenticatedContext(
          channel,
          authorityDomain: authorityDomain,
          interactionRef: interactionRef,
          responseRef: responseRef,
          invokerRef: invokerRef,
          channelRef: channelRef,
          applicationRef: applicationRef,
          sequence: sequence,
          revision: revision,
          callbackType: callbackType,
          operation: operation,
          attachmentRefs: transport.refs,
          interactionContractDigest: interactionContractDigest as String?,
          senderDeviceId: senderDeviceId,
        );
        if (envelope['group_id'] != context['group_id'] ||
            envelope['policy_generation'] != context['policy_generation'] ||
            envelope['epoch'] != context['epoch'] ||
            envelope['interaction_ref'] != context['interaction_ref'] ||
            envelope['response_ref'] != context['response_ref'] ||
            envelope['sequence'] != context['sequence'] ||
            envelope['revision'] != context['revision'] ||
            envelope['callback_type'] != context['callback_type'] ||
            (hasContract ? envelope['interaction_contract_digest'] : null) !=
                context['interaction_contract_digest'] ||
            !_listEquals(
              rawAttachmentRefs.cast<String>(),
              (context['attachment_refs']! as List).cast<String>(),
            )) {
          throw const FormatException(
            'Encrypted bot response context does not match its projection.',
          );
        }
        final groupId = _decode('${context['group_id']}', 128);
        final ciphertext =
            _decode(envelope['ciphertext']! as String, 64 * 1024);
        final expectedAad = mobileCanonicalInteractionJson(<String, Object?>{
          'context': context,
          'purpose': 'kaede.interaction.response.v1',
        });
        NativeMlsProcessed? processed;
        try {
          processed = _mls.process(groupId, ciphertext);
          if (processed.kind != 'application' ||
              processed.application == null ||
              processed.aad == null ||
              processed.credential == null ||
              !_constantTimeEquals(processed.aad!, expectedAad)) {
            throw const FormatException(
              'Encrypted bot response authenticated context is invalid.',
            );
          }
          _validateMobileBotResponseCredential(processed.credential!, context);
          final plaintext = Map<String, Object?>.from(
            jsonDecode(
              utf8.decode(processed.application!, allowMalformed: false),
            ) as Map,
          );
          const plaintextFields = <String>{
            'version',
            'kind',
            'context',
            'data',
          };
          if (plaintext.length != plaintextFields.length ||
              !plaintext.keys.toSet().containsAll(plaintextFields) ||
              plaintext['version'] != 1 ||
              plaintext['kind'] != 'interaction_response' ||
              plaintext['context'] is! Map) {
            throw const FormatException(
              'Encrypted bot response plaintext is invalid.',
            );
          }
          final receivedContext =
              mobileCanonicalInteractionJson(plaintext['context']);
          final expectedContext = mobileCanonicalInteractionJson(context);
          try {
            if (!_constantTimeEquals(receivedContext, expectedContext)) {
              throw const FormatException(
                'Encrypted bot response plaintext is invalid.',
              );
            }
          } finally {
            receivedContext.fillRange(0, receivedContext.length, 0);
            expectedContext.fillRange(0, expectedContext.length, 0);
          }
          final data = _authenticatedMobileInteractionResponseData(
            plaintext['data'],
            context,
            transport.transport,
          );
          await _validateMobileInteractionRoutingContractForData(
            data,
            context['callback_type']! as int,
            interactionContract,
            context['interaction_contract_digest'] as String?,
          );
          return MobileDecryptedInteractionResponse(
            context: Map.unmodifiable(context),
            data: Map.unmodifiable(data),
          );
        } finally {
          groupId.fillRange(0, groupId.length, 0);
          ciphertext.fillRange(0, ciphertext.length, 0);
          expectedAad.fillRange(0, expectedAad.length, 0);
          for (final bytes in <Uint8List?>[
            processed?.application,
            processed?.aad,
            processed?.credential,
          ]) {
            bytes?.fillRange(0, bytes.length, 0);
          }
        }
      });

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
    final ({
      Map<String, Object?> context,
      Map<String, Object?>? contract,
    })? richProjection = envelope.containsKey('rich_payload_digest')
        ? await _mobileRichMessageProjection(channel, message, envelope)
        : null;
    final ciphertextText = '${envelope['ciphertext']}';
    final processedKey = _processedCacheKey(
      message.ref,
      message.channelRef,
      message.authorRef,
      envelope,
      message.applicationRef,
      message.webhookRef,
    );
    final groupId = _decode('${envelope['group_id']}', 32);
    final ciphertext = _decode(ciphertextText, 64 * 1024);
    try {
      final expectedContext = richProjection?.context ??
          <String, Object?>{
            'channel_ref': channel.ref.wire,
            'group_id': '${envelope['group_id']}',
            'policy_generation': '${envelope['policy_generation']}',
            'epoch': '${envelope['epoch']}',
            'sender_device_id': '${envelope['sender_device_id']}',
            'operation': '${envelope['operation']}',
            'target_message': envelope['target_message'],
            'attachment_manifest_digest':
                envelope['attachment_manifest_digest'],
          };
      NativeMlsProcessed? processed;
      Uint8List? expectedAad;
      try {
        var cached = _messageCache[ciphertextText];
        if (_processed.containsKey(processedKey) && cached == null) {
          throw const FormatException(
            'Encrypted message plaintext is no longer available in the safe cache.',
          );
        }
        if (richProjection != null && cached == null) {
          final revision = BigInt.parse(
            richProjection.context['message_revision']! as String,
          );
          for (final candidate in _messageCache.values) {
            if (candidate.messageRef != message.ref.wire) continue;
            try {
              final previous = Map<String, Object?>.from(
                  jsonDecode(candidate.plaintext) as Map);
              if (previous['version'] != 2 || previous['kind'] != 'message') {
                continue;
              }
              final previousContext =
                  validateMobileRichMessageAuthenticatedContext(
                previous['context'],
              );
              if (BigInt.parse(
                      previousContext['message_revision']! as String) >=
                  revision) {
                throw const FormatException(
                  'Encrypted rich message revision is stale or replayed.',
                );
              }
            } on FormatException catch (error) {
              if ('$error'.contains('stale or replayed')) rethrow;
            }
          }
        }
        String? plaintext;
        if (cached != null) {
          if (cached.authorRef != message.authorRef.wire) {
            throw const FormatException(
              'Encrypted message cache does not match its author.',
            );
          }
          if (cached.applicationRef != message.applicationRef?.wire) {
            throw const FormatException(
              'Encrypted message cache does not match its app attribution.',
            );
          }
          if (cached.webhookRef != message.webhookRef?.wire) {
            throw const FormatException(
              'Encrypted message cache does not match its webhook attribution.',
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
          expectedAad = richProjection == null
              ? _messageContextBytes(expectedContext)
              : mobileRichMessageAuthenticatedData(expectedContext);
          if (!_constantTimeEquals(processed.aad!, expectedAad)) {
            throw const FormatException(
                'Encrypted message authenticated context was modified.');
          }
          validateMobileEncryptedMessageSenderCredential(
            processed.credential!,
            message,
            '${envelope['sender_device_id']}',
          );
          plaintext =
              utf8.decode(processed.application!, allowMalformed: false);
        }
        final decoded = Map<String, Object?>.from(jsonDecode(plaintext) as Map);
        late final DecryptedE2EEApplication result;
        if (richProjection != null) {
          final rawContext = decoded['context'];
          if (decoded.length != 4 ||
              !decoded.keys.toSet().containsAll(
                const <String>{'version', 'kind', 'context', 'data'},
              ) ||
              decoded['version'] != 2 ||
              decoded['kind'] != 'message' ||
              rawContext is! Map ||
              !decoded.containsKey('data')) {
            throw const FormatException(
              'Encrypted rich message plaintext is invalid.',
            );
          }
          final received = mobileCanonicalInteractionJson(rawContext);
          final expected = mobileCanonicalInteractionJson(
            richProjection.context,
          );
          try {
            if (!_constantTimeEquals(received, expected)) {
              throw const FormatException(
                'Encrypted rich message plaintext context was modified.',
              );
            }
          } finally {
            received.fillRange(0, received.length, 0);
            expected.fillRange(0, expected.length, 0);
          }
          result = await _authenticatedMobileRichMessageApplication(
            decoded['data'],
            richProjection.context,
            richProjection.contract,
            message,
          );
        } else {
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
              'Encrypted message plaintext is invalid.',
            );
          }
          if (rawAttachments.length > 10 ||
              rawAttachments.any(
                (item) =>
                    item is! Map || item.keys.any((key) => key is! String),
              )) {
            throw const FormatException(
              'Encrypted message attachment manifests are invalid.',
            );
          }
          final rawManifests = rawAttachments
              .map((item) => Map<String, Object?>.from(item as Map))
              .toList(growable: false);
          if (envelope['attachment_manifest_digest']
              case final String expected) {
            final actual = _base64url(
              (await Sha256().hash(utf8.encode(jsonEncode(rawManifests))))
                  .bytes,
            );
            if (actual != expected) {
              throw const FormatException(
                'Encrypted attachment manifest was modified.',
              );
            }
          } else if (rawManifests.isNotEmpty) {
            throw const FormatException(
              'Encrypted attachment manifest is unauthenticated.',
            );
          }
          final attachmentRefs = <String>{};
          final attachments = rawManifests.map((manifest) {
            final attachmentId = manifest['attachment_id'];
            final validated = _interactionAttachmentManifest(
              attachmentId is String ? attachmentId : '',
              manifest,
            );
            final ref =
                '${validated['attachment_id']}@${validated['attachment_domain']}';
            if (!attachmentRefs.add(ref)) {
              throw const FormatException(
                'Encrypted message attachment identity is duplicated.',
              );
            }
            return validated;
          }).toList(growable: false);
          result = DecryptedE2EEApplication(
            content: decoded['content']! as String,
            attachments: attachments,
          );
        }
        _putMessageCache(
          ciphertextText,
          cached ??
              MobileMessageCacheEntry(
                plaintext: plaintext,
                authorRef: message.authorRef.wire,
                messageRef: message.ref.wire,
                applicationRef: message.applicationRef?.wire,
                webhookRef: message.webhookRef?.wire,
              ),
        );
        _processed[processedKey] = result;
        return result;
      } finally {
        expectedAad?.fillRange(0, expectedAad.length, 0);
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
        result.add(decrypted?.applyTo(message) ?? message);
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
